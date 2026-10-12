import socket
import time

import numpy as np
import pytest

inference_engine = pytest.importorskip("inference_engine")

from inference_engine import PipelineClient, forward_full
from inference_engine.config import tiny_config

from lastcore_node_agent.inference_adapter import build_stage_executor

from tests.helpers import make_config, run_in_thread, wait_until

MODEL_ID = "lastcore/tiny-cpu"


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_agent_completes_real_inference_through_stage_executor(fake, client, clock, tmp_path):
    port = free_port()
    config = make_config(tmp_path / "state", executor="stage",
                         endpoint=f"http://127.0.0.1:{port}")
    from tests.test_agent import make_agent

    agent = make_agent(fake, client, config, clock, build_stage_executor(config))
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    # The stage executor now hands the assignment deadline to the engine, which
    # enforces it on the real epoch clock (same base as the Controller), so the
    # fixture deadline must be a real epoch value, not the fake agent clock's frame.
    task_id, assignment_id = fake.add_task("node-1", layers=(0, 4), model_id=MODEL_ID,
                                           deadline=time.time() + 60)
    assert wait_until(lambda: agent.stats.started == [assignment_id], timeout=15)
    tokens = np.asarray([[1, 2, 3, 4]], dtype=np.int64)
    logits = PipelineClient(("127.0.0.1", port), model_id=MODEL_ID,
                            task_id=task_id).run(tokens, task_id=task_id)
    config_expected = tiny_config()
    expected = forward_full(tokens, dict(np.load(config.state_dir / "tiny-model.npz")),
                            config_expected)
    assert np.array_equal(logits, expected)
    assert wait_until(lambda: fake.tasks[task_id]["status"] == "completed", timeout=15)
    reports = [r for r in fake.reports if r[0] == assignment_id]
    assert [r[1] for r in reports] == ["running", "completed"]
    agent.stop_event.set()
    thread.join(10)
    assert not thread.is_alive()
    assert "error" not in captured
