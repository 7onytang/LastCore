from concurrent.futures import ThreadPoolExecutor
import random

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.models import TaskRequest
from app.scheduler import allocate
from app.store import Store


ADMIN = "admin-" + "a" * 32
REGISTRATION = "registration-" + "b" * 32


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def assert_bearer_challenge(response):
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.fixture
def setup(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "controller.sqlite3"), 30, clock=lambda: now[0])
    client = TestClient(create_app(Settings(ADMIN, REGISTRATION), store))
    return client, store, now


def register(client, memory=2048, weight=1):
    response = client.post("/v1/nodes/register", headers=auth(REGISTRATION), json={
        "name": "test-node", "platform": "linux", "endpoint": "http://node.local:9000",
        "available_memory_mb": memory, "compute_weight": weight,
    })
    assert response.status_code == 201, response.text
    return response.json()


def task(client, layers=8, **kwargs):
    return client.post("/v1/tasks", headers=auth(ADMIN), json={
        "model_id": "qwen-test", "num_layers": layers, "layer_memory_mb": 512, **kwargs,
    })


def heartbeat(client, node, **kwargs):
    return client.post(f"/v1/nodes/{node['node_id']}/heartbeat", headers=auth(node["node_token"]),
                       json={"available_memory_mb": 2048, **kwargs})


def report(client, node, assignment, status):
    return client.post(f"/v1/nodes/{node['node_id']}/assignments/{assignment['id']}/report",
                       headers=auth(node["node_token"]), json={"status": status})


def test_two_node_plan_and_completion(setup):
    client, _, _ = setup
    nodes = [register(client), register(client)]
    result = task(client)
    assert result.status_code == 201
    plan = result.json()
    assignments = plan["assignments"]
    assert [(a["layer_start"], a["layer_end"]) for a in assignments] == [(0, 4), (4, 8)]
    assert assignments[0]["previous_node"] is None
    assert assignments[0]["next_node"]["node_id"] == assignments[1]["node_id"]
    assert assignments[1]["previous_node"]["id"] == assignments[0]["id"]
    assert assignments[1]["next_node"] is None
    assert task(client).status_code == 409
    node_map = {n["node_id"]: n for n in nodes}
    for i, a in enumerate(assignments):
        node = node_map[a["node_id"]]
        poll = client.get(f"/v1/nodes/{node['node_id']}/assignments", headers=auth(node["node_token"]))
        assert poll.json()["assignments"][0]["request"]["model_id"] == "qwen-test"
        assert report(client, node, a, "running").status_code == 200
        completed = report(client, node, a, "completed")
        assert completed.json()["status"] == ("running" if i == 0 else "completed")
        assert report(client, node, a, "completed").status_code == 200
        if i == 0:
            assert task(client, layers=1).status_code == 409  # Whole pipeline stays reserved.
    assert task(client).status_code == 201


def test_credentials_and_ownership(setup):
    client, _, _ = setup
    a, b = register(client), register(client)
    assert_bearer_challenge(client.get("/v1/nodes"))
    assert_bearer_challenge(client.get("/v1/nodes", headers={"Authorization": "Basic dXNlcjpwYXNz"}))
    assert_bearer_challenge(client.get("/v1/tasks", headers=auth(REGISTRATION)))
    assert_bearer_challenge(client.post("/v1/nodes/register", headers=auth(ADMIN), json={
        "name": "invalid-registration-credential", "platform": "linux",
        "endpoint": "http://node.local:9000", "available_memory_mb": 2048,
    }))
    assert_bearer_challenge(client.get("/v1/nodes", headers=auth(a["node_token"])))
    assert_bearer_challenge(client.post(f"/v1/nodes/{a['node_id']}/heartbeat", headers=auth("invalid-node-token"),
                                        json={"available_memory_mb": 100}))
    assert_bearer_challenge(client.post(f"/v1/nodes/{a['node_id']}/heartbeat", headers=auth(b["node_token"]),
                                        json={"available_memory_mb": 100}))
    view = client.get("/v1/nodes", headers=auth(ADMIN)).json()
    assert "token_hash" not in str(view) and a["node_token"] not in str(view)
    assignments = task(client).json()["assignments"]
    foreign = next(s for s in assignments if s["node_id"] == b["node_id"])
    assert report(client, a, foreign, "completed").status_code == 404


def test_expired_node_fails_entire_task_and_can_reconnect(setup):
    client, _, now = setup
    nodes = [register(client), register(client)]
    plan = task(client).json()
    now[0] += 20
    heartbeat(client, nodes[0])
    now[0] += 11
    result = client.get(f"/v1/tasks/{plan['id']}", headers=auth(ADMIN)).json()
    assert result["status"] == "failed"
    assert all(a["status"] == "failed" for a in result["assignments"])
    statuses = {n["id"]: n["status"] for n in client.get("/v1/nodes", headers=auth(ADMIN)).json()["nodes"]}
    assert statuses[nodes[0]["node_id"]] == "available"
    assert statuses[nodes[1]["node_id"]] == "offline"
    assert heartbeat(client, nodes[1]).json()["status"] == "available"
    assert task(client).status_code == 201
    old = next(a for a in plan["assignments"] if a["node_id"] == nodes[1]["node_id"])
    assert report(client, nodes[1], old, "completed").status_code == 409


def test_deadline_and_cancellation(setup):
    client, _, now = setup
    node = register(client)
    plan = task(client, layers=4, timeout_seconds=10).json()
    now[0] += 10
    expired = client.get(f"/v1/tasks/{plan['id']}", headers=auth(ADMIN)).json()
    assert expired["status"] == "failed"
    assert expired["error"] == "task deadline exceeded"
    plan = task(client, layers=4).json()
    url = f"/v1/tasks/{plan['id']}/cancel"
    assert client.post(url, headers=auth(ADMIN)).json()["status"] == "cancelled"
    assert client.post(url, headers=auth(ADMIN)).status_code == 200
    assert report(client, node, plan["assignments"][0], "running").status_code == 409
    commands = client.get(f"/v1/nodes/{node['node_id']}/assignments", headers=auth(node["node_token"])).json()
    assert any(a["task_status"] == "cancelled" for a in commands["assignments"])


def test_failure_releases_all_reservations(setup):
    client, _, _ = setup
    nodes = [register(client), register(client)]
    plan = task(client).json()
    a = plan["assignments"][0]
    node = next(n for n in nodes if n["node_id"] == a["node_id"])
    result = report(client, node, a, "failed").json()
    assert result["status"] == "failed"
    assert all(a["status"] == "failed" for a in result["assignments"])
    assert task(client).status_code == 201


def test_draining_nodes_and_no_capacity(setup):
    client, _, _ = setup
    assert task(client).status_code == 409
    node = register(client)
    assert heartbeat(client, node, accepting_tasks=False).json()["status"] == "draining"
    assert task(client, layers=1).status_code == 409
    heartbeat(client, node)
    assert task(client, layers=5).status_code == 409
    assert client.get("/v1/tasks", headers=auth(ADMIN)).json()["tasks"] == []
    assert task(client, layers=4).status_code == 201


def test_restart_preserves_credentials_and_reservations(setup):
    client, store, now = setup
    node = register(client)
    plan = task(client, layers=4).json()
    reopened = Store(store.path, 30, clock=lambda: now[0])
    other = TestClient(create_app(Settings(ADMIN, REGISTRATION), reopened))
    assert heartbeat(other, node).json()["status"] == "busy"
    assert task(other, layers=1).status_code == 409
    assert other.get(f"/v1/tasks/{plan['id']}", headers=auth(ADMIN)).json() == plan


def test_concurrent_task_creation_has_single_winner(setup):
    client, store, _ = setup
    register(client)
    request = TaskRequest(model_id="test", num_layers=4, layer_memory_mb=512)

    def create(_):
        try:
            store.create_task(request)
            return 201
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(8)))
    assert results.count(201) == 1
    assert results.count(409) == 7


@pytest.mark.parametrize("field,value", [("num_layers", 0), ("layer_memory_mb", -1),
                                        ("max_nodes", 0), ("timeout_seconds", 1), ("unknown", 1)])
def test_invalid_task_rejected(setup, field, value):
    client, _, _ = setup
    assert task(client, **{field: value}).status_code == 422


def test_startup_rejects_unsafe_config():
    with pytest.raises(ValueError):
        create_app(Settings("", ""))
    with pytest.raises(ValueError):
        create_app(Settings(ADMIN, ADMIN))


def test_scheduler_respects_capacity_and_weights():
    nodes = [{"id": "a", "endpoint": "http://a", "available_memory_mb": 100, "compute_weight": 3},
             {"id": "b", "endpoint": "http://b", "available_memory_mb": 100, "compute_weight": 1}]
    plan = allocate(nodes, 8, 10, 2)
    assert [(a["layer_start"], a["layer_end"]) for a in plan] == [(0, 6), (6, 8)]
    nodes[0]["available_memory_mb"] = 10
    assert allocate(nodes, 8, 10, 1)[0]["node_id"] == "b"


def test_scheduler_randomized_coverage():
    rng = random.Random(42)
    for _ in range(100):
        nodes = [{"id": str(i), "endpoint": f"http://n{i}", "available_memory_mb": rng.randint(1, 128),
                  "compute_weight": rng.uniform(0.1, 100)} for i in range(rng.randint(1, 12))]
        max_nodes = rng.randint(1, len(nodes))
        size = rng.randint(1, 16)
        capacity = sum(sorted([n["available_memory_mb"] // size for n in nodes], reverse=True)[:max_nodes])
        if not capacity:
            with pytest.raises(ValueError):
                allocate(nodes, 1, size, max_nodes)
            continue
        layers = rng.randint(1, capacity)
        plan = allocate(nodes, layers, size, max_nodes)
        assert len(plan) <= max_nodes
        cursor = 0
        node_map = {n["id"]: n for n in nodes}
        for shard in plan:
            assert shard["layer_start"] == cursor
            assert shard["layer_end"] > cursor
            count = shard["layer_end"] - cursor
            assert count * size <= node_map[shard["node_id"]]["available_memory_mb"]
            cursor = shard["layer_end"]
        assert cursor == layers


def reference_greedy_allocate(nodes, num_layers, layer_memory_mb, max_nodes):
    """Pre-optimization O(num_layers * max_nodes) scan, kept as the oracle."""
    candidates = sorted(
        [n for n in nodes if n["available_memory_mb"] >= layer_memory_mb],
        key=lambda n: (-n["compute_weight"], -n["available_memory_mb"], n["id"]),
    )
    selected = candidates[:max_nodes]
    capacity = lambda n: n["available_memory_mb"] // layer_memory_mb
    if sum(map(capacity, selected)) < num_layers:
        selected = sorted(candidates, key=lambda n: (-capacity(n), -n["compute_weight"], n["id"]))[:max_nodes]
    if sum(map(capacity, selected)) < num_layers:
        raise ValueError("insufficient available node memory within max_nodes")
    counts = [0] * len(selected)
    for _ in range(num_layers):
        index = min(
            (i for i, n in enumerate(selected) if counts[i] < capacity(n)),
            key=lambda i: ((counts[i] + 1) / selected[i]["compute_weight"], i),
        )
        counts[index] += 1
    start = 0
    result = []
    for node, count in zip(selected, counts):
        if count:
            result.append({"node_id": node["id"], "endpoint": node["endpoint"],
                           "layer_start": start, "layer_end": start + count})
            start += count
    return result


def test_scheduler_heap_matches_reference_greedy():
    rng = random.Random(1337)
    checked = 0
    for _ in range(300):
        nodes = [{"id": f"n{i}-{rng.random():.6f}", "endpoint": f"http://n{i}",
                  "available_memory_mb": rng.randint(1, 4096),
                  "compute_weight": rng.choice([rng.uniform(0.01, 1000), float(rng.randint(1, 8)),
                                                1 / rng.randint(1, 8)])}
                 for i in range(rng.randint(1, 16))]
        max_nodes = rng.randint(1, len(nodes))
        size = rng.randint(1, 512)
        capacity = sum(sorted([n["available_memory_mb"] // size for n in nodes], reverse=True)[:max_nodes])
        layers = rng.randint(1, capacity) if capacity else 1
        reference = reference_greedy_allocate(nodes, layers, size, max_nodes) \
            if capacity else None
        if capacity:
            assert allocate(nodes, layers, size, max_nodes) == reference
            checked += 1
        else:
            with pytest.raises(ValueError):
                allocate(nodes, layers, size, max_nodes)
    assert checked > 200


def test_scheduler_extreme_input_100k_layers_x_128mb():
    import time

    nodes = [{"id": f"node-{i:03d}", "endpoint": f"http://node-{i:03d}:9000",
              "available_memory_mb": 10_000_000, "compute_weight": 1.0 + (i % 7) * 0.5}
             for i in range(128)]
    started = time.perf_counter()
    plan = allocate(nodes, 100_000, 128, 128)
    elapsed = time.perf_counter() - started
    assert len(plan) == 128
    cursor = 0
    for shard in plan:
        assert shard["layer_start"] == cursor
        assert 0 < shard["layer_end"] - shard["layer_start"] <= 10_000_000 // 128
        cursor = shard["layer_end"]
    assert cursor == 100_000
    assert elapsed < 1.0, f"100k x 128 allocation took {elapsed:.3f}s (budget 1.0s)"
