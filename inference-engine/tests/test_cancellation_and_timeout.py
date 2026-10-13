import socket
import threading
import time

import numpy as np
import pytest

from communication import Channel
from inference_engine import (ChannelTimeout, PipelineClient, PeerError,
                              StageServer, TaskCancelled, generate_weights,
                              load_stage, save_weights)
from inference_engine.config import tiny_config


@pytest.fixture
def checkpoint(tmp_path):
    config = tiny_config()
    path = tmp_path / "tiny.npz"
    save_weights(path, generate_weights(config, seed=4))
    return config, path


def test_client_cancel_event_aborts_before_connecting(checkpoint):
    config, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers))
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(TaskCancelled):
            PipelineClient(server.address, cancel=cancel).run(np.array([[1, 2]], dtype=np.int64))
        assert server.processed == 0
    finally:
        server.close()


def test_cancel_event_stops_the_stage_before_any_work(checkpoint):
    config, path = checkpoint
    cancel = threading.Event()
    cancel.set()
    server = StageServer(load_stage(config, path, 0, config.n_layers), timeout=0.2, cancel=cancel)
    try:
        assert server.serve_forever(max_requests=1) == []
        with pytest.raises(TaskCancelled):
            server.serve_once()
        assert server.processed == 0
    finally:
        server.close()


def test_upstream_cancel_frame_stops_the_stage(checkpoint):
    config, path = checkpoint
    server = StageServer(load_stage(config, path, 2, 4), timeout=5)
    failure: list[Exception] = []

    def swallow():
        try:
            server.serve_once()
        except Exception as exc:  # noqa: BLE001 - propagated to the upstream as a CANCEL frame
            failure.append(exc)

    thread = threading.Thread(target=swallow)
    thread.start()
    try:
        link = Channel.connect(server.address, timeout=5)
        link.send_hello(role="client", layer_end=2)
        link.recv_hello()
        link.send_cancel("upstream aborted the task")
        with pytest.raises(TaskCancelled, match="cancelled"):
            link.recv_tensor()
        link.close()
    finally:
        thread.join(timeout=20)
        server.close()
    assert failure and isinstance(failure[0], TaskCancelled)
    assert server.processed == 0


def test_silent_peer_hits_the_read_timeout(checkpoint):
    config, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers), timeout=0.3)
    silent = socket.create_connection(server.address, timeout=5)
    try:
        started = time.monotonic()
        with pytest.raises(ChannelTimeout):
            server.serve_once()
        assert time.monotonic() - started < 10
    finally:
        silent.close()
        server.close()


def test_deadline_expiry_stops_the_stage(checkpoint):
    config, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers), timeout=30,
                         deadline=time.monotonic() + 0.3)
    try:
        with pytest.raises(ChannelTimeout, match="deadline"):
            server.serve_once()
    finally:
        server.close()


def test_idle_timeout_does_not_kill_a_waiting_node(checkpoint):
    config, path = checkpoint
    stop = threading.Event()
    server = StageServer(load_stage(config, path, 0, config.n_layers), timeout=0.2)
    thread = threading.Thread(target=server.serve_forever, kwargs={"should_stop": stop.is_set})
    thread.start()
    time.sleep(0.6)
    assert thread.is_alive()
    try:
        logits = PipelineClient(server.address).run(np.array([[1, 2]], dtype=np.int64))
        assert logits.shape[-1] == config.vocab_size
    finally:
        stop.set()
        thread.join(timeout=20)
        server.close()


def test_stage_reports_a_local_failure_to_its_upstream(checkpoint):
    config, path = checkpoint
    server = StageServer(load_stage(config, path, 0, config.n_layers), timeout=5)
    thread = threading.Thread(target=_expect_failure, args=(server,))
    thread.start()
    try:
        with pytest.raises(PeerError, match="token ids"):
            PipelineClient(server.address).run(np.array([[99]], dtype=np.int64))
    finally:
        thread.join(timeout=20)
        server.close()


def _expect_failure(server):
    try:
        server.serve_once()
    except Exception:  # noqa: BLE001 - the upstream is told through an ERROR frame
        pass
