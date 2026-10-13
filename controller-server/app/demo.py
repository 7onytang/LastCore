"""Run a complete local Controller + multi-node inference demo."""

import json
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from .agent import MODEL_ID, NodeAgent, linear_demo, request_json
from .main import Settings, create_app


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    admin_token = secrets.token_urlsafe(32)
    registration_token = secrets.token_urlsafe(32)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    temporary = tempfile.TemporaryDirectory(prefix="lastcore-demo-")
    app = create_app(Settings(admin_token, registration_token,
                              str(Path(temporary.name) / "controller.sqlite3")))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    agents = []
    agent_threads = []
    stop = threading.Event()
    try:
        server_thread.start()
        for _ in range(100):
            try:
                request_json(base_url, "/healthz")
                break
            except RuntimeError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Controller did not become ready")

        for index, weight in enumerate((1.0, 1.5, 2.0), start=1):
            agent = NodeAgent(base_url, f"Demo Node {index}", base_url, registration_token,
                               Path(temporary.name) / f"node-{index}.json", 4096, weight, 0.15)
            agents.append(agent)
            thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
            thread.start()
            agent_threads.append(thread)

        admin = {"Authorization": f"Bearer {admin_token}"}
        input_vector = [0.25, -0.5, 1.0, 0.75, -0.125]
        from urllib.request import Request, urlopen

        body = json.dumps({
            "model_id": MODEL_ID,
            "num_layers": 12,
            "layer_memory_mb": 1,
            "max_nodes": 3,
            "timeout_seconds": 60,
            "input_vector": input_vector,
            "weight_seed": 2026,
        }).encode()
        request = Request(base_url + "/v1/tasks", data=body, headers={
            **admin, "Content-Type": "application/json",
        }, method="POST")
        with urlopen(request, timeout=10) as response:
            task = json.loads(response.read())

        print(f"Task: {task['id']}")
        print(f"Nodes: {len(agents)} | output units: 12 | model: {MODEL_ID}")
        deadline = time.time() + 30
        while time.time() < deadline:
            current = request_json(base_url, f"/v1/tasks/{task['id']}", admin_token)
            if current["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Demo task timed out")
        if current["status"] != "completed":
            raise RuntimeError(f"Demo task ended as {current['status']}: {current['error']}")

        expected = linear_demo(input_vector, 2026, 0, 12)
        actual = current["result"]
        error = max(abs(a - b) for a, b in zip(expected, actual))
        print("Assignment ranges:")
        for assignment in current["assignments"]:
            node = next(a.name for a in agents if a.node_id == assignment["node_id"])
            print(f"  {node}: [{assignment['layer_start']}, {assignment['layer_end']}) "
                  f"{assignment['status']}")
        print("Aggregated output:", [round(value, 6) for value in actual])
        print(f"Single-node comparison max error: {error:.12g}")
        if error > 1e-12:
            raise RuntimeError("Distributed output does not match the single-node result")
        print("DEMO PASS: distributed inference completed and matched the reference result")
    finally:
        stop.set()
        for thread in agent_threads:
            thread.join(timeout=2)
        server.should_exit = True
        server_thread.join(timeout=3)
        temporary.cleanup()


if __name__ == "__main__":
    main()
