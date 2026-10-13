"""Node agent for the built-in deterministic demonstration workloads."""

import argparse
import json
import math
import os
import platform
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL_ID = "lastcore/linear-demo-v1"
PI_ID = "lastcore/pi-monte-carlo-v1"
LOG_ID = "lastcore/log-statistics-v1"


def _platform_id():
    system = platform.system() or "unknown"
    if system == "Darwin":
        system = "macOS"
    machine = platform.machine() or "unknown"
    return f"{system}-{machine}"


def request_json(base_url, path, token=None, body=None, method=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read()) if response.status != 204 else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Controller returned HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Controller connection failed: {exc.reason}") from exc


def _weight(seed, row, column):
    value = (seed ^ (row * 0x9E3779B1) ^ (column * 0x85EBCA77)) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return (value / 0xFFFFFFFF) * 2.0 - 1.0


def linear_demo(input_vector, seed, start, end):
    """Run a row-sharded tanh(Wx+b) inference pass without external model files."""
    output = []
    for row in range(start, end):
        total = _weight(seed, row, len(input_vector))
        for column, value in enumerate(input_vector):
            total += _weight(seed, row, column) * value
        output.append(math.tanh(total))
    return output


def _random_unit(seed, index, stream):
    value = (seed + index * 0x9E3779B97F4A7C15 + stream * 0xD1B54A32D192ED03) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 31
    return value / 0x10000000000000000


def pi_demo(total_samples, seed, chunks, start, end):
    first = total_samples * start // chunks
    last = total_samples * end // chunks
    inside = 0
    for index in range(first, last):
        x = _random_unit(seed, index, 0)
        y = _random_unit(seed, index, 1)
        inside += x * x + y * y <= 1.0
    return {"inside": inside, "samples": last - first}


def log_stats(lines, start, end):
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
    for line in lines[start:end]:
        upper = line.upper()
        level = next((candidate for candidate in ("ERROR", "WARN", "INFO")
                      if candidate in upper), "OTHER")
        counts[level] += 1
    return counts


class NodeAgent:
    def __init__(self, controller, name, endpoint, registration_token, state_path,
                 available_memory_mb=4096, compute_weight=1.0, poll_seconds=1.0):
        self.controller = controller.rstrip("/")
        self.name = name
        self.endpoint = endpoint
        self.registration_token = registration_token
        self.state_path = Path(state_path).expanduser()
        self.available_memory_mb = available_memory_mb
        self.compute_weight = compute_weight
        self.poll_seconds = poll_seconds
        self.node_id = None
        self.node_token = None
        self._started = set()
        self._load_or_register()

    def _load_or_register(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.node_id = state["node_id"]
            self.node_token = state["node_token"]
            return
        state = request_json(self.controller, "/v1/nodes/register", self.registration_token, {
            "name": self.name,
            "platform": _platform_id(),
            "endpoint": self.endpoint,
            "available_memory_mb": self.available_memory_mb,
            "compute_weight": self.compute_weight,
        }, method="POST")
        self.node_id = state["node_id"]
        self.node_token = state["node_token"]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"node_id": self.node_id, "node_token": self.node_token}))
        self.state_path.chmod(0o600)

    def _heartbeat(self):
        return request_json(self.controller, f"/v1/nodes/{self.node_id}/heartbeat", self.node_token,
                            {"available_memory_mb": self.available_memory_mb, "accepting_tasks": True},
                            method="POST")

    def _report(self, assignment_id, status, **values):
        return request_json(
            self.controller,
            f"/v1/nodes/{self.node_id}/assignments/{assignment_id}/report",
            self.node_token,
            {"status": status, **values},
            method="POST",
        )

    def run(self, stop_event=None):
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set():
            try:
                self._heartbeat()
                response = request_json(self.controller, f"/v1/nodes/{self.node_id}/assignments",
                                        self.node_token)
                for assignment in response["assignments"]:
                    if stop_event.is_set():
                        break
                    assignment_id = assignment["id"]
                    if assignment_id in self._started:
                        continue
                    if assignment["task_status"] not in ("queued", "running"):
                        self._started.add(assignment_id)
                        continue
                    if assignment["deadline"] <= time.time():
                        self._started.add(assignment_id)
                        continue
                    if assignment["status"] in ("completed", "failed", "cancelled"):
                        self._started.add(assignment_id)
                        continue
                    self._started.add(assignment_id)
                    try:
                        self._execute(assignment)
                    except RuntimeError:
                        self._started.discard(assignment_id)
                        raise
            except (RuntimeError, ValueError, KeyError) as exc:
                print(f"[{self.name}] {exc}", flush=True)
            stop_event.wait(self.poll_seconds)

    def _execute(self, assignment):
        request = assignment["request"]
        assignment_id = assignment["id"]
        workload = request.get("workload", "linear")
        if workload == "linear" and (request.get("model_id") != MODEL_ID or request.get("input_vector") is None):
            self._report(assignment_id, "failed", error="unsupported workload; expected lastcore/linear-demo-v1")
            return
        if workload == "pi" and request.get("model_id") != PI_ID:
            self._report(assignment_id, "failed", error="unsupported pi workload")
            return
        if workload == "log_stats" and request.get("model_id") != LOG_ID:
            self._report(assignment_id, "failed", error="unsupported log_stats workload")
            return
        if workload not in ("linear", "pi", "log_stats"):
            self._report(assignment_id, "failed", error="unsupported workload")
            return
        self._report(assignment_id, "running")
        try:
            start, end = assignment["layer_start"], assignment["layer_end"]
            if workload == "linear":
                result = linear_demo(request["input_vector"], request["weight_seed"], start, end)
            elif workload == "pi":
                result = pi_demo(request["total_samples"], request["weight_seed"],
                                 request["num_layers"], start, end)
            else:
                result = log_stats(request["log_lines"], start, end)
            self._report(assignment_id, "completed", result=result)
        except Exception as exc:
            self._report(assignment_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2048])


def main():
    parser = argparse.ArgumentParser(description="LastCore node agent")
    parser.add_argument("--controller", required=True, help="Controller base URL")
    parser.add_argument("--name", required=True, help="Display name for this node")
    parser.add_argument("--endpoint", required=True, help="Node endpoint recorded by the Controller")
    parser.add_argument("--registration-token", default=os.environ.get("CONTROLLER_REGISTRATION_TOKEN"))
    parser.add_argument("--state-file", default="~/.lastcore/node.json")
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--compute-weight", type=float, default=1.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if not args.registration_token:
        parser.error("provide --registration-token or CONTROLLER_REGISTRATION_TOKEN")
    agent = NodeAgent(args.controller, args.name, args.endpoint, args.registration_token,
                      args.state_file, args.memory_mb, args.compute_weight, args.poll_seconds)
    print(f"Agent {args.name} registered as {agent.node_id}; polling {args.controller}", flush=True)
    try:
        agent.run()
    except KeyboardInterrupt:
        print("Agent stopped", flush=True)


if __name__ == "__main__":
    main()
