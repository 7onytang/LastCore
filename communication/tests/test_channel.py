import errno
import socket
import threading
import time

import numpy as np
import pytest

from communication.channel import Channel, ChannelListener
import communication.channel as channel_module
from communication.errors import (ChannelClosed, ChannelTimeout, PeerError,
                                  ProtocolError, TaskCancelled)
from communication.protocol import MAGIC, PREFIX, VERSION, FrameType


@pytest.fixture
def pair():
    left, right = socket.socketpair()
    yield Channel(left), Channel(right)
    left.close()
    right.close()


def test_hello_tensor_result_roundtrip(pair):
    client, server = pair
    client.send_hello(role="client", model_id="m", layer_end=0)
    assert server.recv_hello()["role"] == "client"
    server.send_hello(role="first", layer_start=0, layer_end=2)
    assert client.recv_hello()["layer_end"] == 2
    hidden = np.arange(8, dtype=np.float32).reshape(1, 2, 4)
    client.send_tensor("hidden", hidden, task_id="t1", assignment_id="a1")
    received, header = server.recv_tensor()
    assert np.array_equal(received, hidden)
    assert header["task_id"] == "t1"
    server.send_result("logits", received * 2, task_id="t1")
    logits, _ = client.recv_result()
    assert np.array_equal(logits, hidden * 2)
    client.send_bye()
    assert server.recv_frame().type is FrameType.BYE


def test_error_frame_becomes_peer_error(pair):
    client, server = pair
    server.send_error("model shard missing", code="ShardError")
    with pytest.raises(PeerError) as error:
        client.recv_result()
    assert error.value.code == "ShardError"
    assert "model shard missing" in str(error.value)


def test_cancel_frame_becomes_task_cancelled(pair):
    client, server = pair
    server.send_cancel("operator cancelled")
    with pytest.raises(TaskCancelled, match="operator cancelled"):
        client.recv_tensor()


def test_unexpected_frame_type_is_a_protocol_error(pair):
    client, server = pair
    server.send_bye()
    with pytest.raises(ProtocolError, match="received BYE"):
        client.recv_tensor()


def test_closed_peer_raises_channel_closed(pair):
    client, server = pair
    client.close()
    with pytest.raises(ChannelClosed):
        server.recv_frame()


def test_read_timeout_is_enforced(pair):
    client, server = pair
    client.timeout = 0.2
    started = time.monotonic()
    with pytest.raises(ChannelTimeout):
        client.recv_frame()
    assert time.monotonic() - started < 5


def test_absolute_deadline_is_enforced(pair):
    client, server = pair
    client.deadline = time.time() + 0.2
    client.timeout = 30
    with pytest.raises(ChannelTimeout, match="deadline"):
        client.recv_frame()


def test_cancel_event_is_polled_before_sending(pair):
    client, server = pair
    cancel = threading.Event()
    client.cancel = cancel
    cancel.set()
    with pytest.raises(TaskCancelled):
        client.send_hello()


def test_cancel_event_interrupts_a_blocked_read(pair):
    client, server = pair
    cancel = threading.Event()
    client.cancel = cancel
    client.timeout = None
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelled):
        client.recv_tensor()
    assert time.monotonic() - started < 5


def _blocked_send_channel(timeout=None, deadline=None):
    """A channel whose peer never reads, so a large frame cannot drain."""
    socket_a, socket_b = socket.socketpair()
    socket_a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    channel = Channel(socket_a, timeout=timeout, deadline=deadline)
    return channel, socket_a, socket_b


def test_cancel_event_interrupts_a_blocked_send():
    channel, socket_a, socket_b = _blocked_send_channel(timeout=5.0)
    cancel = threading.Event()
    channel.cancel = cancel
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    try:
        with pytest.raises(TaskCancelled):
            channel.send_frame(FrameType.TENSOR, {"kind": "tensor"}, b"\x00" * (8 * 1024 * 1024))
        assert time.monotonic() - started < 2.0
    finally:
        socket_a.close()
        socket_b.close()


def test_absolute_deadline_interrupts_a_blocked_send():
    channel, socket_a, socket_b = _blocked_send_channel(deadline=time.time() + 0.3)
    started = time.monotonic()
    try:
        with pytest.raises(ChannelTimeout, match="deadline"):
            channel.send_frame(FrameType.TENSOR, {"kind": "tensor"}, b"\x00" * (8 * 1024 * 1024))
        assert time.monotonic() - started < 2.0
    finally:
        socket_a.close()
        socket_b.close()


def _stream_unfinished_frame(sock, *, declared_payload=64 * 1024 * 1024, streamed=4 * 1024 * 1024,
                             chunk=16 * 1024, interval=0.004, budget=2.0):
    """Announce a large payload, then keep sending bytes that never complete it."""
    header = b"{}"
    sock.sendall(PREFIX.pack(MAGIC, VERSION, int(FrameType.TENSOR), len(header), declared_payload) + header)
    sent, started = 0, time.monotonic()
    try:
        while sent < streamed and time.monotonic() - started < budget:
            size = min(chunk, streamed - sent)
            sock.sendall(b"\x00" * size)
            sent += size
            time.sleep(interval)
    except OSError:
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def test_cancel_is_checked_while_the_peer_keeps_streaming():
    """A peer that never stops sending must not be able to postpone a cancel."""
    listener = ChannelListener("127.0.0.1", 0)
    try:
        peer = socket.create_connection(listener.address, timeout=5)
        stream = threading.Thread(target=_stream_unfinished_frame, args=(peer,), daemon=True)
        stream.start()
        conn = listener.accept(timeout=5)
        cancel = threading.Event()
        conn.cancel = cancel
        conn.timeout = 30
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(TaskCancelled):
            conn.recv_frame()
        assert time.monotonic() - started < 2.0
        conn.close()
    finally:
        listener.close()


def test_deadline_is_checked_while_the_peer_keeps_streaming():
    """A streaming peer must not be able to overrun the task deadline."""
    listener = ChannelListener("127.0.0.1", 0)
    try:
        peer = socket.create_connection(listener.address, timeout=5)
        stream = threading.Thread(target=_stream_unfinished_frame, args=(peer,), daemon=True)
        stream.start()
        conn = listener.accept(timeout=5)
        conn.timeout = 30
        conn.deadline = time.time() + 0.3
        started = time.monotonic()
        with pytest.raises(ChannelTimeout, match="deadline"):
            conn.recv_frame()
        assert time.monotonic() - started < 2.0
        conn.close()
    finally:
        listener.close()


def test_connect_to_a_closed_port_fails_fast():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()
    with pytest.raises(ChannelClosed, match="cannot connect"):
        Channel.connect((host, port), connect_timeout=2)


def test_connect_cancelled_before_socket_creation(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    called = False

    def resolve(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("cancelled connect must not resolve or open a socket")

    monkeypatch.setattr(channel_module.socket, "getaddrinfo", resolve)
    with pytest.raises(TaskCancelled, match="before connecting"):
        Channel.connect(("127.0.0.1", 1), cancel=cancel)
    assert not called


def test_connect_deadline_is_checked_after_synchronous_name_resolution(monkeypatch):
    socket_created = False

    def slow_resolve(*_args, **_kwargs):
        time.sleep(0.05)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 1))]

    def unexpected_socket(*_args, **_kwargs):
        nonlocal socket_created
        socket_created = True
        raise AssertionError("expired deadline must stop before TCP socket creation")

    monkeypatch.setattr(channel_module.socket, "getaddrinfo", slow_resolve)
    monkeypatch.setattr(channel_module.socket, "socket", unexpected_socket)
    with pytest.raises(ChannelTimeout, match="deadline exceeded while connecting"):
        Channel.connect(("example.test", 1), deadline=time.time() + 0.01)
    assert not socket_created


class _PendingSocket:
    def __init__(self):
        self.closed = False
        self.connect_called = False

    def setblocking(self, _enabled):
        return None

    def connect_ex(self, _address):
        self.connect_called = True
        return errno.EINPROGRESS

    def getsockopt(self, *_args):
        return 0

    def close(self):
        self.closed = True


def _patch_pending_connect(monkeypatch, pending):
    monkeypatch.setattr(channel_module.socket, "getaddrinfo",
                        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM,
                                                     0, "", ("127.0.0.1", 1))])
    monkeypatch.setattr(channel_module.socket, "socket", lambda *_args: pending)


def test_connect_cancel_interrupts_pending_socket(monkeypatch):
    pending = _PendingSocket()
    cancel = threading.Event()
    _patch_pending_connect(monkeypatch, pending)

    def poll(*_args, **_kwargs):
        cancel.set()
        return [], [], []

    monkeypatch.setattr(channel_module.select, "select", poll)
    with pytest.raises(TaskCancelled, match="while connecting"):
        Channel.connect(("127.0.0.1", 1), cancel=cancel)
    assert pending.connect_called and pending.closed


def test_connect_deadline_interrupts_pending_socket(monkeypatch):
    pending = _PendingSocket()
    _patch_pending_connect(monkeypatch, pending)
    monkeypatch.setattr(channel_module.select, "select", lambda *_args, **_kwargs: ([], [], []))
    with pytest.raises(ChannelTimeout, match="while connecting"):
        Channel.connect(("127.0.0.1", 1), deadline=time.time() + 0.05)
    assert pending.connect_called and pending.closed


def test_listener_exposes_an_ephemeral_port_and_completes_a_roundtrip():
    listener = ChannelListener("127.0.0.1", 0)
    try:
        assert listener.port > 0
        result = {}

        def serve():
            conn = listener.accept(timeout=10)
            try:
                result["hello"] = conn.recv_hello()
                conn.send_hello(role="last")
                tensor, _ = conn.recv_tensor()
                conn.send_result("logits", tensor + 1)
            finally:
                conn.close()

        thread = threading.Thread(target=serve)
        thread.start()
        client = Channel.connect(listener.address, timeout=10)
        client.send_hello(role="client")
        assert client.recv_hello()["role"] == "last"
        client.send_tensor("hidden", np.zeros((1, 1, 2), dtype=np.float32))
        logits, _ = client.recv_result()
        assert np.array_equal(logits, np.ones((1, 1, 2), dtype=np.float32))
        client.close()
        thread.join()
        assert result["hello"]["role"] == "client"
    finally:
        listener.close()


def test_listener_accept_timeout_is_reported():
    listener = ChannelListener("127.0.0.1", 0)
    try:
        with pytest.raises(ChannelTimeout):
            listener.accept(timeout=0.2)
    finally:
        listener.close()


def test_invalid_address_string_is_rejected():
    from communication.errors import TransportError
    with pytest.raises(TransportError, match="host:port"):
        Channel.connect("not-an-address")
