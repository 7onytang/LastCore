import json
import threading
import time
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linear_edge import LinearExecutor, MODEL, origin, result_file
from lastcore_node_agent.executor import ExecutionContext, ExecutionCancelled


def context(**request_changes):
    request = {"model_id": MODEL, "input_vector": [1, 2, 3, 4], "weight_seed": 7}
    request.update(request_changes)
    return ExecutionContext(task_id="task", assignment_id="assignment", model_id=request["model_id"],
                            position=0, layer_start=0, layer_end=2, previous_node=None,
                            next_node=None, deadline=time.time()+20, raw={"request": request})


def test_real_values_and_durable_replay_without_recompute(tmp_path, monkeypatch):
    executor = LinearExecutor(tmp_path)
    run = context()
    executor.run(run)
    path = result_file(tmp_path, run.assignment_id)
    saved = json.loads(path.read_text())
    assert saved["result"] == [0.9999684130511179, -0.9047475044778314]
    before = path.read_bytes()
    monkeypatch.setattr("linear_edge.weight", lambda *args: pytest.fail("must not recompute saved outcome"))
    executor.run(run)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="Saved computation"):
        executor.run(context(weight_seed=8))


@pytest.mark.parametrize("changes", [
    {"model_id": "unrecognized"}, {"input_vector": []}, {"input_vector": [float("nan")]},
    {"input_vector": [float("inf")]}, {"input_vector": [True]}, {"input_vector": [1]*257},
    {"input_vector": [1e20]}, {"weight_seed": -1}, {"weight_seed": True},
])
def test_no_result_written_for_invalid_workload(tmp_path, changes):
    with pytest.raises(ValueError):
        LinearExecutor(tmp_path).run(context(**changes))
    assert not list(tmp_path.rglob("*.json"))


def test_cancellation_precedes_compute_and_cache_write(tmp_path):
    run = context()
    run.cancel.set()
    with pytest.raises(ExecutionCancelled):
        LinearExecutor(tmp_path).run(run)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com?q=x", "https://example.com#x"])
def test_remote_origin_restrictions(url):
    with pytest.raises(ValueError):
        origin(url)


def test_loopback_origin_is_allowed_for_native_acceptance():
    assert origin("http://127.0.0.1:4567/") == "http://127.0.0.1:4567"
