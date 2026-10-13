from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - only reached on Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt

CREDENTIALS_FILE = "node-credentials.json"
JOURNAL_FILE = "journal.json"
PENDING_REPORTS_FILE = "pending-reports.json"
LOCK_FILE = "agent.lock"
JOURNAL_LIMIT = 512


class StateError(Exception):
    pass


@dataclass(frozen=True)
class NodeCredentials:
    node_id: str
    node_token: str
    heartbeat_ttl_seconds: int


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc


def save_credentials(state_dir: Path, credentials: NodeCredentials) -> None:
    _atomic_write_json(Path(state_dir) / CREDENTIALS_FILE, {
        "node_id": credentials.node_id,
        "node_token": credentials.node_token,
        "heartbeat_ttl_seconds": credentials.heartbeat_ttl_seconds,
    })


def load_credentials(state_dir: Path) -> NodeCredentials | None:
    data = _read_json(Path(state_dir) / CREDENTIALS_FILE)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise StateError("credentials file must contain a JSON object")
    node_id = data.get("node_id")
    node_token = data.get("node_token")
    try:
        ttl = int(data.get("heartbeat_ttl_seconds") or 30)
    except (TypeError, ValueError) as exc:
        raise StateError("credentials file has invalid heartbeat_ttl_seconds") from exc
    if not isinstance(node_id, str) or not node_id or not isinstance(node_token, str) or not node_token:
        raise StateError("credentials file has invalid node_id/node_token")
    return NodeCredentials(node_id, node_token, ttl)


def settle_journal(state_dir: Path, assignment_id: str, status: str) -> None:
    path = Path(state_dir) / JOURNAL_FILE
    data = _read_json(path) or {}
    entries = data.get("settled") if isinstance(data, dict) else None
    entries = [entry for entry in (entries or []) if isinstance(entry, dict)]
    entries.append({"id": assignment_id, "status": status})
    entries = entries[-JOURNAL_LIMIT:]
    _atomic_write_json(path, {"settled": entries})


def load_settled(state_dir: Path) -> dict[str, str]:
    data = _read_json(Path(state_dir) / JOURNAL_FILE)
    if data is None:
        return {}
    entries = data.get("settled") if isinstance(data, dict) else None
    settled: dict[str, str] = {}
    for entry in entries or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            settled[entry["id"]] = str(entry.get("status", ""))
    return settled


def load_pending_reports(state_dir: Path) -> dict[str, tuple[str, str | None]]:
    data = _read_json(Path(state_dir) / PENDING_REPORTS_FILE)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise StateError("pending reports file must contain a JSON object")
    pending: dict[str, tuple[str, str | None]] = {}
    for assignment_id, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
            raise StateError(f"pending report for {assignment_id} is invalid")
        error = entry.get("error")
        pending[str(assignment_id)] = (entry["status"], error if isinstance(error, str) else None)
    return pending


def record_pending_report(state_dir: Path, assignment_id: str, status: str,
                          error: str | None) -> None:
    path = Path(state_dir) / PENDING_REPORTS_FILE
    data = _read_json(path) or {}
    if not isinstance(data, dict):
        raise StateError("pending reports file must contain a JSON object")
    data[assignment_id] = {"status": status, "error": error}
    _atomic_write_json(path, data)


def clear_pending_report(state_dir: Path, assignment_id: str) -> None:
    path = Path(state_dir) / PENDING_REPORTS_FILE
    data = _read_json(path)
    if not isinstance(data, dict) or assignment_id not in data:
        return
    del data[assignment_id]
    _atomic_write_json(path, data)


def acquire_state_lock(state_dir: Path):
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    handle = (path / LOCK_FILE).open("a+")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle
