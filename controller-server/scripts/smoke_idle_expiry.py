"""Exercise a real HTTP Controller; no API reads may trigger idle expiry."""

import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="lastcore-expiry-") as directory:
    database = directory + "/controller.sqlite3"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    admin, registration = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = {**os.environ, "CONTROLLER_ADMIN_TOKEN": admin,
           "CONTROLLER_REGISTRATION_TOKEN": registration,
           "CONTROLLER_DATABASE_PATH": database, "CONTROLLER_HEARTBEAT_TTL_SECONDS": "5",
           "CONTROLLER_EXPIRY_INTERVAL_SECONDS": "1"}
    with open(directory + "/server.log", "w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app",
                                    "--factory", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=root, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                end = time.monotonic() + 10
                while True:
                    try:
                        response = client.get("/healthz")
                        response.raise_for_status()
                        break
                    except httpx.ConnectError:
                        assert process.poll() is None, "server exited during startup"
                        assert time.monotonic() < end, "startup timeout"
                        time.sleep(0.1)
                response = client.post("/v1/nodes/register", headers={"Authorization": f"Bearer {registration}"},
                                       json={"name": "HTTP smoke", "platform": "test",
                                             "endpoint": "http://127.0.0.1:9000",
                                             "available_memory_mb": 1024, "compute_weight": 1})
                assert response.status_code == 201, response.text
                node = response.json()
                response = client.post("/v1/tasks", headers={"Authorization": f"Bearer {admin}"},
                                       json={"model_id": "lastcore/linear-demo-v1", "num_layers": 1,
                                             "layer_memory_mb": 512, "timeout_seconds": 60,
                                             "max_nodes": 1, "target_node_id": node["node_id"],
                                             "input_vector": [0.0], "weight_seed": 0})
                assert response.status_code == 201, response.text
                task = response.json()
                # No further HTTP requests: directly inspect persisted state.
                end = time.monotonic() + 10
                while True:
                    with closing(sqlite3.connect(database)) as db:
                        row = db.execute("SELECT status,error FROM tasks WHERE id=?", (task["id"],)).fetchone()
                        assignment = db.execute("SELECT status FROM assignments WHERE task_id=?", (task["id"],)).fetchone()
                    if row[0] == "failed":
                        assert row[1] == "assigned node heartbeat expired", row
                        assert assignment[0] == "failed", assignment
                        print("PASS: real Uvicorn HTTP registration/task creation; idle TTL expiry persisted without HTTP traffic")
                        break
                    assert time.monotonic() < end, "task remained active without traffic"
                    time.sleep(0.1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise
        log.seek(0)
        output = log.read()
        assert "Application shutdown complete" in output, output
        assert "ERROR" not in output and "Traceback" not in output, output
        # Uvicorn re-raises captured SIGTERM after completing ASGI shutdown.
        assert process.returncode in (0, -signal.SIGTERM), process.returncode
print("PASS: graceful shutdown completed; test process and temporary database cleaned up")
