import socket
import threading
import time

import numpy as np
import pytest

from communication.channel import Channel, ChannelListener
from communication.errors import (ChannelClosed, ChannelTimeout, PeerError,
                                  ProtocolError, TaskCancelled)
from communication.protocol import FrameType


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
    client.deadline = time.monotonic() + 0.2
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


def test_connect_to_a_closed_port_fails_fast():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    host, port = probe.getsockname()
    probe.close()
    with pytest.raises(ChannelClosed, match="cannot connect"):
        Channel.connect((host, port), connect_timeout=2)


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
