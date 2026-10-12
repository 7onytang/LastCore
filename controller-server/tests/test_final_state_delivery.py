"""LC-7 final-state delivery beyond the assignment poll window.

The LC-7 acceptance says a node must still receive the terminal states it has
to process when more than 100 tasks exist. Today ``Store.assignments`` caps
its per-node history at the 100 newest tasks (``ORDER BY created_at DESC
LIMIT 100``), so a terminal command that scrolls past that window is never
observed by the node and its settle journal never records it.

Two tests share one 210-task history:

* ``test_poll_window_caps_at_100_newest_tasks`` characterizes the current
  window behavior; it passes on the LC-7 baseline (672b4f7) and should be
  retired together with the LIMIT once reliable delivery lands.
* ``test_cancelled_terminal_delivered_beyond_100_tasks`` encodes the LC-7
  target behavior; it is xfail on the baseline until the lease/terminal-ack
  implementation (proposal in docs/lc7-lease-epoch-state-transitions.md,
  pending LC-5 contract approval) closes the gap.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.store import Store

ADMIN = "admin-" + "a" * 32
REGISTRATION = "registration-" + "b" * 32
WINDOW = 100
TOTAL = 2 * WINDOW + 10  # 210 tasks: window holds the newest 100 only


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def build_history(tmp_path, now):
    """One node, 210 sequential tasks; returns (client, node, cancelled_id).

    Tasks 1..100 run to completion. Task 101 is cancelled by the operator
    while the node has never polled, so its terminal state is unobserved.
    Tasks 102..210 run to completion, pushing task 101 outside the newest-100
    window. Clock steps of 0.1s keep created_at ordering deterministic while
    staying inside the 30s heartbeat TTL.
    """
    store = Store(str(tmp_path / "controller.sqlite3"), 30, clock=lambda: now[0])
    client = TestClient(create_app(Settings(ADMIN, REGISTRATION), store))
    node = client.post("/v1/nodes/register", headers=auth(REGISTRATION), json={
        "name": "window-node", "platform": "linux", "endpoint": "http://node.local:9000",
        "available_memory_mb": 1_000_000, "compute_weight": 1,
    }).json()

    def create():
        response = client.post("/v1/tasks", headers=auth(ADMIN), json={
            "model_id": "m", "num_layers": 1, "layer_memory_mb": 1,
        })
        assert response.status_code == 201, response.text
        now[0] += 0.1
        return response.json()

    def run_to_completion(plan):
        assignment = plan["assignments"][0]
        assert client.post(
            f"/v1/nodes/{node['node_id']}/assignments/{assignment['id']}/report",
            headers=auth(node["node_token"]), json={"status": "running"}).status_code == 200
        assert client.post(
            f"/v1/nodes/{node['node_id']}/assignments/{assignment['id']}/report",
            headers=auth(node["node_token"]), json={"status": "completed"}).status_code == 200

    for _ in range(100):
        run_to_completion(create())
    cancelled = create()
    cancel_url = f"/v1/tasks/{cancelled['id']}/cancel"
    assert client.post(cancel_url, headers=auth(ADMIN)).json()["status"] == "cancelled"
    for _ in range(TOTAL - 101):
        run_to_completion(create())
    return client, node, cancelled["id"]


def visible_task_ids(client, node):
    poll = client.get(f"/v1/nodes/{node['node_id']}/assignments",
                      headers=auth(node["node_token"])).json()["assignments"]
    return {row["task_id"] for row in poll}, poll


def test_poll_window_caps_at_100_newest_tasks(tmp_path):
    now = [1000.0]
    client, node, cancelled_id = build_history(tmp_path, now)
    ids, rows = visible_task_ids(client, node)
    assert len(rows) == WINDOW
    assert len(ids) == WINDOW
    # The unobserved cancelled task is the 101st newest and is not delivered.
    assert cancelled_id not in ids
    # Everything inside the window is one of the 100 newest tasks.
    for row in rows:
        assert row["task_status"] in ("queued", "running", "completed", "failed", "cancelled")


@pytest.mark.xfail(
    reason="LC-7 目标行为：终态投递不受 100 条轮询窗口限制（租约/终态确认实现前，"
           "超出窗口的未观察终态会丢失）。见 docs/lc7-lease-epoch-state-transitions.md",
    strict=False)
def test_cancelled_terminal_delivered_beyond_100_tasks(tmp_path):
    now = [1000.0]
    client, node, cancelled_id = build_history(tmp_path, now)
    for _ in range(3):  # repeated polls must keep delivering until observed
        ids, rows = visible_task_ids(client, node)
        assert cancelled_id in ids
        row = next(r for r in rows if r["task_id"] == cancelled_id)
        assert row["task_status"] == "cancelled"
        assert row["status"] == "cancelled"
