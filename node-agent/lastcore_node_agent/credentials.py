from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

CREDENTIALS_FILE = "node-credentials.json"
JOURNAL_FILE = "journal.json"
PENDING_REPORTS_FILE = "pending-reports.json"
LOCK_FILE = "agent.lock"
JOURNAL_LIMIT = 512
SETTLED_STATUSES = frozenset({"completed", "failed", "stopped", "skipped-terminal"})


class StateError(Exception):
    pass


@dataclass(frozen=True)
class NodeCredentials:
    node_id: str
    node_token: str
    heartbeat_ttl_seconds: int

    def __post_init__(self):
        if (not isinstance(self.node_id, str) or not self.node_id
                or not isinstance(self.node_token, str) or not self.node_token):
            raise StateError("credentials have invalid node_id/node_token")
        if type(self.heartbeat_ttl_seconds) is not int or not 5 <= self.heartbeat_ttl_seconds <= 3600:
            raise StateError("credentials heartbeat_ttl_seconds must be an integer between 5 and 3600")

    @classmethod
    def from_record(cls, data) -> "NodeCredentials":
        if not isinstance(data, dict):
            raise StateError("credentials must contain an object")
        # Older records may omit TTL. An explicit invalid value is never an
        # absent default and must not be coerced or silently repaired.
        return cls(data.get("node_id"), data.get("node_token"),
                   data.get("heartbeat_ttl_seconds", 30))


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


def _json_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise StateError("state JSON contains duplicate object keys")
        data[key] = value
    return data


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=_json_object)
        # Every state format is an object. None is reserved for an absent file,
        # never a parsed JSON null or another present-but-invalid top-level value.
        if not isinstance(data, dict):
            raise StateError("state JSON must contain an object")
        return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
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
    return NodeCredentials.from_record(data)


def _settled_entries(data) -> list[dict]:
    # Missing is the only implicit empty journal. Never repair a present file by
    # discarding entries: they are the evidence used to prevent repeat execution.
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) != {"settled"}:
        raise StateError("settled journal schema is invalid; preserve for recovery")
    entries = data["settled"]
    if not isinstance(entries, list) or len(entries) > JOURNAL_LIMIT:
        raise StateError("settled journal entries are invalid; preserve for recovery")
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"id", "status"}
                or not isinstance(entry["id"], str) or not entry["id"]
                or not isinstance(entry["status"], str) or entry["status"] not in SETTLED_STATUSES):
            raise StateError("settled journal entry is invalid; preserve for recovery")
    return entries


def settle_journal(state_dir: Path, assignment_id: str, status: str) -> None:
    path = Path(state_dir) / JOURNAL_FILE
    entries = _settled_entries(_read_json(path))
    entry = {"id": assignment_id, "status": status}
    _settled_entries({"settled": [entry]})
    _atomic_write_json(path, {"settled": (entries + [entry])[-JOURNAL_LIMIT:]})


def load_settled(state_dir: Path) -> dict[str, str]:
    entries = _settled_entries(_read_json(Path(state_dir) / JOURNAL_FILE))
    return {entry["id"]: entry["status"] for entry in entries}


def _validate_pending_data(data):
    if not isinstance(data, dict):
        raise StateError("pending reports file must contain a JSON object")
    results = {}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            raise StateError("pending journal entry schema is invalid")
        if not isinstance(entry.get("status"), str):
            raise StateError("pending journal status must be a legacy string")
        if "error" in entry and entry["error"] is not None and not isinstance(entry["error"], str):
            raise StateError("pending journal error must be a string or null")
        if {"workload_result", "binding", "result"} & entry.keys():
            if not {"workload_result", "binding"}.issubset(entry):
                raise StateError("incomplete typed journal marker; preserve for recovery")
            binding = entry.get("binding")
            if (not isinstance(binding, dict) or entry.get("status") != "completed"
                    or any(not isinstance(binding.get(field), str) or not binding[field]
                           for field in ("origin", "assignment_id", "task_id", "node_id", "model_id"))
                    or type(binding.get("layer_start")) is not int or type(binding.get("layer_end")) is not int
                    or not 0 <= binding["layer_start"] < binding["layer_end"] <= 100_000):
                raise StateError("pending result has no valid source binding; preserve for recovery")
            if (set(entry) != {"status", "error", "workload_result", "binding"} or entry["error"] is not None
                    or set(binding) != {"origin", "assignment_id", "task_id", "node_id", "model_id", "layer_start", "layer_end", "request_sha256"}
                    or binding["assignment_id"] != str(key)):
                raise StateError("pending typed identity/schema mismatch; preserve for recovery")
            values = entry["workload_result"]
            model = binding["model_id"]
            if (not isinstance(values, dict) or any(type(value) is not int or not 0 <= value <= 100_000_000 for value in values.values())
                    or model not in ("lastcore/pi-monte-carlo-v1", "lastcore/log-statistics-v1")
                    or not isinstance(binding.get("request_sha256"), str) or len(binding["request_sha256"]) != 64
                    or any(c not in "0123456789abcdef" for c in binding["request_sha256"])
                    or (model == "lastcore/pi-monte-carlo-v1" and (set(values) != {"inside", "samples"}
                        or values["samples"] <= 0 or values["inside"] > values["samples"]))
                    or (model == "lastcore/log-statistics-v1" and (not set(values).issubset({"ERROR", "WARN", "INFO", "OTHER"})
                        or sum(values.values()) != binding["layer_end"] - binding["layer_start"]))):
                raise StateError("pending typed result is invalid; preserve for recovery")
            results[str(key)] = (values, binding)
        elif not set(entry).issubset({"status", "error"}):
            raise StateError("pending legacy journal has unknown fields")
    return results



def load_pending_reports(state_dir: Path) -> dict[str, tuple[str, str | None]]:
    data = _read_json(Path(state_dir) / PENDING_REPORTS_FILE)
    if data is None:
        return {}
    _validate_pending_data(data)
    return {assignment_id: (entry["status"], entry.get("error")) for assignment_id, entry in data.items()}


def load_pending_report_results(state_dir: Path) -> dict[str, object]:
    data = _read_json(Path(state_dir) / PENDING_REPORTS_FILE)
    if data is None:
        return {}
    return _validate_pending_data(data)


def record_pending_report(state_dir: Path, assignment_id: str, status: str,
                          error: str | None, result=None, binding=None) -> None:
    path = Path(state_dir) / PENDING_REPORTS_FILE
    data = _read_json(path)
    if data is None:
        data = {}
    _validate_pending_data(data)
    if binding is not None and result is None:
        raise StateError("typed pending outcome requires its result")
    entry = {"status": status, "error": error}
    if result is not None:
        entry["workload_result"] = result
        entry["binding"] = binding
    _validate_pending_data({assignment_id: entry})
    if assignment_id in data:
        previous = data[assignment_id]
        if previous != entry and ({"workload_result", "binding", "result"} & previous.keys() or result is not None):
            raise StateError("cannot replace a pending typed outcome")
    data[assignment_id] = entry
    _atomic_write_json(path, data)


def clear_pending_report(state_dir: Path, assignment_id: str) -> None:
    path = Path(state_dir) / PENDING_REPORTS_FILE
    data = _read_json(path)
    if data is None:
        return
    _validate_pending_data(data)
    if assignment_id not in data:
        return
    del data[assignment_id]
    _atomic_write_json(path, data)


def _lock_exclusive_nonblocking(handle) -> None:
    """Take a non-blocking exclusive lock on an open file handle.

    Mirrors ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` on POSIX and
    ``msvcrt.locking(fd, LK_NBLCK, 1)`` on Windows: it raises ``OSError`` when the
    lock is already held by another process (or another handle in this process) and
    otherwise holds byte 0 until the handle is closed. Closing the handle - or the
    owning process exiting - releases the lock, matching the POSIX semantics the
    node-agent relies on.
    """
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire_state_lock(state_dir: Path):
    path = Path(state_dir)
    path.mkdir(parents=True, exist_ok=True)
    handle = (path / LOCK_FILE).open("a+")
    try:
        _lock_exclusive_nonblocking(handle)
    except OSError:
        handle.close()
        return None
    return handle
