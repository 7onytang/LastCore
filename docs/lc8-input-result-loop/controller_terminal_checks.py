#!/usr/bin/env python3
"""LC-8 controller-side terminal semantics for the input/result loop.

Exercises the real FastAPI controller (no HTTP mocks) to pin down the parts of
"result readable + idempotent + no fake completed after cancel" that today live
only at the assignment-report layer:

  1. a ``completed`` report is idempotent: repeating it returns the same terminal
     task instead of an error or a state regression;
  2. after ``POST /v1/tasks/{id}/cancel``, a late ``completed`` report is rejected
     with 409, so a cancelled task can never be marked completed by a late node;
  3. the task reads back as ``cancelled`` after the rejected late report.

These checks do not invent a result payload: today the Controller stores only
``status``/``error`` per assignment (``controller-server/app/models.py``), which
is exactly the "result is not readable" gap recorded in gap-analysis.md.

Module location:

  LC_CONTROLLER_DIR  default: $LC_REPO_ROOT/controller-server   (or the repo root
                     if it already is the controller-server directory)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ADMIN = "a" * 40
REGISTRATION = "b" * 40


def _bootstrap() -> Path:
    repo = Path(os.environ.get("LC_REPO_ROOT", Path.cwd())).expanduser().resolve()
    directory = Path(os.environ.get("LC_CONTROLLER_DIR", repo)).expanduser().resolve()
    if not (directory / "app" / "main.py").is_file():
        directory = (repo / "controller-server").resolve()
    if not (directory / "app" / "main.py").is_file():
        raise SystemExit(
            f"LC_CONTROLLER_DIR={directory} has no app/main.py; point it at controller-server/")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return directory


_bootstrap()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import Settings, create_app  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="also write the JSON report to this path")
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="lc8-ctrl-"))
    settings = Settings(admin_token=ADMIN, registration_token=REGISTRATION,
                        database_path=str(workdir / "controller.sqlite3"),
                        heartbeat_ttl_seconds=30)
    client = TestClient(create_app(settings))
    report: dict = {}

    node = client.post("/v1/nodes/register",
                       headers={"Authorization": f"Bearer {REGISTRATION}"},
                       json={"name": "lc8-node", "platform": "test",
                             "endpoint": "http://127.0.0.1:9101",
                             "available_memory_mb": 4096, "compute_weight": 1.0})
    node.raise_for_status()
    node_id, node_token = node.json()["node_id"], node.json()["node_token"]
    node_auth = {"Authorization": f"Bearer {node_token}"}
    admin_auth = {"Authorization": f"Bearer {ADMIN}"}

    def new_task(num_layers: int) -> dict:
        response = client.post("/v1/tasks", headers=admin_auth,
                               json={"model_id": "lastcore/tiny-cpu", "num_layers": num_layers,
                                     "layer_memory_mb": 1024, "max_nodes": 1,
                                     "timeout_seconds": 600})
        response.raise_for_status()
        return response.json()

    def report_assignment(assignment_id: str, status: str, error=None):
        return client.post(f"/v1/nodes/{node_id}/assignments/{assignment_id}/report",
                           headers=node_auth, json={"status": status, "error": error})

    # 1) completed report is idempotent
    task = new_task(num_layers=1)
    assignment = task["assignments"][0]["id"]
    report["running"] = report_assignment(assignment, "running").status_code
    first = report_assignment(assignment, "completed")
    repeat = report_assignment(assignment, "completed")
    report["first_completed_status"] = first.status_code
    report["first_completed_task_status"] = first.json()["status"]
    report["repeat_completed_status"] = repeat.status_code
    report["repeat_completed_task_status"] = repeat.json()["status"]
    report["completed_report_idempotent"] = (
        first.status_code == 200 and repeat.status_code == 200
        and first.json()["status"] == "completed" and repeat.json()["status"] == "completed")

    # 2) cancelled task rejects a late completed report
    cancelled_task = new_task(num_layers=1)
    cancelled_assignment = cancelled_task["assignments"][0]["id"]
    report_assignment(cancelled_assignment, "running")
    cancel = client.post(f"/v1/tasks/{cancelled_task['id']}/cancel", headers=admin_auth)
    report["cancel_status"] = cancel.status_code
    report["cancel_task_status"] = cancel.json()["status"]
    late = report_assignment(cancelled_assignment, "completed")
    report["late_completed_status"] = late.status_code
    report["late_completed_rejected"] = late.status_code == 409
    readback = client.get(f"/v1/tasks/{cancelled_task['id']}", headers=admin_auth).json()
    report["readback_status"] = readback["status"]
    report["terminal_status_preserved"] = readback["status"] == "cancelled"
    report["result_payload_fields"] = sorted(readback.keys())

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")

    ok = (report["completed_report_idempotent"] and report["late_completed_rejected"]
          and report["terminal_status_preserved"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
