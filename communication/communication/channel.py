"""Blocking, single-task-per-connection tensor channel over TCP.

The channel owns framing only: it never interprets model semantics. Timeouts are
enforced twice -- a per-read socket timeout and an absolute task deadline -- and a
caller-supplied cancel event is polled while waiting so that an operator cancel or
a controller-side termination stops the transfer instead of blocking on a peer.
"""

from __future__ import annotations

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


def _recv_exactly(sock: socket.socket, size: int, deadline: float | None, cancel) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        if cancel is not None and cancel.is_set():
            raise TaskCancelled("cancelled while receiving")
        limit = CANCEL_POLL_SECONDS if deadline is None else min(CANCEL_POLL_SECONDS, max(deadline - time.monotonic(), 0.0))
        if limit <= 0:
            raise ChannelTimeout("task deadline exceeded while receiving")
        sock.settimeout(limit)
        try:
            chunk = sock.recv(remaining)
        except socket.timeout:
            if cancel is not None and cancel.is_set():
                raise TaskCancelled("cancelled while receiving") from None
            if deadline is not None and time.monotonic() >= deadline:
                raise ChannelTimeout("task deadline exceeded while receiving") from None
            continue
        except OSError as exc:
            raise ChannelClosed(f"socket closed while receiving: {exc}") from exc
        if not chunk:
            raise ChannelClosed("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


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
            sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as exc:
            raise ChannelClosed(f"cannot connect to {host}:{port}: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, **kwargs)

    def _deadline(self) -> float | None:
        if self.deadline is None and self.timeout is None:
            return None
        limit = time.monotonic() + (self.timeout if self.timeout is not None else CANCEL_POLL_SECONDS)
        return min(limit, self.deadline) if self.deadline is not None else limit

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise TaskCancelled("cancelled before sending")

    def send_frame(self, kind: FrameType, header: Mapping[str, Any], payload: bytes = b"") -> None:
        self._check_cancel()
        raw = encode_frame(Frame(kind, header, payload))
        limit = self._deadline()
        if limit is not None:
            self._sock.settimeout(max(limit - time.monotonic(), 0.001))
        try:
            self._sock.sendall(raw)
        except socket.timeout as exc:
            raise ChannelTimeout("timed out while sending") from exc
        except OSError as exc:
            raise ChannelClosed(f"socket closed while sending: {exc}") from exc

    def recv_frame(self) -> Frame:
        prefix = _recv_exactly(self._sock, PREFIX_SIZE, self._deadline(), self.cancel)
        kind, header_len, payload_len = parse_prefix(prefix)
        deadline = self._deadline()
        header = decode_header(_recv_exactly(self._sock, header_len, deadline, self.cancel)) if header_len else {}
        payload = _recv_exactly(self._sock, payload_len, deadline, self.cancel) if payload_len else b""
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
            now = time.monotonic()
            limit = CANCEL_POLL_SECONDS
            if timeout is not None:
                limit = min(limit, max(timeout - (now - started), 0.0))
            if deadline is not None:
                limit = min(limit, max(deadline - now, 0.0))
            if limit <= 0:
                if deadline is not None and now >= deadline:
                    raise ChannelTimeout("task deadline exceeded while accepting")
                raise ChannelTimeout("timed out waiting for an upstream connection")
            self._sock.settimeout(limit)
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise ChannelTimeout("task deadline exceeded while accepting") from None
                if timeout is not None and now - started >= timeout:
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
