"""Cross-module integration contract: real controller-server app driven by the real node-agent.

Serves the actual FastAPI controller (routing, pydantic validation, SQLite store) on a
loopback port with uvicorn and drives it over real HTTP with the actual node-agent
ControllerClient and NodeAgent loop. Verifies the interface both modules rely on:
registration, heartbeat, assignment polling, running/completed reports, cancel
propagation, terminal-report idempotency, credential rejection mapping, and
heartbeat-TTL expiry.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "node-agent"))

from app.main import Settings, create_app  # noqa: E402
from lastcore_node_agent.agent import NodeAgent, RunStats  # noqa: E402
from lastcore_node_agent.config import AgentConfig  # noqa: E402
from lastcore_node_agent.controller import ControllerClient, ControllerError  # noqa: E402
from lastcore_node_agent.executor import SliceDemoExecutor  # noqa: E402

ADMIN_TOKEN = "admin-token-0123456789abcdef0123456789abcdef"
REGISTRATION_TOKEN = "registration-token-0123456789abcdef0123456789ab"
REGISTRATION_PAYLOAD = {
    "name": "contract-node",
    "platform": "test-platform",
    "endpoint": "http://127.0.0.1:9",
    "available_memory_mb": 1024,
    "compute_weight": 1.0,
}


def wait_until(predicate, timeout: float, interval: float = 0.05, message: str = ""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"condition not met within {timeout}s: {message or predicate}")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def controller_url(tmp_path: Path):
    app = create_app(Settings(admin_token=ADMIN_TOKEN, registration_token=REGISTRATION_TOKEN,
                              database_path=str(tmp_path / "controller.sqlite3"),
                              heartbeat_ttl_seconds=5))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, name="contract-controller", daemon=True)
    thread.start()
    wait_until(lambda: server.started, timeout=10, message="uvicorn server started")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    assert not thread.is_alive(), "controller server did not stop cleanly"


def make_agent_client(base_url: str) -> ControllerClient:
    return ControllerClient(base_url)


def make_admin_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                        timeout=10.0, trust_env=False)


def make_config(base_url: str, state_dir: Path) -> AgentConfig:
    return AgentConfig(controller_url=base_url, endpoint=REGISTRATION_PAYLOAD["endpoint"],
                       available_memory_mb=REGISTRATION_PAYLOAD["available_memory_mb"],
                       compute_weight=REGISTRATION_PAYLOAD["compute_weight"],
                       node_name=REGISTRATION_PAYLOAD["name"], platform_name="test-platform",
                       state_dir=state_dir, registration_token=REGISTRATION_TOKEN,
                       heartbeat_interval=0.2, poll_interval=0.05)


def start_agent(agent: NodeAgent) -> threading.Thread:
    thread = threading.Thread(target=agent.run_forever, name="contract-agent", daemon=True)
    thread.start()
    return thread


def stop_agent(agent: NodeAgent, thread: threading.Thread) -> None:
    agent.shutdown()
    thread.join(5)
    assert not thread.is_alive(), "agent loop did not stop cleanly"


def create_task(admin: httpx.Client, **overrides) -> dict:
    payload = {"model_id": "contract-model", "num_layers": 4, "layer_memory_mb": 1,
               "timeout_seconds": 120}
    payload.update(overrides)
    response = admin.post("/v1/tasks", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def get_task(admin: httpx.Client, task_id: str) -> dict:
    response = admin.get(f"/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_node_available(admin: httpx.Client) -> dict:
    nodes = wait_until(lambda: [n for n in admin.get("/v1/nodes").json()["nodes"]
                                if n["status"] == "available"],
                       timeout=10, message="registered node available")
    assert len(nodes) == 1
    return nodes[0]


def test_full_lifecycle_register_assign_run_complete(tmp_path, controller_url):
    client = make_agent_client(controller_url)
    admin = make_admin_client(controller_url)
    config = make_config(controller_url, tmp_path / "state")
    agent = NodeAgent(config, client, stats=RunStats(),
                      executor_factory=lambda: SliceDemoExecutor(slices=2, slice_seconds=0.02))
    thread = start_agent(agent)
    try:
        node = wait_node_available(admin)
        assert node["name"] == "contract-node"
        assert node["available_memory_mb"] == 1024

        task = create_task(admin)
        assert task["status"] == "queued"
        assert len(task["assignments"]) == 1
        assignment_id = task["assignments"][0]["id"]

        wait_until(lambda: agent.stats.started == [assignment_id], timeout=10,
                   message="agent picked up assignment")
        wait_until(lambda: get_task(admin, task["id"])["status"] == "completed", timeout=15,
                   message="task completed through real report path")
        final = get_task(admin, task["id"])
        assert final["assignments"][0]["status"] == "completed"
        assert final["assignments"][0]["error"] is None
        assert agent.stats.completed == [assignment_id]
        assert agent.stats.failed == []
        assert agent.stats.heartbeats_ok >= 1
        assert agent.stats.heartbeats_failed == 0

        # Terminal reports are idempotent on the controller side.
        credentials = agent.credentials
        assert credentials is not None
        terminal = client.report(credentials.node_id, credentials.node_token,
                                 assignment_id, "completed")
        assert terminal["status"] == "completed"
    finally:
        stop_agent(agent, thread)
        client.close()
        admin.close()


def test_credential_and_report_error_mapping(tmp_path, controller_url):
    client = make_agent_client(controller_url)
    try:
        with pytest.raises(ControllerError) as rejected:
            client.register("wrong-registration-token", REGISTRATION_PAYLOAD)
        assert rejected.value.status_code == 401
        assert rejected.value.detail == "invalid registration credential"

        registered = client.register(REGISTRATION_TOKEN, REGISTRATION_PAYLOAD)
        node_id, node_token = registered["node_id"], registered["node_token"]
        assert registered["heartbeat_ttl_seconds"] == 5

        with pytest.raises(ControllerError) as bad_heartbeat:
            client.heartbeat(node_id, "wrong-node-token", 512, True)
        assert bad_heartbeat.value.status_code == 401
        assert bad_heartbeat.value.detail == "invalid node credential"

        with pytest.raises(ControllerError) as bad_assignments:
            client.assignments("not-a-node-id", node_token)
        assert bad_assignments.value.status_code == 401

        with pytest.raises(ControllerError) as unknown_assignment:
            client.report(node_id, node_token, "missing-assignment-id", "completed")
        assert unknown_assignment.value.status_code == 404
        assert unknown_assignment.value.detail == "assignment not found"

        assert client.heartbeat(node_id, node_token, 512, True)["status"] == "available"
    finally:
        client.close()


def test_cancel_propagates_from_controller_to_agent(tmp_path, controller_url):
    client = make_agent_client(controller_url)
    admin = make_admin_client(controller_url)
    agent = NodeAgent(make_config(controller_url, tmp_path / "state"), client, stats=RunStats(),
                      executor_factory=lambda: SliceDemoExecutor(slices=600, slice_seconds=0.02))
    thread = start_agent(agent)
    try:
        wait_node_available(admin)
        task = create_task(admin)
        assignment_id = task["assignments"][0]["id"]
        wait_until(lambda: agent.stats.started == [assignment_id], timeout=10,
                   message="agent picked up assignment")
        wait_until(lambda: get_task(admin, task["id"])["assignments"][0]["status"] == "running",
                   timeout=10, message="running report reached controller")

        cancelled = admin.post(f"/v1/tasks/{task['id']}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        wait_until(lambda: agent.stats.stopped == [assignment_id], timeout=8,
                   message="agent cancelled locally after observing terminal task status")
        final = get_task(admin, task["id"])
        assert final["assignments"][0]["status"] == "cancelled"
        assert agent.stats.completed == []
        assert agent.stats.failed == []
    finally:
        stop_agent(agent, thread)
        client.close()
        admin.close()


def test_heartbeat_ttl_expiry_fails_task(tmp_path, controller_url):
    client = make_agent_client(controller_url)
    admin = make_admin_client(controller_url)
    agent = NodeAgent(make_config(controller_url, tmp_path / "state"), client, stats=RunStats(),
                      executor_factory=lambda: SliceDemoExecutor(slices=600, slice_seconds=0.02))
    thread = start_agent(agent)
    try:
        wait_node_available(admin)
        task = create_task(admin)
        wait_until(lambda: agent.stats.started, timeout=10, message="assignment started")

        stop_agent(agent, thread)
        wait_until(lambda: get_task(admin, task["id"])["status"] == "failed", timeout=15,
                   message="controller expired the task after heartbeat TTL")
        expired = get_task(admin, task["id"])
        assert expired["error"] == "assigned node heartbeat expired"
        assert expired["assignments"][0]["status"] == "failed"
        assert agent.stats.completed == []
    finally:
        agent.shutdown()
        thread.join(5)
        client.close()
        admin.close()
