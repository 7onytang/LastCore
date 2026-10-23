"""SQLite/ASGI + inert worker only: no socket, executor or real workload."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.models import AssignmentReport, NodeRegistration, TaskRequest
from app.store import Store
from lastcore_node_agent.agent import CredentialRejected, NodeAgent, _ActiveRun
from lastcore_node_agent.controller import ControllerClient
from lastcore_node_agent.credentials import NodeCredentials
from lastcore_node_agent.executor import ExecutionContext
from lastcore_node_agent.config import AgentConfig


class InertWorker:
    alive = True

    def is_alive(self):
        return self.alive


@pytest.fixture
def scene(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "fixture.sqlite3"), 30, clock=lambda: now[0])
    def register(name):
        return store.register(NodeRegistration(name=name, platform="fixture",
            endpoint="http://fixture.invalid:9000", available_memory_mb=1, compute_weight=1))
    owner = register("owner")
    api = TestClient(create_app(Settings("a" * 32, "r" * 32), store))
    requests = []
    override = [None]
    def transport(request):
        requests.append(request)
        if override[0] and request.url.path.endswith("/" + old["assignments"][0]["id"]):
            return override[0](request)
        reply = api.request(request.method, request.url.path, headers=dict(request.headers), content=request.content)
        return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)
    client = ControllerClient("http://fixture.invalid", transport=httpx.MockTransport(transport))
    def create():
        now[0] += .01
        return store.create_task(TaskRequest(model_id="status-only-fixture", num_layers=1,
            layer_memory_mb=1, max_nodes=1, timeout_seconds=86400))
    old = create()
    aid = old["assignments"][0]["id"]
    store.report(owner["node_id"], owner["node_token"], aid, AssignmentReport(status="running"))
    def forbidden():
        pytest.fail("fixture must never instantiate a real executor")
    agent = NodeAgent(AgentConfig("http://fixture.invalid", "http://fixture.invalid:9000", 1,
        state_dir=tmp_path / "state", node_name="fixture", platform_name="fixture"), client, clock=lambda: now[0], executor_factory=forbidden)
    agent.credentials = NodeCredentials.from_record(owner)
    agent._last_heartbeat_ok = now[0]
    context = ExecutionContext(task_id=old["id"], assignment_id=aid, model_id="status-only-fixture",
        position=0, layer_start=0, layer_end=1, previous_node=None, next_node=None,
        deadline=old["deadline"], raw={**old["assignments"][0], "request": old["request"]})
    run = _ActiveRun(context=context, worker=InertWorker(), assignment_id=aid, task_id=old["id"])
    agent._active = run
    yield store, owner, old, create, agent, run, client, api, requests, override, register
    client.close()
    api.close()


def test_hidden_cancel_resolves_through_asgi_without_releasing_local_worker(scene):
    store, owner, old, create, agent, run, client, api, requests, _, _ = scene
    store.cancel(old["id"])
    for _ in range(105):
        store.cancel(create()["id"])
    queued = create()
    rows = client.assignments(owner["node_id"], owner["node_token"])
    assert len(rows) == 100 and all(row["id"] != run.assignment_id for row in rows)
    agent._poll_once()
    assert run.context.cancel_reason == "task-terminal"
    assert agent._current_run() is run  # A requested stop is not cleanup confirmation.
    assert not agent.stats.started
    assert store.get_task(queued["id"])["status"] == "queued"
    assert requests[-1].url.path.endswith("/" + run.assignment_id)
    assert client.assignment(owner["node_id"], owner["node_token"], run.assignment_id)["task_status"] == "cancelled"


def test_lookup_ownership_and_auth_do_not_reveal_other_assignments(scene):
    store, owner, old, _, _, run, client, api, _, _, register = scene
    outsider = register("outsider")
    base = f"/v1/nodes/{owner['node_id']}/assignments/"
    headers = {"Authorization": f"Bearer {owner['node_token']}"}
    row = api.get(base + run.assignment_id, headers=headers)
    assert row.status_code == 200
    assert row.json() == store.assignments(owner["node_id"], owner["node_token"])[0]
    for token in (outsider["node_token"], "a" * 32, "r" * 32, "bad"):
        response = api.get(base + run.assignment_id, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "task_id" not in response.json()
    assert api.get(base + run.assignment_id).status_code == 401
    own_path = f"/v1/nodes/{outsider['node_id']}/assignments/"
    outside_headers = {"Authorization": f"Bearer {outsider['node_token']}"}
    denied = api.get(own_path + run.assignment_id, headers=outside_headers)
    missing = api.get(own_path + "missing", headers=outside_headers)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json() == {"detail": "assignment not found"}


@pytest.mark.parametrize("failure", ["unknown", "network", "server", "wrong-row", "unauthorized"])
def test_lookup_failure_does_not_claim_stop_or_start_new_work(scene, failure):
    store, owner, old, create, agent, run, client, api, requests, override, _ = scene
    def reject(request):
        if failure == "network":
            raise httpx.ConnectError("fixture outage", request=request)
        if failure == "wrong-row":
            return httpx.Response(200, json={"id": "other", "node_id": owner["node_id"], "task_status": "cancelled"})
        return httpx.Response({"unknown": 404, "server": 500, "unauthorized": 401}[failure],
                              json={"detail": "assignment not found"})
    override[0] = reject
    if failure == "unauthorized":
        with pytest.raises(CredentialRejected):
            agent._watch_active([], run)
    else:
        agent._watch_active([], run)
        agent._watch_active([], run)
        assert len(requests) == 2  # Unknown ID/errors are retried, not cached as unsupported.
    assert not run.context.cancel.is_set()
    assert agent._current_run() is run and not agent.stats.started
    assert agent._assignment_lookup_supported


@pytest.mark.parametrize("status,detail", [(404, "Not Found"), (405, "Method Not Allowed"), (501, "Not Implemented")])
def test_old_server_fallback_is_explicit_cached_and_still_watches_visible_rows(scene, caplog, status, detail):
    store, owner, old, create, agent, run, client, api, requests, override, _ = scene
    override[0] = lambda request: httpx.Response(status, json={"detail": detail})
    agent._watch_active([], run)
    agent._watch_active([], run)
    assert len(requests) == 1
    assert not run.context.cancel.is_set()
    assert not agent._assignment_lookup_supported
    assert "legacy latest-100" in caplog.text and "deadline" in caplog.text
    store.cancel(old["id"])
    agent._poll_once()
    assert run.context.cancel_reason == "task-terminal"
    assert agent._current_run() is run and not agent.stats.started


def test_visible_active_row_needs_no_lookup_and_wrong_task_cannot_cancel(scene):
    store, owner, old, create, agent, run, client, api, requests, override, _ = scene
    visible = {"id": run.assignment_id, "task_id": run.task_id, "task_status": "running"}
    agent._watch_active([visible], run)
    assert requests == [] and not run.context.cancel.is_set()
    override[0] = lambda request: httpx.Response(200, json={
        "id": run.assignment_id, "node_id": owner["node_id"],
        "task_id": "unrelated", "task_status": "cancelled"})
    agent._watch_active([], run)
    assert not run.context.cancel.is_set()


def test_failed_task_lookup_also_requests_stop(scene):
    store, owner, old, create, agent, run, client, api, requests, override, _ = scene
    store.report(owner["node_id"], owner["node_token"], run.assignment_id,
                 AssignmentReport(status="failed", error="fixture failure"))
    for _ in range(105):
        store.cancel(create()["id"])
    agent._poll_once()
    assert run.context.cancel_reason == "task-terminal"
    assert agent._current_run() is run and not agent.stats.started
