"""Observe persisted expiry directly, so API-triggered cleanup cannot mask it."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.models import NodeRegistration, TaskRequest
from app.store import Store


@pytest.fixture
def state(tmp_path):
    now = [1000.0]
    store = Store(str(tmp_path / "controller.sqlite3"), 30, clock=lambda: now[0])
    store.register(NodeRegistration(name="node", platform="test", endpoint="http://node.local:9000",
                                    available_memory_mb=2048, compute_weight=1))
    task = store.create_task(TaskRequest(model_id="test", num_layers=4, layer_memory_mb=512,
                                        timeout_seconds=60))
    return store, now, task


def app(store):
    return create_app(Settings("a" * 32, "b" * 32, expiry_interval_seconds=0.1), store)


def persisted(store, task):
    with closing(sqlite3.connect(store.path)) as db:
        return db.execute("SELECT status, error FROM tasks WHERE id=?", (task["id"],)).fetchone()


@pytest.mark.parametrize("elapsed,error", [(30, "assigned node heartbeat expired"),
                                          (60, "task deadline exceeded")])
def test_startup_reconciles_expired_state_without_requests(state, elapsed, error):
    store, now, task = state
    now[0] += elapsed
    # Reopen the persisted database to exercise a real application restart.
    reopened = Store(store.path, 30, clock=lambda: now[0])
    with TestClient(app(reopened)):
        assert persisted(reopened, task) == ("failed", error)
        with closing(sqlite3.connect(store.path)) as db:
            assert db.execute("SELECT status FROM assignments").fetchone()[0] == "failed"


@pytest.mark.parametrize("elapsed,error", [(30, "assigned node heartbeat expired"),
                                          (60, "task deadline exceeded")])
def test_idle_sweep_persists_expiry_and_stops_on_shutdown(state, monkeypatch, elapsed, error):
    store, now, task = state
    expired = threading.Event()
    unexpected_call = threading.Event()
    closed = threading.Event()
    original = store.expire

    def observe():
        if closed.is_set():
            unexpected_call.set()
        original()
        if persisted(store, task)[0] == "failed":
            expired.set()

    monkeypatch.setattr(store, "expire", observe)
    with TestClient(app(store)):
        assert persisted(store, task)[0] == "queued"
        now[0] += elapsed
        assert expired.wait(3), "no background expiry without API traffic"
        assert persisted(store, task) == ("failed", error)
    closed.set()
    assert not unexpected_call.wait(0.3), "expiry worker survived application shutdown"
    # Expired reservations can be reused once a node reconnects.
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE nodes SET last_seen=?", (now[0],))
    replacement = store.create_task(TaskRequest(model_id="test", num_layers=4, layer_memory_mb=512))
    assert replacement["status"] == "queued"


def test_sweep_retries_sqlite_errors(state, monkeypatch, caplog):
    store, now, task = state
    recovered = threading.Event()
    original = store.expire
    calls = 0

    def transient_error():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("database is locked")
        original()
        if calls > 2 and persisted(store, task)[0] == "failed":
            recovered.set()

    monkeypatch.setattr(store, "expire", transient_error)
    with TestClient(app(store)):
        now[0] += 30
        assert recovered.wait(3)
        assert persisted(store, task)[0] == "failed"
    assert "Controller expiry sweep failed" in caplog.text


def test_failed_startup_does_not_start_worker(state, monkeypatch):
    store, _, _ = state
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(store, "expire", fail)
    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        with TestClient(app(store)):
            pytest.fail("startup must reject unavailable database")
    assert calls == 1


def test_shutdown_drains_inflight_sqlite_work(state, monkeypatch):
    store, _, _ = state
    scanning = threading.Event()
    release = threading.Event()
    exiting = threading.Event()
    exited = threading.Event()
    original = store.expire
    calls = 0

    def slow_sweep():
        nonlocal calls
        calls += 1
        if calls == 2:
            scanning.set()
            assert release.wait(3)
        original()

    monkeypatch.setattr(store, "expire", slow_sweep)

    def lifecycle():
        with TestClient(app(store)):
            assert scanning.wait(3)
            exiting.set()
        exited.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lifecycle)
        try:
            assert exiting.wait(3)
            assert not exited.wait(0.1), "shutdown abandoned an in-flight SQLite sweep"
        finally:
            release.set()
        future.result(timeout=3)
    assert exited.is_set()


@pytest.mark.parametrize("interval", [0, -1, 0.09, 60.1, float("inf"), float("nan")])
def test_invalid_expiry_interval_rejected(interval):
    with pytest.raises(ValueError, match="Expiry interval"):
        create_app(Settings("a" * 32, "b" * 32, expiry_interval_seconds=interval))


def test_expiry_interval_from_environment(monkeypatch):
    monkeypatch.setenv("CONTROLLER_EXPIRY_INTERVAL_SECONDS", "2.5")
    assert Settings.from_env().expiry_interval_seconds == 2.5
