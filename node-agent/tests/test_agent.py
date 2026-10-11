import sys
import threading
import time
import types

import httpx
import pytest

from lastcore_node_agent.agent import CredentialRejected, NodeAgent
from lastcore_node_agent.credentials import (
    NodeCredentials,
    load_credentials,
    load_settled,
    save_credentials,
)
from lastcore_node_agent.executor import (
    ExecutionContext,
    ExecutionCancelled,
    Executor,
    SliceDemoExecutor,
    load_executor,
)

from tests.helpers import FactoryRecorder, make_config, run_in_thread, wait_until


class SlowExecutor(Executor):
    name = "slow"

    def __init__(self, slices=200, slice_seconds=0.05):
        self.slices = slices
        self.slice_seconds = slice_seconds
        self.ran = False
        self.cancel_observed = False

    def run(self, context):
        self.ran = True
        for _ in range(self.slices):
            if context.cancel.is_set():
                self.cancel_observed = True
                raise ExecutionCancelled(context.cancel_reason)
            time.sleep(self.slice_seconds)


class ExplodingExecutor(Executor):
    name = "explode"

    def run(self, context):
        raise ValueError("boom")


def make_agent(fake_unused, client, config, clock, executor_factory=None):
    return NodeAgent(config, client, clock=lambda: clock[0], executor_factory=executor_factory)


def stop_agent(thread, captured):
    thread.join(5)
    assert not thread.is_alive()
    assert "error" not in captured


def test_happy_path_registers_persists_and_completes(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SliceDemoExecutor(slices=2, slice_seconds=0.01))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1", layers=(0, 4))
    assert wait_until(lambda: fake.tasks[task_id]["status"] == "completed")
    reports = [r for r in fake.reports if r[0] == assignment_id]
    assert [r[1] for r in reports] == ["running", "completed"]
    assert load_settled(config.state_dir).get(assignment_id) == "completed"
    assert agent.stats.completed == [assignment_id]
    assert load_credentials(config.state_dir) is not None
    agent.stop_event.set()
    stop_agent(thread, captured)

    second = make_agent(fake, client, config, clock, factory.create)
    thread2, captured2 = run_in_thread(second)
    assert wait_until(lambda: second.stats.heartbeats_ok >= 1)
    assert fake.register_calls == 1
    second.stop_event.set()
    stop_agent(thread2, captured2)


def test_dedup_single_execution_per_assignment(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SlowExecutor(slices=40, slice_seconds=0.05))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: fake.tasks[task_id]["status"] == "completed", timeout=15)
    assert agent.stats.polls > 5
    assert factory.instances and len(factory.instances) == 1
    assert agent.stats.started == [assignment_id]
    assert wait_until(lambda: agent.stats.completed == [assignment_id])
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_cancel_stops_executor_without_completion_report(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SlowExecutor(slices=200, slice_seconds=0.05))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: agent.stats.started == [assignment_id])
    fake.set_task_status(task_id, "cancelled")
    assert wait_until(lambda: agent.stats.stopped == [assignment_id], timeout=5)
    executor = factory.instances[0]
    assert executor.cancel_observed
    assert [r[1] for r in fake.reports if r[0] == assignment_id] == ["running"]
    assert load_settled(config.state_dir).get(assignment_id) == "stopped"
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_deadline_stops_execution(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SlowExecutor(slices=200, slice_seconds=0.05))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1", deadline_offset=0.3)
    assert wait_until(lambda: agent.stats.started == [assignment_id])
    clock[0] += 1.0
    assert wait_until(lambda: agent.stats.stopped == [assignment_id], timeout=5)
    assert factory.instances[0].cancel_observed
    assert [r[1] for r in fake.reports if r[0] == assignment_id] == ["running"]
    assert load_settled(config.state_dir).get(assignment_id) == "stopped"
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_executor_failure_reports_failed(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(ExplodingExecutor)
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: agent.stats.failed == [assignment_id])
    assert [r[1] for r in fake.reports if r[0] == assignment_id] == ["running", "failed"]
    assert fake.reports[-1][2] == "ValueError: boom"
    assert fake.tasks[task_id]["status"] == "failed"
    assert load_settled(config.state_dir).get(assignment_id) == "failed"
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_unknown_executor_spec_reports_failed(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state", executor="missing_module_xyz:Thing")
    agent = make_agent(fake, client, config, clock)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: agent.stats.failed == [assignment_id])
    assert fake.reports[-1][2].startswith("executor unavailable")
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_heartbeat_stall_cancels_running_task(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SlowExecutor(slices=200, slice_seconds=0.05))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: agent.stats.started == [assignment_id])
    fake.heartbeat_error = httpx.ConnectError("down")
    heartbeats_before = agent.stats.heartbeats_ok
    assert wait_until(lambda: agent.stats.heartbeats_failed >= 2, timeout=5)
    clock[0] += 31.0
    assert wait_until(lambda: agent.stats.stopped == [assignment_id], timeout=5)
    assert factory.instances[0].cancel_observed
    assert agent.stats.heartbeats_ok == heartbeats_before
    assert [r[1] for r in fake.reports if r[0] == assignment_id] == ["running"]
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_saved_credentials_rejected_stops_without_reregistering(fake, client, clock, tmp_path):
    state_dir = tmp_path / "state"
    save_credentials(state_dir, NodeCredentials("ghost", "bad-token", 30))
    config = make_config(state_dir)
    agent = make_agent(fake, client, config, clock)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: "error" in captured, timeout=5)
    assert isinstance(captured["error"], CredentialRejected)
    assert fake.register_calls == 0
    thread.join(5)
    assert not thread.is_alive()


def test_shutdown_cancels_running_executor(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SlowExecutor(slices=200, slice_seconds=0.05))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: agent.stats.started == [assignment_id])
    agent.shutdown()
    assert wait_until(lambda: agent.stats.stopped == [assignment_id], timeout=5)
    assert factory.instances[0].cancel_observed
    stop_agent(thread, captured)


def test_terminal_task_never_started(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SliceDemoExecutor(slices=1, slice_seconds=0.0))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, _ = fake.add_task("node-1")
    fake.set_task_status(task_id, "cancelled")
    time.sleep(0.15)
    assert agent.stats.started == []
    assert fake.reports == []
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_oldest_task_starts_first(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SliceDemoExecutor(slices=1, slice_seconds=0.0))
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    _, first = fake.add_task("node-1")
    _, second = fake.add_task("node-1")
    assert wait_until(lambda: len(agent.stats.completed) == 2, timeout=10)
    assert agent.stats.started == [first, second]
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_agent_retries_until_controller_reachable(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(lambda: SliceDemoExecutor(slices=2, slice_seconds=0.01))
    agent = make_agent(fake, client, config, clock, factory.create)
    fake.register_error = httpx.ConnectError("controller down")
    thread, captured = run_in_thread(agent)
    time.sleep(0.15)
    assert fake.register_calls == 0
    fake.register_error = None
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1")
    assert wait_until(lambda: fake.tasks[task_id]["status"] == "completed")
    agent.stop_event.set()
    stop_agent(thread, captured)


class ContextRecordingExecutor(Executor):
    name = "record"

    def __init__(self):
        self.contexts = []

    def run(self, context):
        self.contexts.append(context)
        for _ in range(100):
            if context.cancel.is_set():
                raise ExecutionCancelled(context.cancel_reason)
            time.sleep(0.01)


def test_execution_context_carries_raw_assignment(fake, client, clock, tmp_path):
    config = make_config(tmp_path / "state")
    factory = FactoryRecorder(ContextRecordingExecutor)
    agent = make_agent(fake, client, config, clock, factory.create)
    thread, captured = run_in_thread(agent)
    assert wait_until(lambda: fake.register_calls == 1)
    task_id, assignment_id = fake.add_task("node-1", layers=(2, 5), model_id="m")
    assert wait_until(lambda: agent.stats.started == [assignment_id])
    assert wait_until(lambda: factory.instances and factory.instances[0].contexts, timeout=5)
    context = factory.instances[0].contexts[0]
    assert context.raw["id"] == assignment_id
    assert context.raw["task_id"] == task_id
    assert context.raw["request"]["model_id"] == "m"
    assert context.raw["layer_start"] == 2
    agent.stop_event.set()
    stop_agent(thread, captured)


def test_load_executor_specs():
    assert isinstance(load_executor("demo"), SliceDemoExecutor)
    with pytest.raises(ValueError):
        load_executor("unknown")
    with pytest.raises(ValueError):
        load_executor("no_colon_spec")
    module = types.ModuleType("fake_exec_mod")
    module.Factory = SlowExecutor
    sys.modules["fake_exec_mod"] = module
    try:
        executor = load_executor("fake_exec_mod:Factory")
        assert isinstance(executor, SlowExecutor)
    finally:
        sys.modules.pop("fake_exec_mod", None)


def test_execution_context_request_cancel():
    context = ExecutionContext(
        task_id="t", assignment_id="a", model_id="m", position=0,
        layer_start=0, layer_end=4, previous_node=None, next_node=None, deadline=1.0)
    assert not context.cancel.is_set()
    context.request_cancel("deadline")
    assert context.cancel.is_set()
    assert context.cancel_reason == "deadline"
