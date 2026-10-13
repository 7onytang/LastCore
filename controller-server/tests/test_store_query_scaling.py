"""Bound public reads by current results, rather than retained task history."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from app.store import Store


NOW = 1_000_000.0
NODE_COUNT = 16
NODE_TOKEN = "local-scaling-test-node-token"
INDEXES = {"tasks_status", "tasks_created_at"}


def history_store(tmp_path, history_count):
    """Seed committed history directly; setup cost is outside every measurement."""
    store = Store(str(tmp_path / f"history-{history_count}.sqlite3"), 30, clock=lambda: NOW)
    request = json.dumps({"model_id": "history-model", "num_layers": 1,
                          "layer_memory_mb": 1, "max_nodes": 1, "timeout_seconds": 600})
    token_hash = hashlib.sha256(NODE_TOKEN.encode()).hexdigest()
    with closing(sqlite3.connect(store.path)) as db, db:
        db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", (
            (f"node-{i:02}", token_hash, f"Node {i}", "linux", f"http://node-{i}.test:9000",
             1024, 1.0, 1, NOW) for i in range(NODE_COUNT)
        ))
        db.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)", (
            (f"history-{i:06}", request, ("completed", "failed", "cancelled")[i % 3],
             NOW - history_count - 10 + i, NOW - 1) for i in range(history_count)
        ))
        db.executemany("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,NULL,NULL)", (
            (f"assignment-{i:06}", f"history-{i:06}", f"node-{i % NODE_COUNT:02}", 0, 0, 1,
             ("completed", "failed", "cancelled")[i % 3]) for i in range(history_count)
        ))
        db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,NULL)",
                   ("active-task", request, "running", NOW, NOW + 600))
        db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                   ("active-assignment", "active-task", "node-00", 0, 0, 1, "running"))
    return store


def measure(monkeypatch, operation):
    """Count SQLite work, not elapsed time or source-level implementation details."""
    connect = sqlite3.connect
    selects = []
    progress_calls = 0

    def progress():
        nonlocal progress_calls
        progress_calls += 1
        return 0

    def trace(sql):
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            selects.append(sql)

    def instrumented_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(trace)
        db.set_progress_handler(progress, 100)
        return db

    with monkeypatch.context() as patch:
        patch.setattr("app.store.sqlite3.connect", instrumented_connect)
        result = operation()
    return result, selects, progress_calls * 100


def operation(store, name):
    if name == "assignments":
        return lambda: store.assignments("node-00", NODE_TOKEN)
    return getattr(store, name)


def assert_result(name, result):
    if name == "expire":
        assert result is None
    elif name == "nodes":
        assert len(result) == NODE_COUNT
        assert {node["id"] for node in result if node["status"] == "busy"} == {"node-00"}
        assert all(node["status"] == "available" for node in result if node["id"] != "node-00")
    elif name == "tasks":
        assert len(result) == 100
        assert result[0]["id"] == "active-task"
        assert result[0]["status"] == "running"
        assert all(len(task["assignments"]) == 1 for task in result)
    else:
        assert result
        assert result[0]["id"] == "active-assignment"
        assert all(assignment["node_id"] == "node-00" for assignment in result)


@pytest.mark.parametrize("name,budget", [("nodes", 3), ("tasks", 3), ("assignments", 4)])
def test_public_reads_have_bounded_select_count(tmp_path, monkeypatch, name, budget):
    store = history_store(tmp_path, 2_000)
    result, selects, _ = measure(monkeypatch, operation(store, name))
    assert_result(name, result)
    # Includes transaction expiry and node authentication where applicable.
    assert len(selects) <= budget, f"{name} issued {len(selects)} SELECTs, budget {budget}"


@pytest.mark.parametrize("name", ["expire", "nodes", "tasks"])
def test_terminal_history_does_not_multiply_public_read_work(tmp_path, monkeypatch, name):
    counts = []
    for history_count in (1_000, 20_000):
        store = history_store(tmp_path, history_count)
        result, _, instructions = measure(monkeypatch, operation(store, name))
        assert_result(name, result)
        counts.append(instructions)
    # Twenty times the retained history must not cause a proportional scan.
    # Allow B-tree depth/planner differences and progress-handler quantization.
    assert counts[1] <= counts[0] * 2 + 1_000, f"{name} SQLite instruction counts: {counts}"


def test_reopening_existing_data_adds_indexes_idempotently(tmp_path):
    store = history_store(tmp_path, 7)
    with closing(sqlite3.connect(store.path)) as db, db:
        # The legacy tables and data are unchanged; only the new indexes are absent.
        for name in INDEXES:
            db.execute(f"DROP INDEX IF EXISTS {name}")
        before = {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                  for table in ("nodes", "tasks", "assignments")}
    for _ in range(2):
        reopened = Store(store.path, 30, clock=lambda: NOW)
        with closing(sqlite3.connect(reopened.path)) as db:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(tasks)")}
            assert INDEXES <= indexes
            after = {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                     for table in before}
            assert after == before
        # Previously issued node credentials and active task state still work.
        assignments = reopened.assignments("node-00", NODE_TOKEN)
        assert assignments[0]["id"] == "active-assignment"
        assert assignments[0]["task_status"] == "running"
