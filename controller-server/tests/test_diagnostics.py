import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from app.diagnostics import diagnose, main
from app.models import NodeRegistration, TaskRequest
from app.store import Store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "controller.sqlite3"
    store = Store(str(path), 30, clock=lambda: 1000.0)
    node = store.register(NodeRegistration(name="private-node-name", platform="test", endpoint="http://node.local:9000",
                                           available_memory_mb=1024, compute_weight=1))
    task = store.create_task(TaskRequest(model_id="private-model", num_layers=1, layer_memory_mb=512))
    return path, node, task


def test_healthy_database_redacted_and_no_expiry(database):
    path, node, task = database
    before = path.read_bytes()
    result = diagnose(path)
    assert result["status"] == "ok"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["checked_at"].endswith("+08:00")
    text = json.dumps(result)
    assert node["node_token"] not in text and node["node_id"] not in text
    assert "private-node-name" not in text and "private-model" not in text
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        # This task is far past its real deadline. Diagnosis must not expire it.
        assert db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0] == "queued"


def test_live_wal_snapshot_detects_foreign_key_violation_without_writes(database):
    path, _, task = database
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE assignments SET node_id='private-orphan' WHERE task_id=?", (task["id"],))
        writer.commit()
        wal = Path(str(path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        before = (path.read_bytes(), wal.read_bytes())
        result = diagnose(path)
        assert result["status"] == "failed"
        assert result["checks"]["foreign_keys"] == {"status": "failed", "violation_count": 1}
        assert "private-orphan" not in json.dumps(result)
        assert (path.read_bytes(), wal.read_bytes()) == before


def test_wrong_database_schema_is_not_healthy(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE users (password TEXT)")
    result = diagnose(path)
    assert result["status"] == "failed"
    assert result["checks"]["schema"]["missing_tables"] == ["assignments", "nodes", "tasks"]
    assert "password" not in json.dumps(result)


def test_missing_columns_and_constraints_detected(tmp_path):
    path = tmp_path / "partial.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE nodes(id TEXT); CREATE TABLE tasks(id TEXT); CREATE TABLE assignments(id TEXT);")
    schema = diagnose(path)["checks"]["schema"]
    assert schema["status"] == "failed"
    assert "token_hash" in schema["missing_columns"]["nodes"]
    assert schema["missing_references"] == [["node_id", "nodes", "id"], ["task_id", "tasks", "id"]]


def test_missing_source_never_creates_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert diagnose(path)["error"] == "database_unavailable"
    assert not path.exists()


def test_invalid_file_is_redacted(tmp_path):
    path = tmp_path / "broken.sqlite3"
    path.write_text("private-file-contents")
    result = diagnose(path)
    assert result["status"] == "error" and result["error"] == "invalid_database"
    assert "private-file-contents" not in json.dumps(result)


def test_locked_database_returns_within_budget(tmp_path):
    path = tmp_path / "locked.sqlite3"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE demo (id INTEGER)")
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = diagnose(path, timeout_seconds=0.1)
        assert time.monotonic() - started < 1.5
        assert result["status"] == "error" and result["error"] in ("timeout", "database_busy")


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf")])
def test_invalid_scan_budget_rejected(database, timeout):
    with pytest.raises(ValueError, match="Timeout"):
        diagnose(database[0], timeout)


def test_cli_json_and_exit_codes(database, tmp_path, capsys):
    assert main(["--database", str(database[0])]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert main(["--database", str(tmp_path / "missing")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "database_unavailable"
    with closing(sqlite3.connect(database[0])) as db:
        db.execute("DROP TABLE assignments")
    assert main(["--database", str(database[0])]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_module_entrypoint(database):
    process = subprocess.run([sys.executable, "-m", "app.diagnostics", "--database", str(database[0])],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["status"] == "ok"
