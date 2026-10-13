"""Scheduling/expiry contracts with real SQLite and retained registry history."""
from contextlib import closing
import sqlite3

import pytest
from fastapi import HTTPException

from app.models import TaskRequest
from app.store import Store


NOW = 1_000.0
REQUEST = TaskRequest(model_id="local-test", num_layers=1, layer_memory_mb=1)


def seed_nodes(db, historical_count=0):
    db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", (
        (f"offline-{i}", "hash", "historical", "test", "http://offline:9000", 1024, 1000, 1, 0)
        for i in range(historical_count)
    ))
    db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)", [
        ("available", "hash", "available", "test", "http://available:9000", 1024, 1, 1, NOW),
        ("draining", "hash", "draining", "test", "http://draining:9000", 1024, 1000, 0, NOW),
        ("boundary", "hash", "TTL", "test", "http://boundary:9000", 1024, 1000, 1, NOW - 30),
        ("reserved", "hash", "busy", "test", "http://reserved:9000", 1024, 1000, 1, NOW),
    ])
    db.execute("INSERT INTO tasks VALUES ('reserved-task', '{}', 'running', ?, ?, NULL)",
               (NOW - 1, NOW + 100))
    # Even a completed shard stays reserved while its pipeline is active.
    db.execute("INSERT INTO assignments VALUES ('reserved-shard', 'reserved-task', 'reserved', 0, 0, 1, 'completed', NULL, NULL)")


def new_store(tmp_path, name="store"):
    return Store(str(tmp_path / f"{name}.sqlite3"), 30, clock=lambda: NOW)


def test_candidate_filter_retains_registry_and_reserves_completed_shards(tmp_path):
    store = new_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        seed_nodes(db, 100)
    plan = store.create_task(REQUEST)
    assert [a["node_id"] for a in plan["assignments"]] == ["available"]
    assert plan["assignments"][0]["endpoint"] == "http://available:9000"
    with pytest.raises(HTTPException) as error:
        store.create_task(REQUEST)
    assert error.value.status_code == 409
    rows = store.nodes()
    assert len(rows) == 104
    assert {n["id"]: n["status"] for n in rows if not n["id"].startswith("offline-")} == {
        "available": "busy", "draining": "draining", "boundary": "offline", "reserved": "busy",
    }


def test_candidate_filter_preserves_nonzero_boolean_storage(tmp_path):
    store = new_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        seed_nodes(db)
        db.execute("UPDATE nodes SET accepting_tasks=2 WHERE id='available'")
    assert store.create_task(REQUEST)["assignments"][0]["node_id"] == "available"


def test_offline_registry_growth_does_not_multiply_scheduling_sql_work(tmp_path, monkeypatch):
    work = []
    for history in (1_000, 20_000):
        store = new_store(tmp_path, f"history-{history}")
        with closing(sqlite3.connect(store.path)) as db, db:
            seed_nodes(db, history)
        original = sqlite3.connect
        calls = [0]

        def progress():
            calls[0] += 1
            return 0

        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_progress_handler(progress, 100)
            return db

        with monkeypatch.context() as patch:
            patch.setattr("app.store.sqlite3.connect", connect)
            assert store.create_task(REQUEST)["assignments"][0]["node_id"] == "available"
        work.append(calls[0] * 100)
    assert work[1] <= work[0] * 2 + 1_000, work


def seed_expired(store, count=1_201):
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("INSERT INTO nodes VALUES ('fresh', 'hash', 'fresh', 'test', 'http://fresh:9000', 1, 1, 1, ?)", (NOW,))
        db.execute("INSERT INTO nodes VALUES ('stale', 'hash', 'stale', 'test', 'http://stale:9000', 1, 1, 1, ?)", (NOW - 30,))
        for i in range(count):
            deadline = NOW if i % 2 == 0 else NOW + 100
            db.execute("INSERT INTO tasks VALUES (?, '{}', 'running', ?, ?, NULL)",
                       (f"task-{i:04}", NOW - 1, deadline))
            for position, status in enumerate(("completed", "running", "pending")):
                node_id = f"node-{i:04}-{position}"
                db.execute("INSERT INTO nodes VALUES (?, 'hash', 'stale', 'test', 'http://stale:9000', 1, 1, 1, ?)",
                           (node_id, NOW - 30))
                db.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?,?)", (
                    f"shard-{i:04}-{position}", f"task-{i:04}", node_id, position,
                    position, position + 1, status, "existing" if status == "completed" else None, None,
                ))
        db.execute("INSERT INTO tasks VALUES ('alive', '{}', 'queued', ?, ?, NULL)", (NOW, NOW + 100))
        db.execute("INSERT INTO assignments VALUES ('alive-shard', 'alive', 'fresh', 0, 0, 1, 'pending', NULL, NULL)")
        db.execute("INSERT INTO tasks VALUES ('terminal', '{}', 'cancelled', 0, 1, 'operator')")
        db.execute("INSERT INTO assignments VALUES ('terminal-shard', 'terminal', 'stale', 0, 0, 1, 'cancelled', 'operator', NULL)")


def snapshot(store):
    with closing(sqlite3.connect(store.path)) as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("nodes", "tasks", "assignments")}


def assert_expired(store, count):
    with closing(sqlite3.connect(store.path)) as db:
        for i in range(count):
            error = "task deadline exceeded" if i % 2 == 0 else "assigned node heartbeat expired"
            assert db.execute("SELECT status,error FROM tasks WHERE id=?", (f"task-{i:04}",)).fetchone() == ("failed", error)
            assert db.execute("SELECT status,error FROM assignments WHERE task_id=? ORDER BY position", (f"task-{i:04}",)).fetchall() == [
                ("completed", "existing"), ("failed", error), ("failed", error),
            ]
        assert db.execute("SELECT status,error FROM tasks WHERE id='alive'").fetchone() == ("queued", None)
        assert db.execute("SELECT status,error FROM tasks WHERE id='terminal'").fetchone() == ("cancelled", "operator")


def test_mass_expiry_batches_updates_and_respects_connection_variable_limit(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    count = 1_201
    seed_expired(store, count)
    original = sqlite3.connect
    updates = []

    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 99)
        db.set_trace_callback(lambda sql: updates.append(sql) if sql.lstrip().startswith("UPDATE") else None)
        return db

    with monkeypatch.context() as patch:
        patch.setattr("app.store.sqlite3.connect", connect)
        store.expire()
    assert_expired(store, count)
    # Two failure-reason groups, with at most 98 task IDs plus one error value.
    assert len(updates) <= 28, len(updates)
    before = snapshot(store)
    store.expire()
    assert snapshot(store) == before


def test_failure_in_a_later_expiry_batch_rolls_back_entire_sweep(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    seed_expired(store, 1_201)
    before = snapshot(store)
    original = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        writes = 0

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().startswith("UPDATE assignments"):
                self.writes += 1
                if self.writes == 2:
                    raise sqlite3.OperationalError("injected later-batch failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original(*args, factory=FailingConnection, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("app.store.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError, match="later-batch"):
            store.expire()
    assert snapshot(store) == before
    store.expire()
    assert_expired(store, 1_201)


def test_rejected_request_still_commits_all_expiry_batches(tmp_path):
    store = new_store(tmp_path)
    seed_expired(store, 1_201)
    with pytest.raises(HTTPException) as error:
        store.get_task("unknown-task")
    assert error.value.status_code == 404
    assert_expired(store, 1_201)


def test_reopening_legacy_registry_adds_candidate_index_without_data_loss(tmp_path):
    store = new_store(tmp_path)
    with closing(sqlite3.connect(store.path)) as db, db:
        seed_nodes(db, 10)
        db.execute("DROP INDEX IF EXISTS nodes_accepting_heartbeat")
    before = snapshot(store)
    for _ in range(2):
        reopened = Store(store.path, 30, clock=lambda: NOW)
        assert snapshot(reopened) == before
        with closing(sqlite3.connect(store.path)) as db:
            assert "nodes_accepting_heartbeat" in {row[1] for row in db.execute("PRAGMA index_list(nodes)")}
