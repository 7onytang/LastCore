"""Public view contracts across batched task and node reads."""
import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException

from app.models import AssignmentReport, Heartbeat, NodeRegistration, TaskRequest
from app.store import Store


def test_batched_history_preserves_limit_order_peers_and_node_isolation(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "history.sqlite3"), 30, clock=lambda: now[0])
    nodes = [store.register(NodeRegistration(
        name=f"node-{i}", platform="test", endpoint=f"http://node-{i}:9000",
        available_memory_mb=1, compute_weight=1,
    )) for i in range(3)]
    outsider = store.register(NodeRegistration(
        name="draining", platform="test", endpoint="http://draining:9000",
        available_memory_mb=1, compute_weight=1, accepting_tasks=False,
    ))
    plans = []
    for i in range(105):
        now[0] += 0.01
        plan = store.create_task(TaskRequest(
            model_id=f"model-{i}", num_layers=3, layer_memory_mb=1, max_nodes=3,
        ))
        plans.append(store.cancel(plan["id"]))

    # Views must use current endpoints, even for a historic assignment.
    changed_id = nodes[0]["node_id"]
    endpoint = "http://reconnected:9100"
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE nodes SET endpoint=? WHERE id=?", (endpoint, changed_id))
    expected = list(reversed(plans[-100:]))
    for plan in expected:
        for assignment in plan["assignments"]:
            if assignment["node_id"] == changed_id:
                assignment["endpoint"] = endpoint
            for field in ("previous_node", "next_node"):
                if assignment[field] and assignment[field]["node_id"] == changed_id:
                    assignment[field]["endpoint"] = endpoint
    assert store.tasks() == expected
    for node in nodes:
        commands = store.assignments(node["node_id"], node["node_token"])
        assert len(commands) == 100
        for command, plan in zip(commands, expected):
            own = next(a for a in plan["assignments"] if a["node_id"] == node["node_id"])
            assert command == {**own, "task_status": "cancelled",
                               "request": plan["request"], "deadline": plan["deadline"]}
            assert command["status"] == "cancelled"
    assert store.assignments(outsider["node_id"], outsider["node_token"]) == []
    with pytest.raises(HTTPException) as error:
        store.assignments(nodes[0]["node_id"], nodes[1]["node_token"])
    assert error.value.status_code == 401
    assert all("token_hash" not in row for row in store.nodes())


def test_bulk_busy_reserves_completed_shards_and_releases_terminal_tasks(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "busy.sqlite3"), 30, clock=lambda: now[0])
    nodes = [store.register(NodeRegistration(
        name=f"node-{i}", platform="test", endpoint=f"http://node-{i}:9000",
        available_memory_mb=1, compute_weight=1,
    )) for i in range(2)]
    tokens = {node["node_id"]: node["node_token"] for node in nodes}
    request = TaskRequest(model_id="test", num_layers=2, layer_memory_mb=1)
    plan = store.create_task(request)
    shard = plan["assignments"][0]
    store.report(shard["node_id"], tokens[shard["node_id"]], shard["id"],
                 AssignmentReport(status="completed"))
    store.heartbeat(shard["node_id"], tokens[shard["node_id"]],
                    Heartbeat(available_memory_mb=1, accepting_tasks=False))
    assert {n["status"] for n in store.nodes()} == {"busy"}
    with pytest.raises(HTTPException) as error:
        store.create_task(request)
    assert error.value.status_code == 409
    store.cancel(plan["id"])
    assert {n["status"] for n in store.nodes()} == {"available", "draining"}
    now[0] += 30
    assert {n["status"] for n in store.nodes()} == {"offline"}


def test_empty_batched_views(tmp_path):
    store = Store(str(tmp_path / "empty.sqlite3"), 30)
    assert store.nodes() == []
    assert store.tasks() == []
    with pytest.raises(HTTPException) as error:
        store.get_task("missing")
    assert error.value.status_code == 404
