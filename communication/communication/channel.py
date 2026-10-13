"""Blocking, single-task-per-connection tensor channel over TCP.

The channel owns framing only: it never interprets model semantics. Timeouts are
enforced twice -- a per-operation socket timeout and an absolute task deadline -- and a
caller-supplied cancel event is polled while waiting so that an operator cancel or
a controller-side termination stops the transfer instead of blocking on a peer.

``deadline`` is an absolute **epoch-seconds** instant (``time.time()`` base), the same
base the Controller uses for ``assignment["deadline"]`` and node-agent uses for
``ExecutionContext.deadline``. Both the cancel event and the deadline are re-checked
on every send and receive iteration (writes are sliced rather than one blocking
``sendall``), so a peer that keeps streaming or stops reading cannot postpone them.

Connection setup checks cancellation and the absolute deadline before and after address
resolution, and while establishing TCP. The standard-library ``getaddrinfo`` call is
synchronous and cannot be interrupted, so resolver latency is outside the connect timeout,
deadline, and cancel guarantees. Use a numeric IP address when connection setup must stay
within a strict time bound.
"""

from __future__ import annotations

import errno
import select
import socket
import time
from typing import Any, Mapping

from .errors import (ChannelClosed, ChannelTimeout, PeerError, ProtocolError,
                     TaskCancelled, TransportError)
from .protocol import (PREFIX_SIZE, Frame, FrameType, decode_header,
                       decode_tensor, encode_frame, encode_tensor,
                       parse_prefix)

CANCEL_POLL_SECONDS = 0.25
DEFAULT_CONNECT_TIMEOUT = 10.0


def _remaining(deadline: float | None) -> float | None:
    """Seconds left before an epoch-seconds deadline; ``None`` when none is set."""
    return None if deadline is None else max(deadline - time.time(), 0.0)


def _connect_with_guards(host: str, port: int, connect_timeout: float,
                         deadline: float | None, cancel) -> socket.socket:
    """Connect without allowing cancellation or an absolute deadline to be stranded."""
    if cancel is not None and cancel.is_set():
        raise TaskCancelled("cancelled before connecting")
    left = _remaining(deadline)
    if left is not None and left <= 0:
        raise ChannelTimeout("task deadline exceeded while connecting")
    budget = connect_timeout if left is None else min(connect_timeout, left)
    deadline_bounded = left is not None and left <= connect_timeout
    end = time.monotonic() + budget
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_error: OSError | None = None
    pending_errors = (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                      getattr(errno, "WSAEINPROGRESS", errno.EINPROGRESS),
                      getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK))
    for family, socktype, proto, _canonname, sockaddr in addresses:
        if cancel is not None and cancel.is_set():
            raise TaskCancelled("cancelled while connecting")
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise ChannelTimeout("task deadline exceeded while connecting")
        if time.monotonic() >= end:
            break
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setblocking(False)
            result = sock.connect_ex(sockaddr)
            if result not in (0, *pending_errors):
                raise OSError(result, "connect failed")
            while result != 0:
                if cancel is not None and cancel.is_set():
                    raise TaskCancelled("cancelled while connecting")
                left = _remaining(deadline)
                if left is not None and left <= 0:
                    raise ChannelTimeout("task deadline exceeded while connecting")
                wait = min(CANCEL_POLL_SECONDS, max(end - time.monotonic(), 0.0))
                if left is not None:
                    wait = min(wait, left)
                if wait <= 0:
                    break
                _readable, writable, exceptional = select.select([], [sock], [sock], wait)
                if not writable and not exceptional:
                    continue
                result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result:
                    raise OSError(result, "connect failed")
            if result == 0:
                sock.setblocking(True)
                return sock
        except (TaskCancelled, ChannelTimeout):
            sock.close()
            raise
        except OSError as exc:
            last_error = exc
            sock.close()
    if cancel is not None and cancel.is_set():
        raise TaskCancelled("cancelled while connecting")
    if _remaining(deadline) == 0:
        raise ChannelTimeout("task deadline exceeded while connecting")
    if deadline_bounded and time.monotonic() >= end:
        raise ChannelTimeout("task deadline exceeded while connecting")
    if last_error is not None:
        raise last_error
    raise TimeoutError("timed out while connecting")


def _recv_exactly(sock: socket.socket, size: int, deadline: float | None, cancel,
                  budget: float | None = None) -> bytes:
    """Read ``size`` bytes, honouring the cancel event, the task deadline and a read budget.

    Cancel and deadline are re-checked on every iteration rather than only when a read times
    out, so a peer that keeps streaming bytes cannot postpone a cancel or overrun the deadline.
    ``budget`` is the per-operation seconds allowance (``Channel.timeout``); ``None`` waits as
    long as progress is made.
    """
    chunks: list[bytes] = []
    remaining = size
    started = time.monotonic()
    while remaining:
        if cancel is not None and cancel.is_set():
            raise TaskCancelled("cancelled while receiving")
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise ChannelTimeout("task deadline exceeded while receiving")
        elapsed = time.monotonic() - started
        if budget is not None and elapsed >= budget:
            raise ChannelTimeout("timed out while receiving")
        limit = CANCEL_POLL_SECONDS
        if budget is not None:
            limit = min(limit, max(budget - elapsed, 0.0))
        if left is not None:
            limit = min(limit, left)
        sock.settimeout(max(limit, 0.001))
        try:
            chunk = sock.recv(remaining)
        except socket.timeout:
            continue
        except OSError as exc:
            raise ChannelClosed(f"socket closed while receiving: {exc}") from exc
        if not chunk:
            raise ChannelClosed("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_exactly(sock: socket.socket, data: bytes, deadline: float | None, cancel,
                  budget: float | None = None) -> None:
    """Write ``data`` fully, honouring the cancel event, the task deadline and a write budget.

    The payload is written in bounded slices instead of one blocking ``sendall``, and cancel and
    deadline are re-checked on every iteration, so a peer that stops reading cannot postpone a
    cancel or overrun the deadline. ``budget`` is the per-operation seconds allowance
    (``Channel.timeout``); ``None`` waits as long as progress is made.
    """
    view = memoryview(data)
    sent = 0
    started = time.monotonic()
    while sent < len(view):
        if cancel is not None and cancel.is_set():
            raise TaskCancelled("cancelled while sending")
        left = _remaining(deadline)
        if left is not None and left <= 0:
            raise ChannelTimeout("task deadline exceeded while sending")
        elapsed = time.monotonic() - started
        if budget is not None and elapsed >= budget:
            raise ChannelTimeout("timed out while sending")
        limit = CANCEL_POLL_SECONDS
        if budget is not None:
            limit = min(limit, max(budget - elapsed, 0.0))
        if left is not None:
            limit = min(limit, left)
        sock.settimeout(max(limit, 0.001))
        try:
            count = sock.send(view[sent:])
        except socket.timeout:
            continue
        except OSError as exc:
            raise ChannelClosed(f"socket closed while sending: {exc}") from exc
        if not count:
            raise ChannelClosed("peer closed the connection while sending")
        sent += count


class Channel:
    """A framed request/response link to one peer stage."""

    def __init__(self, sock: socket.socket, *, timeout: float | None = None, deadline: float | None = None,
                 cancel=None, peer: Mapping[str, Any] | None = None):
        self._sock = sock
        self.timeout = timeout
        self.deadline = deadline
        self.cancel = cancel
        self.peer: Mapping[str, Any] | None = peer
        self.closed = False

    @classmethod
    def connect(cls, address: tuple[str, int] | str, *, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                **kwargs) -> "Channel":
        host, port = _split_address(address)
        try:
            sock = _connect_with_guards(host, port, connect_timeout, kwargs.get("deadline"),
                                        kwargs.get("cancel"))
        except OSError as exc:
            raise ChannelClosed(f"cannot connect to {host}:{port}: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, **kwargs)

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise TaskCancelled("cancelled before sending")

    def send_frame(self, kind: FrameType, header: Mapping[str, Any], payload: bytes = b"") -> None:
        self._check_cancel()
        raw = encode_frame(Frame(kind, header, payload))
        _send_exactly(self._sock, raw, self.deadline, self.cancel, self.timeout)

    def recv_frame(self) -> Frame:
        prefix = _recv_exactly(self._sock, PREFIX_SIZE, self.deadline, self.cancel, self.timeout)
        kind, header_len, payload_len = parse_prefix(prefix)
        header = decode_header(_recv_exactly(self._sock, header_len, self.deadline, self.cancel,
                                             self.timeout)) if header_len else {}
        payload = _recv_exactly(self._sock, payload_len, self.deadline, self.cancel,
                                self.timeout) if payload_len else b""
        return Frame(kind, header, payload)

    def send_hello(self, **fields: Any) -> None:
        self.send_frame(FrameType.HELLO, {"kind": "hello", **fields})

    def recv_hello(self) -> Mapping[str, Any]:
        frame = self.expect(FrameType.HELLO)
        return frame.header

    def send_tensor(self, name: str, array, *, kind: FrameType = FrameType.TENSOR, **fields: Any) -> int:
        header, payload = encode_tensor(name, array)
        self.send_frame(kind, {**fields, **header}, payload)
        return len(payload)

    def recv_tensor(self, expected: FrameType | tuple[FrameType, ...] = FrameType.TENSOR):
        frame = self.expect(expected)
        return decode_tensor(frame.header, frame.payload), frame.header

    def send_result(self, name: str, array, **fields: Any) -> int:
        return self.send_tensor(name, array, kind=FrameType.RESULT, **fields)

    def recv_result(self):
        return self.recv_tensor(FrameType.RESULT)

    def send_error(self, message: str, code: str = "remote_error") -> None:
        self.send_frame(FrameType.ERROR, {"kind": "error", "code": code, "message": message})

    def send_cancel(self, reason: str = "cancelled by peer") -> None:
        self.send_frame(FrameType.CANCEL, {"kind": "cancel", "reason": reason})

    def send_bye(self) -> None:
        self.send_frame(FrameType.BYE, {"kind": "bye"})

    def expect(self, kinds: FrameType | tuple[FrameType, ...]) -> Frame:
        wanted = kinds if isinstance(kinds, tuple) else (kinds,)
        frame = self.recv_frame()
        if frame.type == FrameType.ERROR:
            raise PeerError(str(frame.header.get("message", "remote stage failed")),
                            str(frame.header.get("code", "remote_error")))
        if frame.type == FrameType.CANCEL:
            raise TaskCancelled(str(frame.header.get("reason", "cancelled by peer")))
        if frame.type not in wanted:
            names = ", ".join(kind.name for kind in wanted)
            raise ProtocolError(f"expected {names}, received {frame.type.name}")
        return frame

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()

    def __enter__(self) -> "Channel":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class ChannelListener:
    """Accept loop bound to one address, one task connection at a time."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, backlog: int = 8):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(backlog)
        self.host, self.port = self._sock.getsockname()

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    def accept(self, *, timeout: float | None = None, deadline: float | None = None, cancel=None,
               channel_timeout: float | None = None, **kwargs) -> Channel:
        started = time.monotonic()
        while True:
            if cancel is not None and cancel.is_set():
                raise TaskCancelled("cancelled while accepting")
            left = _remaining(deadline)
            if left is not None and left <= 0:
                raise ChannelTimeout("task deadline exceeded while accepting")
            limit = CANCEL_POLL_SECONDS
            if timeout is not None:
                limit = min(limit, max(timeout - (time.monotonic() - started), 0.0))
            if left is not None:
                limit = min(limit, left)
            if limit <= 0:
                raise ChannelTimeout("timed out waiting for an upstream connection")
            self._sock.settimeout(limit)
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                remaining = _remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise ChannelTimeout("task deadline exceeded while accepting") from None
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise ChannelTimeout("timed out waiting for an upstream connection") from None
                continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return Channel(conn, timeout=channel_timeout, deadline=deadline, cancel=cancel, **kwargs)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "ChannelListener":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _split_address(address: tuple[str, int] | str) -> tuple[str, int]:
    if isinstance(address, tuple):
        return address[0], int(address[1])
    host, _, port = address.rpartition(":")
    if not host or not port.isdigit():
        raise TransportError(f"invalid address {address!r}; expected host:port")
    return host, int(port)
