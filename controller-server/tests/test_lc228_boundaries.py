"""LC-228: controller missing-boundary minimal regression (P0/P1/P2).

Scope: only mirrors LC-223 already-located gaps; no app source changes.
"""
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.store import Store


ADMIN = "admin-" + "a" * 32
REGISTRATION = "registration-" + "b" * 32


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _setup(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "lc228.sqlite3"), 30, clock=lambda: now[0])
    client = TestClient(create_app(Settings(ADMIN, REGISTRATION), store))
    return client, store, now


def _register(client, **overrides):
    body = {
        "name": "lc228-node", "platform": "linux", "endpoint": "http://node.local:9000",
        "available_memory_mb": 2048, "compute_weight": 1,
    }
    body.update(overrides)
    response = client.post("/v1/nodes/register", headers=auth(REGISTRATION), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _task(client, layers=4):
    response = client.post("/v1/tasks", headers=auth(ADMIN), json={
        "model_id": "qwen-test", "num_layers": layers, "layer_memory_mb": 512,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _report(client, node, assignment_id, status, error=None):
    body = {"status": status}
    if error is not None:
        body["error"] = error
    return client.post(
        f"/v1/nodes/{node['node_id']}/assignments/{assignment_id}/report",
        headers=auth(node["node_token"]), json=body)


def test_report_terminal_rollback_rejected_and_error_passthrough(tmp_path):
    """P0: completed->running must 409; failed error must surface, non-failed error dropped."""
    client, _, _ = _setup(tmp_path)
    node = _register(client)
    plan = _task(client)
    assignment = plan["assignments"][0]
    assert _report(client, node, assignment["id"], "running").status_code == 200
    assert _report(client, node, assignment["id"], "completed").status_code == 200
    rollback = _report(client, node, assignment["id"], "running")
    assert rollback.status_code == 409

    plan2 = _task(client)
    failed = _report(client, node, plan2["assignments"][0]["id"], "failed", error="boom")
    assert failed.status_code == 200
    view = client.get(f"/v1/tasks/{plan2['id']}", headers=auth(ADMIN)).json()
    assert view["status"] == "failed"
    assert view["error"] == "boom"

    plan3 = _task(client)
    assert _report(client, node, plan3["assignments"][0]["id"], "running", error="noise").status_code == 200
    view3 = client.get(f"/v1/tasks/{plan3['id']}", headers=auth(ADMIN)).json()
    assert view3["assignments"][0]["error"] is None


def test_unknown_ids_return_404(tmp_path):
    """P1: unknown task GET/cancel and bogus own-node assignment report must 404."""
    client, _, _ = _setup(tmp_path)
    node = _register(client)
    assert client.get("/v1/tasks/does-not-exist", headers=auth(ADMIN)).status_code == 404
    assert client.post("/v1/tasks/does-not-exist/cancel", headers=auth(ADMIN)).status_code == 404
    bogus = _report(client, node, "bogus-assignment-id", "running")
    assert bogus.status_code == 404


def test_registration_heartbeat_validation_and_node_token_forbidden(tmp_path):
    """P2: bad register/heartbeat payloads 422; node token cannot use admin task endpoints (401)."""
    client, _, _ = _setup(tmp_path)
    base = {
        "name": "lc228-node", "platform": "linux", "endpoint": "http://node.local:9000",
        "available_memory_mb": 2048, "compute_weight": 1,
    }
    bad_bodies = [
        {**base, "name": ""},
        {**base, "endpoint": "not-a-url"},
        {**base, "available_memory_mb": 0},
        {**base, "compute_weight": 0},
    ]
    for body in bad_bodies:
        response = client.post("/v1/nodes/register", headers=auth(REGISTRATION), json=body)
        assert response.status_code == 422, body
    node = _register(client)
    hb = client.post(f"/v1/nodes/{node['node_id']}/heartbeat",
                     headers=auth(node["node_token"]), json={"available_memory_mb": -1})
    assert hb.status_code == 422
    assert client.post("/v1/tasks", headers=auth(node["node_token"]), json={
        "model_id": "qwen-test", "num_layers": 1, "layer_memory_mb": 512}).status_code == 401
    assert client.get("/v1/tasks", headers=auth(node["node_token"])).status_code == 401
