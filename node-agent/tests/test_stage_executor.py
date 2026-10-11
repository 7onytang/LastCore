import socket
import threading
import time

import numpy as np
import pytest

inference_engine = pytest.importorskip("inference_engine")

from inference_engine import PipelineClient, forward_full, generate_weights, save_weights
from inference_engine.errors import ShardError

from lastcore_node_agent.executor import ExecutionContext, ExecutionCancelled
from lastcore_node_agent.inference_adapter import StageExecutor

from tests.helpers import wait_until

MODEL_ID = "lastcore/tiny-cpu"
PROMPT = np.asarray([[1, 2, 3]], dtype=np.int64)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_context(port, layers=(0, 4), raw_layers=(0, 4), deadline=None):
    return ExecutionContext(
        task_id="task-1", assignment_id="asg-1", model_id=MODEL_ID, position=0,
        layer_start=layers[0], layer_end=layers[1], previous_node=None,
        next_node=None, deadline=deadline if deadline is not None else time.time() + 60,
        raw={
            "id": "asg-1", "task_id": "task-1", "node_id": "node-1", "position": 0,
            "layer_start": raw_layers[0], "layer_end": raw_layers[1],
            "previous_node": None, "next_node": None, "status": "pending", "error": None,
            "request": {"model_id": MODEL_ID, "num_layers": 4, "layer_memory_mb": 512},
            "deadline": time.time() + 60,
        })


@pytest.fixture
def checkpoint(tmp_path):
    from inference_engine.config import tiny_config

    config = tiny_config()
    weights_path = tmp_path / "tiny.npz"
    save_weights(weights_path, generate_weights(config, seed=0))
    return config, weights_path


def run_executor(executor, context):
    captured = {}

    def target():
        try:
            executor.run(context)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, captured


def test_stage_executor_serves_request_and_completes(checkpoint):
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=1)
    context = make_context(port)
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: _port_accepts(port), timeout=10)
    client = PipelineClient(("127.0.0.1", port), model_id=MODEL_ID, task_id="task-1")
    logits = client.run(PROMPT, task_id="task-1")
    expected = forward_full(PROMPT, dict(np.load(weights)), config)
    assert np.array_equal(logits, expected)
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert "error" not in captured


def test_stage_executor_cancel_stops_and_releases_port(checkpoint):
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=None)
    context = make_context(port)
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: _port_accepts(port), timeout=10)
    context.request_cancel("task-terminal")
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert isinstance(captured.get("error"), ExecutionCancelled)
    assert captured["error"].reason == "task-terminal"
    time.sleep(0.2)
    assert not _port_accepts(port)


def test_stage_executor_serves_until_cancelled_between_requests(checkpoint):
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}", complete_after=None)
    context = make_context(port)
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: _port_accepts(port), timeout=10)
    client = PipelineClient(("127.0.0.1", port), model_id=MODEL_ID, task_id="task-1")
    first = client.run(PROMPT, task_id="task-1")
    second = client.run(PROMPT, task_id="task-1")
    assert np.array_equal(first, second)
    context.request_cancel("shutdown")
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert isinstance(captured.get("error"), ExecutionCancelled)


def test_stage_executor_invalid_endpoint_fails_fast(checkpoint):
    config, weights = checkpoint
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint="not-an-endpoint", complete_after=1)
    with pytest.raises(ShardError):
        executor.run(make_context(free_port()))


def test_stage_executor_tolerates_controller_trailing_slash_endpoints(checkpoint):
    config, weights = checkpoint
    port = free_port()
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{port}/", complete_after=1)
    context = make_context(port)
    context.raw["next_node"] = None
    thread, captured = run_executor(executor, context)
    assert wait_until(lambda: _port_accepts(port), timeout=10)
    client = PipelineClient(("127.0.0.1", port), model_id=MODEL_ID, task_id="task-1")
    logits = client.run(PROMPT, task_id="task-1")
    expected = forward_full(PROMPT, dict(np.load(weights)), config)
    assert np.array_equal(logits, expected)
    assert wait_until(lambda: not thread.is_alive(), timeout=10)
    assert "error" not in captured


def test_stage_executor_segment_mismatch_fails(checkpoint):
    config, weights = checkpoint
    executor = StageExecutor(model_config=config, weights_path=weights,
                             endpoint=f"http://127.0.0.1:{free_port()}", complete_after=1)
    context = make_context(free_port(), raw_layers=(0, 9))
    with pytest.raises(Exception):
        executor.run(context)


def _port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False
