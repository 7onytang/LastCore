import time

import numpy as np
import pytest

from inference_engine import assignment_plan, load_assigned_stage, load_stage, parse_endpoint
from inference_engine.errors import ShardError


def assignment(**overrides):
    payload = {
        "id": "assignment-1", "task_id": "task-1", "node_id": "node-1", "position": 0,
        "layer_start": 0, "layer_end": 2, "status": "pending", "error": None,
        "previous_node": None,
        "next_node": {"node_id": "node-2", "endpoint": "http://10.0.0.12:9000", "id": "assignment-2"},
        "task_status": "queued",
        "request": {"model_id": "lastcore/tiny-cpu", "num_layers": 4, "layer_memory_mb": 64,
                    "max_nodes": 2, "timeout_seconds": 600},
        "deadline": 1_700_000_000.0,
    }
    payload.update(overrides)
    return payload


def test_first_segment_maps_to_a_downstream_peer():
    plan = assignment_plan(assignment())
    assert (plan.layer_start, plan.layer_end) == (0, 2)
    assert plan.is_first and not plan.is_last
    assert plan.next_endpoint == ("10.0.0.12", 9000)
    assert plan.previous_endpoint is None
    assert plan.model_id == "lastcore/tiny-cpu"
    assert plan.num_layers == 4
    assert plan.deadline == 1_700_000_000.0


def test_last_segment_maps_to_an_upstream_peer():
    plan = assignment_plan(assignment(id="assignment-2", position=1, layer_start=2, layer_end=4,
                                      previous_node={"node_id": "node-1",
                                                     "endpoint": "http://10.0.0.11:9000",
                                                     "id": "assignment-1"},
                                      next_node=None))
    assert plan.is_last and not plan.is_first
    assert plan.previous_endpoint == ("10.0.0.11", 9000)
    assert plan.next_endpoint is None


def test_mid_pipeline_segment_keeps_both_peers():
    plan = assignment_plan(assignment(layer_start=1, layer_end=3,
                                      previous_node={"endpoint": "https://10.0.0.11:9000"},
                                      next_node={"endpoint": "10.0.0.13:9000"}))
    assert not plan.is_first and not plan.is_last
    assert (plan.previous_endpoint, plan.next_endpoint) == (("10.0.0.11", 9000), ("10.0.0.13", 9000))


@pytest.mark.parametrize("endpoint", ["10.0.0.1", "host:port", "", "http://10.0.0.1/"])
def test_unusable_endpoints_are_rejected(endpoint):
    with pytest.raises(ShardError):
        parse_endpoint(endpoint)


def test_segment_longer_than_the_task_is_rejected():
    with pytest.raises(ShardError, match="layers"):
        assignment_plan(assignment(layer_end=6))


def test_inverted_segment_is_rejected():
    with pytest.raises(ShardError, match="invalid layer segment"):
        assignment_plan(assignment(layer_start=2, layer_end=2))


def test_missing_fields_are_rejected():
    with pytest.raises(ShardError, match="required fields"):
        assignment_plan({"id": "a"})


def test_peer_without_an_endpoint_is_rejected():
    with pytest.raises(ShardError, match="endpoint"):
        assignment_plan(assignment(next_node={"node_id": "node-2"}))


def test_load_assigned_stage_binds_task_and_deadline(tmp_path):
    import numpy as np

    from inference_engine import generate_weights, save_weights
    from inference_engine.config import tiny_config

    config = tiny_config()
    path = tmp_path / "tiny.npz"
    save_weights(path, generate_weights(config, seed=2))
    runtime, plan = load_assigned_stage(assignment(), config, path)
    assert runtime.task_id == "task-1"
    assert runtime.assignment_id == "assignment-1"
    assert runtime.layer_start == 0 and runtime.layer_end == 2
    assert plan.deadline == 1_700_000_000.0
    hidden = runtime.execute(np.array([[1, 2]], dtype=np.int64))
    assert hidden.shape == (1, 2, config.d_model)


def test_model_mismatch_is_rejected(tmp_path):
    from inference_engine import generate_weights, save_weights
    from inference_engine.config import tiny_config

    config = tiny_config()
    path = tmp_path / "tiny.npz"
    save_weights(path, generate_weights(config, seed=2))
    with pytest.raises(ShardError, match="targets model"):
        load_assigned_stage(assignment(request={"model_id": "other/model", "num_layers": 4}),
                            config, path)
