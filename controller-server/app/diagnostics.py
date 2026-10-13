"""Read-only, redacted checks for a Controller SQLite database."""

import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REQUIRED_COLUMNS = {
    "nodes": {"id", "token_hash", "name", "platform", "endpoint", "available_memory_mb",
              "compute_weight", "accepting_tasks", "last_seen"},
    "tasks": {"id", "request", "status", "created_at", "deadline", "error"},
    "assignments": {"id", "task_id", "node_id", "position", "layer_start", "layer_end", "status", "error"},
}
REQUIRED_REFERENCES = {("task_id", "tasks", "id"), ("node_id", "nodes", "id")}


def diagnose(database: str | Path, timeout_seconds: float = 10.0) -> dict:
    """Check one consistent snapshot; never initialize, expire, or repair rows."""
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
        raise ValueError("Timeout must be greater than zero and at most 300 seconds")
    result = {
        "status": "error",
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
    }
    deadline = time.monotonic() + timeout_seconds
    try:
        uri = Path(database).expanduser().resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=min(timeout_seconds, 1.0))) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            db.execute("BEGIN")
            quick_failures = sum(row[0] != "ok" for row in db.execute("PRAGMA quick_check"))
            fk_violations = sum(1 for _ in db.execute("PRAGMA foreign_key_check"))
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            missing_tables = sorted(REQUIRED_COLUMNS.keys() - tables)
            missing_columns = {}
            for table, expected in REQUIRED_COLUMNS.items():
                if table not in tables:
                    continue
                # Table names are fixed Controller constants, never input SQL.
                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                missing = sorted(expected - columns)
                if missing:
                    missing_columns[table] = missing
            references = { (row[3], row[2], row[4]) for row in
                           db.execute("PRAGMA foreign_key_list(assignments)") }
            missing_references = sorted(REQUIRED_REFERENCES - references)
            checks = {
                "quick_check": {"status": "failed" if quick_failures else "ok", "failure_count": quick_failures},
                "foreign_keys": {"status": "failed" if fk_violations else "ok", "violation_count": fk_violations},
                "schema": {"status": "failed" if missing_tables or missing_columns or missing_references else "ok",
                           "missing_tables": missing_tables, "missing_columns": missing_columns,
                           "missing_references": [list(reference) for reference in missing_references]},
            }
            if time.monotonic() >= deadline:
                result["error"] = "timeout"
            else:
                result.update(status="ok" if all(c["status"] == "ok" for c in checks.values()) else "failed",
                              checks=checks)
    except sqlite3.Error as exc:
        # SQLite diagnostic messages and offending rows may contain user data.
        # Emit only a stable category, never the exception text or row contents.
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        result["error"] = ("timeout" if time.monotonic() >= deadline else
                           "database_busy" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else
                           "database_unavailable" if code == sqlite3.SQLITE_CANTOPEN else
                           "invalid_database" if code in (sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT) else
                           "database_error")
    except (OSError, ValueError):
        result["error"] = "database_unavailable"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check a Controller database without changing its records")
    parser.add_argument("--database", required=True, help="Existing Controller SQLite file")
    parser.add_argument("--timeout-seconds", type=float, default=10.0, help="Scan budget, default 10 seconds (max 300)")
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.database, args.timeout_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1 if result["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
