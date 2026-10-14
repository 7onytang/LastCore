"""Read-only preflight for a future workload protocol downgrade; never performs it."""
import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def check(database, timeout_seconds=10):
    result = {"status": "error", "business_timezone": "Asia/Shanghai",
              "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
              "scope": "read-only active typed-task gate; caller must stop new admissions first"}
    deadline = time.monotonic() + timeout_seconds
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
            db.execute("BEGIN")
            active = 0
            for (raw_request,) in db.execute("SELECT request FROM tasks WHERE status IN ('queued','running')"):
                request = json.loads(raw_request)
                if not isinstance(request, dict):
                    raise ValueError("invalid request")
                active += (request.get("workload") in ("pi", "log_stats") or
                           request.get("model_id") in ("lastcore/pi-monte-carlo-v1", "lastcore/log-statistics-v1"))
            result.update(status="blocked" if active else "ready", active_typed_tasks=active)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        result["error"] = "unable_to_verify_gate"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    result = check(args.database)
    print(json.dumps(result, sort_keys=True))
    return {"ready": 0, "blocked": 1, "error": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
