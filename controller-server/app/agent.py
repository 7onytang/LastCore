"""Node agent for the built-in deterministic demonstration workloads."""

import argparse
import ctypes
import json
import math
import os
import platform
import re
import statistics
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODEL_ID = "lastcore/linear-demo-v1"
PI_ID = "lastcore/pi-monte-carlo-v1"
LOG_ID = "lastcore/log-statistics-v1"


class AssignmentStopped(RuntimeError):
    """Raised internally when an assignment becomes inactive while executing."""


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


def linear_demo(input_vector, seed, start, end, should_stop=None):
    """Run a row-sharded tanh(Wx+b) inference pass without external model files."""
    output = []
    for row in range(start, end):
        if should_stop is not None and should_stop():
            raise AssignmentStopped("assignment stopped")
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


def pi_demo(total_samples, seed, chunks, start, end, should_stop=None):
    first = total_samples * start // chunks
    last = total_samples * end // chunks
    inside = 0
    for index in range(first, last):
        if should_stop is not None and index % 256 == 0 and should_stop():
            raise AssignmentStopped("assignment stopped")
        x = _random_unit(seed, index, 0)
        y = _random_unit(seed, index, 1)
        inside += x * x + y * y <= 1.0
    return {"inside": inside, "samples": last - first}


def log_stats(lines, start, end, should_stop=None):
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
    for line in lines[start:end]:
        if should_stop is not None and should_stop():
            raise AssignmentStopped("assignment stopped")
        upper = line.upper()
        level = next((candidate for candidate in ("ERROR", "WARN", "INFO")
                      if candidate in upper), "OTHER")
        counts[level] += 1
    return counts


def measure_compute_weight(sample_count=12000, repetitions=3):
    """Estimate relative Python CPU throughput with the same work on each node."""
    rates = []
    for _ in range(repetitions):
        started = time.perf_counter()
        inside = 0
        for index in range(sample_count):
            x = _random_unit(1729, index, 0)
            y = _random_unit(1729, index, 1)
            inside += x * x + y * y <= 1.0
        elapsed = time.perf_counter() - started
        if elapsed > 0:
            rates.append(sample_count / elapsed)
    if not rates:
        return 1.0
    return round(min(1_000_000.0, max(0.001, statistics.median(rates) / 1000.0)), 6)


def system_available_memory_mb():
    """Read available physical memory on Linux, macOS, or Windows."""
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="ascii") as memory_info:
                for line in memory_info:
                    if line.startswith("MemAvailable:"):
                        return max(0, int(line.split()[1]) // 1024)
        elif system == "Darwin":
            output = subprocess.run(["vm_stat"], capture_output=True, text=True,
                                    check=True, timeout=2).stdout
            page_size_match = re.search(r"page size of (\d+) bytes", output)
            if page_size_match:
                page_size = int(page_size_match.group(1))
                pages = 0
                for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
                    line = next((line for line in output.splitlines()
                                 if line.startswith(key + ":")), None)
                    if line:
                        count = line.split(":", 1)[1].strip().rstrip(".").replace(",", "")
                        pages += int(count)
                return max(0, pages * page_size // (1024 * 1024))
        elif system == "Windows":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return max(0, status.ullAvailPhys // (1024 * 1024))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    try:
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return max(0, available_pages * page_size // (1024 * 1024))
    except (AttributeError, OSError, ValueError):
        return None


class NodeAgent:
    def __init__(self, controller, name, endpoint, registration_token, state_path,
                 available_memory_mb=None, compute_weight=None, poll_seconds=1.0,
                 max_retry_seconds=30.0):
        self.controller = controller.rstrip("/")
        self.name = name
        self.endpoint = endpoint
        self.registration_token = registration_token
        self.state_path = Path(state_path).expanduser()
        self._memory_override_mb = available_memory_mb
        self._available_memory_mb = None
        self._memory_sampled_at = 0.0
        self.compute_weight = measure_compute_weight() if compute_weight is None else compute_weight
        if not math.isfinite(self.compute_weight) or not 0 < self.compute_weight <= 1_000_000:
            raise ValueError("compute_weight must be finite, positive, and at most 1,000,000")
        self.poll_seconds = poll_seconds
        self.max_retry_seconds = max_retry_seconds
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a finite positive number")
        if not math.isfinite(max_retry_seconds) or max_retry_seconds <= 0:
            raise ValueError("max_retry_seconds must be a finite positive number")
        self.node_id = None
        self.node_token = None
        self.heartbeat_ttl_seconds = 30
        self._last_controller_contact = time.monotonic()
        self._started = set()
        self._load_or_register()

    def _current_memory_mb(self):
        memory_override = getattr(self, "_memory_override_mb",
                                  getattr(self, "available_memory_mb", None))
        if memory_override is not None:
            return memory_override
        now = time.monotonic()
        available_memory = getattr(self, "_available_memory_mb", None)
        sampled_at = getattr(self, "_memory_sampled_at", 0.0)
        if available_memory is None or now - sampled_at >= 5:
            measured = system_available_memory_mb()
            if measured is not None:
                self._available_memory_mb = min(10_000_000, max(1, measured))
            elif available_memory is None:
                self._available_memory_mb = 4096
            self._memory_sampled_at = now
        return getattr(self, "_available_memory_mb", available_memory)

    def _load_or_register(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            self.node_id = state["node_id"]
            self.node_token = state["node_token"]
            self.heartbeat_ttl_seconds = state.get("heartbeat_ttl_seconds", self.heartbeat_ttl_seconds)
            return
        state = request_json(self.controller, "/v1/nodes/register", self.registration_token, {
            "name": self.name,
            "platform": _platform_id(),
            "endpoint": self.endpoint,
            "available_memory_mb": self._current_memory_mb(),
            "compute_weight": self.compute_weight,
        }, method="POST")
        self.node_id = state["node_id"]
        self.node_token = state["node_token"]
        self.heartbeat_ttl_seconds = state.get("heartbeat_ttl_seconds", self.heartbeat_ttl_seconds)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"node_id": self.node_id, "node_token": self.node_token,
                                               "heartbeat_ttl_seconds": self.heartbeat_ttl_seconds}))
        self.state_path.chmod(0o600)

    def _watch_assignment(self, assignment, agent_stop, cancel_event, done_event):
        """Observe server-side cancellation while a workload is running."""
        assignment_id = assignment["id"]
        interval = min(self.poll_seconds, 5.0)
        while not done_event.wait(interval):
            if agent_stop.is_set() or assignment["deadline"] <= time.time():
                cancel_event.set()
                return
            try:
                # The main loop is blocked in the executor, so the watcher owns
                # the heartbeat lease until the assignment finishes.
                self._heartbeat()
                self._last_controller_contact = time.monotonic()
                response = request_json(self.controller, f"/v1/nodes/{self.node_id}/assignments",
                                        self.node_token)
                self._last_controller_contact = time.monotonic()
            except (RuntimeError, ValueError, KeyError):
                if time.monotonic() - self._last_controller_contact >= self.heartbeat_ttl_seconds:
                    cancel_event.set()
                    return
                continue
            current = next((item for item in response.get("assignments", [])
                            if item.get("id") == assignment_id), None)
            if current is None:
                continue
            if (current.get("task_status") not in ("queued", "running")
                    or current.get("status") in ("completed", "failed", "cancelled")
                    or current.get("deadline", 0) <= time.time()):
                cancel_event.set()
                return

    def _heartbeat(self):
        compute_weight = getattr(self, "compute_weight", None)
        return request_json(self.controller, f"/v1/nodes/{self.node_id}/heartbeat", self.node_token,
                            {"available_memory_mb": self._current_memory_mb(),
                             "compute_weight": compute_weight, "accepting_tasks": True},
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
        retry_delay = min(self.poll_seconds, self.max_retry_seconds)
        while not stop_event.is_set():
            try:
                self._heartbeat()
                self._last_controller_contact = time.monotonic()
                response = request_json(self.controller, f"/v1/nodes/{self.node_id}/assignments",
                                        self.node_token)
                self._last_controller_contact = time.monotonic()
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
                    cancel_event = threading.Event()
                    done_event = threading.Event()
                    watcher = threading.Thread(target=self._watch_assignment,
                                               args=(assignment, stop_event, cancel_event, done_event),
                                               name=f"lastcore-watch-{assignment_id[:8]}", daemon=True)
                    watcher.start()
                    try:
                        self._execute(assignment, cancel_event)
                    except RuntimeError:
                        self._started.discard(assignment_id)
                        raise
                    finally:
                        done_event.set()
                        watcher.join(timeout=max(1.0, self.poll_seconds + 0.1))
            except (RuntimeError, ValueError, KeyError) as exc:
                print(f"[{self.name}] {exc}", flush=True)
                stop_event.wait(retry_delay)
                retry_delay = min(retry_delay * 2, self.max_retry_seconds)
            else:
                retry_delay = min(self.poll_seconds, self.max_retry_seconds)
                stop_event.wait(self.poll_seconds)

    def _execute(self, assignment, stop_event=None):
        stop_event = stop_event or threading.Event()
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
                result = linear_demo(request["input_vector"], request["weight_seed"], start, end,
                                     stop_event.is_set)
            elif workload == "pi":
                result = pi_demo(request["total_samples"], request["weight_seed"],
                                 request["num_layers"], start, end, stop_event.is_set)
            else:
                result = log_stats(request["log_lines"], start, end, stop_event.is_set)
            if stop_event.is_set():
                return
            self._report(assignment_id, "completed", result=result)
        except AssignmentStopped:
            return
        except Exception as exc:
            if stop_event.is_set():
                return
            self._report(assignment_id, "failed", error=f"{type(exc).__name__}: {exc}"[:2048])


def main():
    parser = argparse.ArgumentParser(description="LastCore node agent")
    parser.add_argument("--controller", required=True, help="Controller base URL")
    parser.add_argument("--name", required=True, help="Display name for this node")
    parser.add_argument("--endpoint", required=True, help="Node endpoint recorded by the Controller")
    parser.add_argument("--registration-token", default=os.environ.get("CONTROLLER_REGISTRATION_TOKEN"))
    parser.add_argument("--state-file", default="~/.lastcore/node.json")
    parser.add_argument("--memory-mb", type=int, default=None,
                        help="fixed memory override; default samples available system memory")
    parser.add_argument("--compute-weight", type=float, default=None,
                        help="fixed relative CPU score; default runs a short local benchmark")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if not args.registration_token:
        parser.error("provide --registration-token or CONTROLLER_REGISTRATION_TOKEN")
    agent = NodeAgent(args.controller, args.name, args.endpoint, args.registration_token,
                      args.state_file, args.memory_mb, args.compute_weight, args.poll_seconds)
    print(f"Agent {args.name} registered as {agent.node_id}; CPU score {agent.compute_weight:.3f}, "
          f"available memory {agent._current_memory_mb()} MB; polling {args.controller}", flush=True)
    try:
        agent.run()
    except KeyboardInterrupt:
        print("Agent stopped", flush=True)


if __name__ == "__main__":
    main()
