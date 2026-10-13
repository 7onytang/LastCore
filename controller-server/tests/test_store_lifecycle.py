"""Retain real connections so garbage collection cannot hide missing close()."""
import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException

from app.models import NodeRegistration, TaskRequest
from app.store import Store


@pytest.fixture
def connections(monkeypatch):
    opened = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr("app.store.sqlite3.connect", tracked_connect)
    return opened


def assert_closed(db):
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        db.execute("SELECT 1")


def test_initialization_closes_connection_and_persists_schema(tmp_path, connections):
    store = Store(str(tmp_path / "store.sqlite3"), 30)
    assert len(connections) == 1
    assert_closed(connections[0])
    with closing(sqlite3.connect(store.path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"nodes", "tasks", "assignments", "assignment_results"}
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("failure", ["journal_mode", "schema"])
def test_initialization_failure_closes_connection(tmp_path, monkeypatch, failure):
    opened = []
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if failure == "journal_mode" and sql == "PRAGMA journal_mode=WAL":
                raise sqlite3.OperationalError("initialization failed")
            return super().execute(sql, *args, **kwargs)

        def executescript(self, script):
            if failure == "schema":
                raise sqlite3.OperationalError("initialization failed")
            return super().executescript(script)

    def failing_connect(*args, **kwargs):
        db = connect(*args, factory=FailingConnection, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr("app.store.sqlite3.connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError, match="initialization failed"):
        Store(str(tmp_path / "store.sqlite3"), 30)
    assert_closed(opened[0])


@pytest.mark.parametrize("failure", ["row_factory", "foreign_keys"])
def test_transaction_setup_failure_closes_connection(tmp_path, monkeypatch, failure):
    store = Store(str(tmp_path / "store.sqlite3"), 30)
    opened = []
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def __setattr__(self, name, value):
            if failure == "row_factory" and name == "row_factory":
                raise RuntimeError("transaction setup failed")
            super().__setattr__(name, value)

        def execute(self, sql, *args, **kwargs):
            if failure == "foreign_keys" and sql == "PRAGMA foreign_keys=ON":
                raise RuntimeError("transaction setup failed")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        db = connect(*args, factory=FailingConnection, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr("app.store.sqlite3.connect", failing_connect)
    with pytest.raises(RuntimeError, match="transaction setup failed"):
        with store.transaction():
            pytest.fail("setup failure must prevent entering the transaction")
    assert_closed(opened[0])


def test_transaction_normal_return_closes_and_commits(tmp_path, connections):
    store = Store(str(tmp_path / "store.sqlite3"), 30)

    def insert_task():
        with store.transaction() as db:
            db.execute("INSERT INTO tasks VALUES ('committed', '{}', 'queued', 0, 1, NULL)")
            return "returned"

    assert insert_task() == "returned"
    assert_closed(connections[-1])
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT id FROM tasks").fetchall() == [("committed",)]


def test_transaction_exception_closes_and_rolls_back(tmp_path, connections):
    store = Store(str(tmp_path / "store.sqlite3"), 30)
    with pytest.raises(ValueError, match="request failed"):
        with store.transaction() as db:
            db.execute("INSERT INTO tasks VALUES ('rolled-back', '{}', 'queued', 0, 1, NULL)")
            raise ValueError("request failed")
    assert_closed(connections[-1])
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT id FROM tasks").fetchall() == []


def test_rejected_request_preserves_committed_expiry_and_closes(tmp_path, connections):
    now = [1000.0]
    store = Store(str(tmp_path / "store.sqlite3"), 30, clock=lambda: now[0])
    store.register(NodeRegistration(name="node", platform="linux", endpoint="http://node.local:9000",
                                    available_memory_mb=1024, compute_weight=1))
    task = store.create_task(TaskRequest(model_id="test", num_layers=1, layer_memory_mb=512,
                                        timeout_seconds=10))
    now[0] += 10
    with pytest.raises(HTTPException) as exc:
        store.get_task("unknown-task")
    assert exc.value.status_code == 404
    assert_closed(connections[-1])
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT status, error FROM tasks WHERE id=?", (task["id"],)).fetchone() == (
            "failed", "task deadline exceeded")
        assert db.execute("SELECT status FROM assignments WHERE task_id=?", (task["id"],)).fetchall() == [
            ("failed",)]
