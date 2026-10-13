"""Deadline-stack regression: StageExecutor + ChannelListener epoch handling.

LC-262 ports PR21's reviewed deadline/communication-cancel fixes onto the PR22
baseline (adapter hands ``plan.deadline`` to ``StageServer``; the communication
layer interprets ``deadline`` as epoch seconds and re-checks cancel/deadline on
every send/receive iteration; ``serve_forever`` re-raises ``ChannelTimeout``
once the epoch deadline expires). These tests prove the fixed behaviour on the
real stack with real clocks — no cancel event is set in the deadline tests, so
only the engine-level deadline path can stop the stage.

Scope is deliberately narrow: expiry, future-expiry, listener epoch handling,
cancel resource reclamation, and one bounded blocked-send cancel probe. Major
lease/stale-epoch, auth, result-backfill and digest contracts are untouched.
"""

import socket
import threading
import time

import pytest

inference_engine = pytest.importorskip("inference_engine")

from inference_engine import generate_weights, save_weights

from lastcore_node_agent.executor import ExecutionCancelled, ExecutionContext
from lastcore_node_agent.inference_adapter import StageExecutor

from communication.channel import Channel, ChannelListener
from communication.errors import ChannelTimeout, TaskCancelled
from communication.protocol import FrameType

MODEL_ID = "lastcore/tiny-cpu"


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_context(port, deadline):
    return ExecutionContext(
        task_id="task-1", assignment_id="asg-1", model_id=MODEL_ID, position=0,
        layer_start=0, layer_end=4, previous_node=None, next_node=None,
        deadline=deadline,
        raw={
            "id": "asg-1", "task_id": "task-1", "node_id": "node-1", "position": 0,
            "layer_start": 0, "layer_end": 4,
            "previous_node": None, "next_node": None, "status": "pending", "error": None,
            "request": {"model_id": MODEL_ID, "num_layers": 4, "layer_memory_mb": 512},
            "deadline": deadline,
        })


def run_executor(executor, context):
    captured = {}

    def target():
        try:
            executor.run(context)
        except BaseException as exc:  # noqa: BLE001 - the test asserts on the outcome
            captured["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, captured


def _port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def checkpoint(tmp_path):
    from inference_engine.config import tiny_config

    config = tiny_config()
    weights_path = tmp_path / "tiny.npz"
    save_weights(weights_path, generate_weights(config, seed=0))
    return config, weights_path


def test_expired_epoch_deadline_stops_stage_without_cancel(checkpoint):
    """Expired epoch deadline stops the stage via the engine deadline path."""
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=None,
                             serve_poll=0.05)
    context = make_context(port, time.time() - 10)
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert isinstance(captured.get("error"), ExecutionCancelled)
    assert captured["error"].reason == "deadline"
    time.sleep(0.3)
    assert not _port_accepts(port)


def test_future_epoch_deadline_stops_stage_after_expiry(checkpoint):
    """A near-future epoch deadline stops the stage once it passes."""
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=None,
                             serve_poll=0.05)
    context = make_context(port, time.time() + 0.5)
    started = time.monotonic()
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert time.monotonic() - started < 10
    assert isinstance(captured.get("error"), ExecutionCancelled)
    assert captured["error"].reason == "deadline"
    time.sleep(0.3)
    assert not _port_accepts(port)


def test_listener_expired_epoch_deadline_raises_immediately():
    """ChannelListener honours an expired epoch deadline (LC-257 probe case)."""
    listener = ChannelListener("127.0.0.1", 0)
    try:
        started = time.monotonic()
        with pytest.raises(ChannelTimeout, match="deadline"):
            listener.accept(deadline=time.time() - 10)
        assert time.monotonic() - started < 5
    finally:
        listener.close()


def test_cancel_reclaims_stage_resources(checkpoint):
    """Explicit cancel stops the stage and releases the bound port."""
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=None,
                             serve_poll=0.05)
    context = make_context(port, time.time() + 60)
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: _port_accepts(port), timeout=10)
    context.request_cancel("task-terminal")
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert isinstance(captured.get("error"), ExecutionCancelled)
    assert captured["error"].reason == "task-terminal"
    time.sleep(0.3)
    assert not _port_accepts(port)


def test_blocked_send_cancel_is_bounded():
    """A send stalled by a saturated peer still honours cancel promptly.

    The pair is first saturated with blocking writes (no reader) until the
    kernel itself reports the stall, so the subsequent framed send cannot
    finish before the cancel fires; ``_send_exactly`` must re-check the cancel
    event on every write iteration instead of blocking inside one ``sendall``.
    """
    socket_a, socket_b = socket.socketpair()
    socket_a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    socket_a.settimeout(3.0)
    try:
        while True:
            socket_a.sendall(b"\x00" * (1 * 1024 * 1024))
    except socket.timeout:
        pass  # saturated: the stall below is real, not timing luck
    channel = Channel(socket_a, timeout=30.0)
    cancel = threading.Event()
    channel.cancel = cancel
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    try:
        with pytest.raises(TaskCancelled):
            channel.send_frame(FrameType.TENSOR, {"kind": "tensor"},
                               b"\x00" * (8 * 1024 * 1024))
        assert time.monotonic() - started < 8.0
    finally:
        socket_a.close()
        socket_b.close()
