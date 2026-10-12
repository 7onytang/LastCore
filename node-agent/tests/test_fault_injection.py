"""LC-7 fault injection: restart, out-of-order and late reporting.

Scenarios run the real NodeAgent against the real controller app (Store +
FastAPI, in-process via an ASGI transport) with one shared controllable clock
driving both sides. Scenario/expectation mapping lives in
docs/lc7-lease-epoch-state-transitions.md; the currently-observed baseline
behavior (672b4f7) is what these tests assert, and the lease/epoch proposal
marks which assertions change once approved and implemented.
"""

import sys
import threading
import time
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.controller import ControllerClient, ControllerError
from lastcore_node_agent.credentials import load_settled
from lastcore_node_agent.executor import ExecutionCancelled, Executor, SliceDemoExecutor

from tests.helpers import FactoryRecorder, make_config, run_in_thread, wait_until

REPO_ROOT = Path(__file__).resolve().parents[2]
_controller_path = str(REPO_ROOT / "controller-server")
if _controller_path not in sys.path:
    sys.path.insert(0, _controller_path)

from app.main import Settings, create_app  # noqa: E402
from app.store import Store  # noqa: E402

ADMIN = "admin-" + "a" * 32
REGISTRATION = "registration-" + "b" * 32
TTL = 30


class SyncASGITransport(httpx.BaseTransport):
    """Runs the controller's ASGI app for the node's sync httpx client."""

    def __init__(self, app):
        self._async = httpx.ASGITransport(app=app)

    def handle_request(self, request):
        async def call():
            response = await self._async.handle_async_request(request)
            await response.aread()
            return response

        response = anyio.run(call)
        return httpx.Response(response.status_code, headers=response.headers,
                              content=response.content, extensions=response.extensions)


class Stack:
    """Real controller app plus a node-side ControllerClient on one clock."""

    def __init__(self, tmp_path, now):
        self.now = now
        self.store = Store(str(tmp_path / "controller.sqlite3"), TTL, clock=lambda: now[0])
        self.app = create_app(Settings(ADMIN, REGISTRATION), self.store)
        self.admin = TestClient(self.app)
        self.node_client = ControllerClient(
            "http://controller.test", transport=SyncASGITransport(self.app))

    def register_node(self):
        response = self.admin.post("/v1/nodes/register", headers={"Authorization": f"Bearer {REGISTRATION}"},
                                   json={"name": "fi-node", "platform": "linux",
                                         "endpoint": "http://node.local:9000",
                                         "available_memory_mb": 2048, "compute_weight": 1})
        assert response.status_code == 201, response.text
        return response.json()

    def create_task(self, **kwargs):
        response = self.admin.post("/v1/tasks", headers={"Authorization": f"Bearer {ADMIN}"},
                                   json={"model_id": "m", "num_layers": 2,
                                         "layer_memory_mb": 1, **kwargs})
        assert response.status_code == 201, response.text
        return response.json()

    def task(self, task_id):
        response = self.admin.get(f"/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {ADMIN}"})
        assert response.status_code == 200, response.text
        return response.json()

    def cancel(self, task_id):
        return self.admin.post(f"/v1/tasks/{task_id}/cancel",
                               headers={"Authorization": f"Bearer {ADMIN}"})


class HoldExecutor(Executor):
    """Blocks until released so a crash can be injected mid-execution."""

    name = "hold"

    def __init__(self):
        self.release = threading.Event()
        self.cancel_observed = threading.Event()

    def run(self, context):
        while not self.release.wait(0.02):
            if context.cancel.is_set():
                self.cancel_observed.set()
                raise ExecutionCancelled(context.cancel_reason)
        if context.cancel.is_set():
            self.cancel_observed.set()
            raise ExecutionCancelled(context.cancel_reason)


class SliceRecorder(Executor):
    name = "slices"

    def __init__(self, slices=200, slice_seconds=0.02):
        self.slices = slices
        self.slice_seconds = slice_seconds
        self.executed = 0
        self.cancel_observed = False

    def run(self, context):
        for _ in range(self.slices):
            if context.cancel.is_set():
                self.cancel_observed = True
                raise ExecutionCancelled(context.cancel_reason)
            time.sleep(self.slice_seconds)
            self.executed += 1


def test_node_restart_mid_execution_reexecutes_once(tmp_path, clock):
    now, stack = clock, Stack(tmp_path, clock)
    state_dir = tmp_path / "state"
    config = make_config(state_dir)
    built = []

    def executor_factory():
        executor = HoldExecutor() if not built else SliceDemoExecutor(slices=1, slice_seconds=0.0)
        built.append(executor)
        return executor

    agent = NodeAgent(config, stack.node_client, clock=lambda: now[0],
                      executor_factory=executor_factory)
    assert agent._join_controller()
    plan = stack.create_task()
    assignment_id = plan["assignments"][0]["id"]
    agent._poll_once()
    assert agent.stats.started == [assignment_id]
    assert stack.task(plan["id"])["status"] == "running"

    # Crash: the process dies mid-execution without shutdown or any report.
    built[0].release.set()
    assert load_settled(state_dir) == {}

    # Restart: same state dir reuses the node identity, no new registration.
    restarted = NodeAgent(config, stack.node_client, clock=lambda: now[0],
                          executor_factory=executor_factory)
    assert restarted._join_controller()
    nodes = stack.admin.get("/v1/nodes", headers={"Authorization": f"Bearer {ADMIN}"}).json()["nodes"]
    assert len(nodes) == 1
    restarted._poll_once()
    restarted._poll_once()

    assert stack.task(plan["id"])["status"] == "completed"
    assert load_settled(state_dir).get(assignment_id) == "completed"
    assert len(built) == 2  # at-least-once: the executor was rebuilt after the crash


def test_duplicate_and_out_of_order_reports_are_idempotent(tmp_path, clock):
    stack = Stack(tmp_path, clock)
    node = stack.register_node()
    plan = stack.create_task()
    assignment_id = plan["assignments"][0]["id"]

    def report(status, error=None):
        return stack.node_client.report(node["node_id"], node["node_token"],
                                        assignment_id, status, error)

    assert stack.task(plan["id"])["status"] == "queued"
    report("running")
    assert stack.task(plan["id"])["status"] == "running"
    report("completed")
    assert stack.task(plan["id"])["status"] == "completed"
    report("completed")  # duplicate terminal acknowledgement stays idempotent
    assert stack.task(plan["id"])["status"] == "completed"
    with pytest.raises(ControllerError) as stale:
        report("running")  # out-of-order stale transition is rejected
    assert stale.value.status_code == 409
    with pytest.raises(ControllerError) as conflict:
        report("failed", "late failure")  # conflicting terminal is rejected
    assert conflict.value.status_code == 409
    final = stack.task(plan["id"])
    assert final["status"] == "completed" and final["error"] is None
    assert final["assignments"][0]["status"] == "completed"


def test_late_report_after_lease_expiry_rejected(tmp_path, clock):
    now, stack = clock, Stack(tmp_path, clock)
    node = stack.register_node()
    plan = stack.create_task()
    assignment_id = plan["assignments"][0]["id"]
    stack.node_client.report(node["node_id"], node["node_token"], assignment_id, "running")

    now[0] += TTL + 1  # no heartbeat inside the TTL: the implicit lease lapses
    expired = stack.task(plan["id"])
    assert expired["status"] == "failed"
    assert expired["error"] == "assigned node heartbeat expired"

    view = stack.node_client.heartbeat(node["node_id"], node["node_token"], 2048, True)
    assert view["status"] == "available"  # reconnect is accepted
    assert stack.task(plan["id"])["status"] == "failed"  # no resurrection

    with pytest.raises(ControllerError) as late:
        stack.node_client.report(node["node_id"], node["node_token"],
                                 assignment_id, "completed")
    assert late.value.status_code == 409  # stale execution result is rejected
    stack.node_client.report(node["node_id"], node["node_token"],
                             assignment_id, "failed")  # same-status ack stays idempotent

    new_task = stack.create_task()
    assert new_task["status"] == "queued"  # resources are free for a new task


def test_cancel_stops_execution_and_new_task_runs_after(tmp_path, clock):
    now, stack = clock, Stack(tmp_path, clock)
    state_dir = tmp_path / "state"
    config = make_config(state_dir)
    factory = FactoryRecorder(SliceRecorder)
    agent = NodeAgent(config, stack.node_client, clock=lambda: now[0],
                      executor_factory=factory.create)
    thread, captured = run_in_thread(agent)
    try:
        assert wait_until(lambda: agent.stats.heartbeats_ok >= 1)
        plan_a = stack.create_task()
        a0 = plan_a["assignments"][0]["id"]
        assert wait_until(lambda: agent.stats.started == [a0])
        assert stack.cancel(plan_a["id"]).json()["status"] == "cancelled"
        assert stack.cancel(plan_a["id"]).status_code == 200  # cancel is idempotent

        assert wait_until(lambda: agent.stats.stopped == [a0], timeout=5)
        recorder = factory.instances[0]
        assert recorder.cancel_observed
        assert recorder.executed < recorder.slices  # stopped before running to the end
        assert load_settled(state_dir).get(a0) == "stopped"

        plan_b = stack.create_task()  # resources were released by the cancel
        b0 = plan_b["assignments"][0]["id"]
        assert wait_until(lambda: stack.task(plan_b["id"])["status"] == "completed", timeout=10)
        assert agent.stats.started == [a0, b0]  # old and new execution never overlap
        assert load_settled(state_dir).get(b0) == "completed"
    finally:
        agent.stop_event.set()
        thread.join(5)
        assert not thread.is_alive()
        assert "error" not in captured
