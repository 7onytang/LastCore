import math
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.store import Store


ADMIN = "admin-" + "a" * 32
REGISTRATION = "registration-" + "b" * 32


def auth(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "controller.sqlite3"), 30, clock=lambda: now[0])
    return TestClient(create_app(Settings(ADMIN, REGISTRATION), store)), store, now


def register(client, weight=1):
    response = client.post("/v1/nodes/register", headers=auth(REGISTRATION), json={
        "name": "linear-client", "platform": "macOS-arm64", "endpoint": "http://127.0.0.1:9000",
        "available_memory_mb": 4096, "compute_weight": weight,
    })
    assert response.status_code == 201
    return response.json()


def submit(client, node, **changes):
    payload = dict(model_id="lastcore/linear-demo-v1", num_layers=2, layer_memory_mb=1,
                   max_nodes=1, timeout_seconds=120, input_vector=[1, 2, 3, 4], weight_seed=7,
                   target_node_id=node["node_id"])
    payload.update(changes)
    return client.post("/v1/tasks", headers=auth(ADMIN), json=payload)


def report(client, node, assignment, **payload):
    return client.post(f"/v1/nodes/{node['node_id']}/assignments/{assignment['id']}/report",
                       headers=auth(node["node_token"]), json=payload)


def swift_reference(seed, row, values):
    # Matches the existing Swift client's UInt32 wrapping arithmetic.
    def weight(column):
        x = seed ^ ((row * 0x9E3779B1) & 0xFFFFFFFF) ^ ((column * 0x85EBCA77) & 0xFFFFFFFF)
        x ^= x >> 16
        x = (x * 0x7FEB352D) & 0xFFFFFFFF
        x ^= x >> 15
        x = (x * 0x846CA68B) & 0xFFFFFFFF
        x ^= x >> 16
        return x / 0xFFFFFFFF * 2 - 1
    total = weight(len(values))
    for column, value in enumerate(values):
        total += weight(column) * value
    return math.tanh(total)


def test_exact_mac_target_and_persistent_real_values(setup):
    client, store, now = setup
    target = register(client)
    other = register(client, weight=1000)
    response = submit(client, target)
    assert response.status_code == 201, response.text
    task = response.json()
    assert len(task["assignments"]) == 1
    assignment = task["assignments"][0]
    assert assignment["node_id"] == target["node_id"]
    assert client.get(f"/v1/nodes/{other['node_id']}/assignments", headers=auth(other["node_token"])).json() == {"assignments": []}
    polled = client.get(f"/v1/nodes/{target['node_id']}/assignments", headers=auth(target["node_token"])).json()["assignments"][0]
    assert polled["request"]["input_vector"] == [1, 2, 3, 4]
    assert polled["request"]["weight_seed"] == 7
    assert report(client, target, assignment, status="running").status_code == 200
    values = [swift_reference(7, row, [1, 2, 3, 4]) for row in range(2)]
    completed = report(client, target, assignment, status="completed", result=values)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "completed"
    assert completed.json()["result"] == values
    assert completed.json()["assignments"][0]["result"] == values
    reopened = Store(store.path, 30, clock=lambda: now[0])
    assert reopened.get_task(task["id"])["result"] == values
    assert reopened.assignments(target["node_id"], target["node_token"])[0]["result"] == values
    assert client.get("/v1/tasks", headers=auth(target["node_token"])).status_code == 401
    assert report(client, other, assignment, status="completed", result=values).status_code == 404


@pytest.mark.parametrize("state", ["offline", "draining", "busy", "missing", "no_capacity"])
def test_unavailable_target_never_falls_back(setup, state):
    client, _, now = setup
    target = register(client)
    if state == "offline":
        now[0] += 31
    elif state in ("draining", "no_capacity"):
        response = client.post(f"/v1/nodes/{target['node_id']}/heartbeat", headers=auth(target["node_token"]),
                               json={"available_memory_mb": 0 if state == "no_capacity" else 4096,
                                     "accepting_tasks": state != "draining"})
        assert response.status_code == 200
    elif state == "busy":
        assert submit(client, target).status_code == 201
    elif state == "missing":
        target = {"node_id": "00000000-0000-0000-0000-000000000000"}
    register(client, weight=1000)
    assert submit(client, target).status_code == 409


@pytest.mark.parametrize("changes", [
    {"input_vector": None}, {"input_vector": []}, {"input_vector": [1] * 257},
    {"input_vector": [1e20]}, {"weight_seed": None}, {"weight_seed": -1},
    {"target_node_id": None}, {"target_node_id": "not-a-node"}, {"num_layers": 4097},
    {"max_nodes": 2}, {"model_id": "lastcore/tiny-cpu"},
])
def test_invalid_linear_contract_rejected_before_scheduling(setup, changes):
    client, _, _ = setup
    target = register(client)
    assert submit(client, target, **changes).status_code == 422
    assert client.get("/v1/tasks", headers=auth(ADMIN)).json()["tasks"] == []


def test_results_are_required_shaped_finite_and_immutable(setup):
    client, _, _ = setup
    node = register(client)
    task = submit(client, node).json()
    assignment = task["assignments"][0]
    for payload in [{"status": "completed"}, {"status": "completed", "result": [0.1]},
                    {"status": "completed", "result": [0.1, 2]},
                    {"status": "running", "result": [0.1, 0.2]}]:
        assert report(client, node, assignment, **payload).status_code == 422
    url = f"/v1/nodes/{node['node_id']}/assignments/{assignment['id']}/report"
    response = client.post(url, headers=auth(node["node_token"]), content='{"status":"completed","result":[NaN,0]}')
    assert response.status_code == 422
    assert report(client, node, assignment, status="completed", result=[0.1, 0.2]).status_code == 200
    assert report(client, node, assignment, status="completed", result=[0.1, 0.2]).status_code == 200
    assert report(client, node, assignment, status="completed", result=[0.2, 0.1]).status_code == 409
    assert client.get(f"/v1/tasks/{task['id']}", headers=auth(ADMIN)).json()["result"] == [0.1, 0.2]


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_late_result_cannot_revive_task(setup, terminal):
    client, _, now = setup
    node = register(client)
    task = submit(client, node).json()
    if terminal == "cancelled":
        assert client.post(f"/v1/tasks/{task['id']}/cancel", headers=auth(ADMIN)).status_code == 200
    else:
        now[0] += 121
    assert report(client, node, task["assignments"][0], status="completed", result=[0.1, 0.2]).status_code == 409
    assert client.get(f"/v1/tasks/{task['id']}", headers=auth(ADMIN)).json()["result"] is None


def test_legacy_schema_and_insert_stay_compatible(setup):
    client, store, _ = setup
    node = register(client)
    task = client.post("/v1/tasks", headers=auth(ADMIN), json={
        "model_id": "lastcore/tiny-cpu", "num_layers": 2, "layer_memory_mb": 1,
    }).json()
    assignment = task["assignments"][0]
    assert report(client, node, assignment, status="completed", result=[0, 0]).status_code == 422
    assert report(client, node, assignment, status="completed").json()["status"] == "completed"
    with sqlite3.connect(store.path) as db:
        assert len(db.execute("PRAGMA table_info(assignments)").fetchall()) == 8
        db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,NULL)",
                   ("old-insert", task["id"], node["node_id"], 1, 0, 1, "completed"))
    assert store.get_task(task["id"])["result"] is None
