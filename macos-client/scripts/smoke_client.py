"""Run the production Swift ClientModel against a fresh real loopback Controller.

No user's Keychain or saved settings, no public cloud task and no mocked HTTP.
Run with the Controller development Python environment on macOS.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx


ROOT = Path(__file__).resolve().parents[2]


def wait_file(path, process):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if process.poll() is not None:
            raise RuntimeError("Swift client exited before publishing result")
        time.sleep(0.1)
    raise RuntimeError("Swift client did not publish within the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="lastcore-real-swift-") as folder:
        run = Path(folder)
        subprocess.run(["swiftc", "-parse-as-library", "-module-cache-path", str(run / "swift-cache"),
                        str(ROOT / "macos-client/Sources/LastCoreClient/ClientModel.swift"),
                        str(ROOT / "macos-client/tests/SmokeClient.swift"), "-o", str(run / "client")], check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        admin, registration = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = dict(os.environ, CONTROLLER_ADMIN_TOKEN=admin, CONTROLLER_REGISTRATION_TOKEN=registration,
                   CONTROLLER_DATABASE_PATH=str(run / "controller.sqlite3"), CONTROLLER_REAL_JOBS_ENABLED="false")
        config = run / "private.json"
        config.write_text(json.dumps({"origin": origin, "registration": registration}))
        config.chmod(0o600)
        payload = None
        try:
            with (run / "server.log").open("w") as log:
                server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                                           "--host", "127.0.0.1", "--port", str(port)],
                                          cwd=ROOT / "controller-server", env=env, stdout=log, stderr=log)
            processes.append(server)
            with httpx.Client(base_url=origin, timeout=5, trust_env=False) as api:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if api.get("/healthz").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("isolated Controller failed to start")
                    time.sleep(0.1)
                with (run / "client.log").open("w") as log:
                    client = subprocess.Popen([str(run / "client"), str(run)], stdout=log, stderr=log)
                processes.append(client)
                node = wait_file(run / "ready.json", client)
                request = dict(model_id="lastcore/linear-demo-v1", num_layers=2, layer_memory_mb=1,
                               max_nodes=1, timeout_seconds=120, input_vector=[1, 2, 3, 4], weight_seed=7,
                               target_node_id=node["node_id"])
                response = api.post("/v1/tasks", headers={"Authorization": "Bearer " + admin}, json=request)
                response.raise_for_status()
                task_id = response.json()["id"]
                result = wait_file(run / "result.json", client)
                final = api.get("/v1/tasks/" + task_id, headers={"Authorization": "Bearer " + admin})
                final.raise_for_status()
                final = final.json()
                spec = importlib.util.spec_from_file_location("linear_reference", ROOT / "controller-server/tests/test_targeted_linear.py")
                sys.path.insert(0, str(ROOT / "controller-server"))
                reference_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(reference_module)
                expected = [reference_module.swift_reference(7, row, request["input_vector"]) for row in range(2)]
                assert result["task_id"] == task_id
                assert final["status"] == "completed" and final["result"] == result["result"]
                assert len(final["assignments"]) == 1 and final["assignments"][0]["node_id"] == node["node_id"]
                error = max(abs(a - b) for a, b in zip(expected, result["result"]))
                assert len(result["result"]) == 2 and error < 1e-12
                payload = dict(passed=True, scope=result["scope"], task_id=task_id, node_id=node["node_id"],
                               request=request, result=result["result"], reference=expected, max_abs_diff=error,
                               task_status=final["status"], assignment_status=final["assignments"][0]["status"],
                               ui_observable_result_published=result["ui_observable_result_published"],
                               client_model_sha256=hashlib.sha256((ROOT / "macos-client/Sources/LastCoreClient/ClientModel.swift").read_bytes()).hexdigest(),
                               actual_user_macbook_pro=False, actual_gui_screenshot=False,
                               keychain_and_user_settings_untouched=True)
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        payload["owned_processes_exited"] = all(p.poll() is not None for p in processes)
        Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
