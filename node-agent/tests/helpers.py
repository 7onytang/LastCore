import json
import threading
import time

import httpx
import pytest

from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.controller import ControllerClient

REGISTRATION_TOKEN = "registration-" + "b" * 32


class FakeController:
    def __init__(self, clock):
        self.clock = clock
        self.registration_token = REGISTRATION_TOKEN
        self.nodes = {}
        self.register_calls = 0
        self.tasks = {}
        self.created_counter = 0
        self.reports = []
        self.heartbeats = []
        self.heartbeat_error = None
        self.assignments_error = None
        self.register_error = None
        self.report_error = None

    def add_task(self, node_id, layers=(0, 4), model_id="m", deadline_offset=60.0, status="queued",
                 deadline=None):
        self.created_counter += 1
        task_id = f"task-{self.created_counter}"
        assignment_id = f"asg-{self.created_counter}"
        self.tasks[task_id] = {
            "id": task_id,
            "created": self.created_counter,
            "status": status,
            "deadline": deadline if deadline is not None else self.clock[0] + deadline_offset,
            "request": {"model_id": model_id, "num_layers": layers[1], "layer_memory_mb": 512},
            "assignments": {assignment_id: {
                "id": assignment_id, "task_id": task_id, "node_id": node_id, "position": 0,
                "layer_start": layers[0], "layer_end": layers[1],
                "previous_node": None, "next_node": None, "status": "pending", "error": None,
            }},
        }
        return task_id, assignment_id

    def set_task_status(self, task_id, status):
        self.tasks[task_id]["status"] = status

    def _node_from_path(self, request):
        node_id = request.url.path.split("/")[3]
        header = request.headers.get("Authorization", "")
        node = self.nodes.get(node_id)
        if node is None or header != f"Bearer {node['token']}":
            return None, node_id
        return node, node_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/v1/nodes/register":
            if self.register_error is not None:
                raise self.register_error
            if request.headers.get("Authorization", "") != f"Bearer {self.registration_token}":
                return httpx.Response(401, json={"detail": "invalid registration credential"})
            self.register_calls += 1
            node_id = f"node-{self.register_calls}"
            token = f"token-{self.register_calls}-" + "c" * 24
            self.nodes[node_id] = {"token": token, "last_seen": self.clock[0]}
            return httpx.Response(201, json={"node_id": node_id, "node_token": token,
                                             "heartbeat_ttl_seconds": 30})
        if method == "POST" and path.startswith("/v1/nodes/") and path.endswith("/heartbeat"):
            node, node_id = self._node_from_path(request)
            if node is None:
                return httpx.Response(401, json={"detail": "invalid node credential"})
            if self.heartbeat_error is not None:
                raise self.heartbeat_error
            node["last_seen"] = self.clock[0]
            self.heartbeats.append((node_id, self.clock[0]))
            return httpx.Response(200, json={"status": "available",
                                            "accepting_tasks": json.loads(request.content)["accepting_tasks"]})
        if method == "GET" and path.startswith("/v1/nodes/") and path.endswith("/assignments"):
            node, node_id = self._node_from_path(request)
            if node is None:
                return httpx.Response(401, json={"detail": "invalid node credential"})
            if self.assignments_error is not None:
                raise self.assignments_error
            return httpx.Response(200, json={"assignments": self._rows(node_id)})
        if method == "POST" and path.endswith("/report"):
            node, node_id = self._node_from_path(request)
            if node is None:
                return httpx.Response(401, json={"detail": "invalid node credential"})
            if self.report_error is not None:
                raise self.report_error
            parts = path.split("/")
            assignment_id = parts[5]
            task = next((t for t in self.tasks.values() if assignment_id in t["assignments"]), None)
            if task is None:
                return httpx.Response(404, json={"detail": "assignment not found"})
            row = task["assignments"][assignment_id]
            if row["node_id"] != node_id:
                return httpx.Response(404, json={"detail": "assignment not found"})
            if task["status"] not in ("queued", "running") or row["status"] in ("completed", "failed"):
                return httpx.Response(409, json={"detail": "assignment is terminal"})
            body = json.loads(request.content)
            status = body.get("status")
            self.reports.append((assignment_id, status, body.get("error")))
            row["status"] = status
            if status == "failed":
                task["status"] = "failed"
            elif status == "completed" and all(r["status"] == "completed"
                                               for r in task["assignments"].values()):
                task["status"] = "completed"
            else:
                task["status"] = "running"
            return httpx.Response(200, json={"status": task["status"]})
        return httpx.Response(404, json={"detail": "not found"})

    def _rows(self, node_id):
        rows = []
        for task in sorted(self.tasks.values(), key=lambda t: -t["created"]):
            for row in task["assignments"].values():
                if row["node_id"] == node_id:
                    rows.append({**row, "task_status": task["status"],
                                 "request": task["request"], "deadline": task["deadline"]})
        return rows


class FactoryRecorder:
    def __init__(self, builder):
        self.builder = builder
        self.instances = []

    def create(self):
        instance = self.builder()
        self.instances.append(instance)
        return instance


def make_config(state_dir, **overrides):
    defaults = dict(
        controller_url="http://controller.test",
        endpoint="http://node.test:9000",
        available_memory_mb=2048,
        compute_weight=1.0,
        node_name="test-node",
        platform_name="test",
        state_dir=state_dir,
        heartbeat_interval=0.02,
        poll_interval=0.01,
        accepting_tasks=True,
        executor="demo",
        registration_token=REGISTRATION_TOKEN,
        report_retries=3,
        retry_backoff=0.01,
        stop_grace=3.0,
        request_timeout=5.0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def run_in_thread(agent):
    captured = {}

    def target():
        try:
            agent.run_forever()
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, captured


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def fake(clock):
    return FakeController(clock)


@pytest.fixture
def client(fake):
    client = ControllerClient("http://controller.test",
                              transport=httpx.MockTransport(fake.handler))
    yield client
    client.close()
