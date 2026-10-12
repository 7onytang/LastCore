"""System-level harness for LastCore's two-node CPU MVP.

Starts the real Controller (`controller-server/`) plus two real Node Agent
processes (`node-agent/`) that each run an inference-engine ``StageServer``,
drives a request through the pipeline and collects system-level evidence:

* end-to-end latency (the whole request is one prefill forward pass, so this is
  also the TTFT; there is no decode loop or streaming yet),
* effective prefill throughput (prompt tokens / end-to-end seconds),
* per-process peak RSS sampled from ``ps``,
* per-stage compute time and parameter bytes parsed from the Node Agent log,
* wire bytes on the client->first-stage hop, counted by a transparent TCP relay
  (the A->B hop cannot be relayed: a node's registered endpoint is by design the
  address its ``StageServer`` binds, see ``inference-engine/README.md``).

Nothing in this module changes product code; it only launches and observes it.

    python -m integration.run_scenarios --help
"""

from __future__ import annotations

import ast
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = "lastcore/tiny-cpu"
STAGE_REQUEST_LOG = re.compile(r"stage served request: (?P<payload>\{.*\})")
SERVING_LOG = re.compile(r"stage \S+ serving on (?P<host>[^:\s]+):(?P<port>\d+) layers "
                         r"\[(?P<start>\d+), (?P<end>\d+)\) role=(?P<role>\w+)")
STOPPED_LOG = re.compile(r"assignment (?P<assignment>\S+) stopped locally \((?P<reason>[^)]*)\)")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_until(predicate, *, timeout: float, interval: float = 0.1, message: str = ""):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
        except Exception as exc:  # noqa: BLE001 - polling helper, keep the last reason
            last_error = exc
        else:
            if value:
                return value
        time.sleep(interval)
    raise TimeoutError(f"{message or 'condition'} not met within {timeout}s "
                       f"(last error: {last_error!r})")


def http_json(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None,
              timeout: float = 10.0) -> tuple[int, object]:
    """Minimal stdlib HTTP client so the harness needs no extra dependency."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail: object = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            detail = raw.decode("utf-8", "replace")
        return exc.code, detail


class LaunchedProcess:
    """A supervised child process with its own log and sampled peak RSS."""

    def __init__(self, name: str, argv: list[str], *, cwd: Path, env: dict[str, str],
                 log_path: Path, sample_interval: float = 0.1):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.log_path = log_path
        self.sample_interval = sample_interval
        self.process: subprocess.Popen | None = None
        self.peak_rss_kb = 0
        self._stopped = False
        self._stop_payload: dict = {}
        self.started_at: float | None = None
        self._log_handle = None
        self._sampler: threading.Thread | None = None
        self._stop_sampling = threading.Event()

    def start(self) -> "LaunchedProcess":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(self.argv, cwd=str(self.cwd), env=self.env,
                                        stdout=self._log_handle, stderr=subprocess.STDOUT,
                                        start_new_session=True, text=True)
        self.started_at = time.monotonic()
        self._sampler = threading.Thread(target=self._sample_rss, name=f"rss-{self.name}",
                                         daemon=True)
        self._sampler.start()
        return self

    def _sample_rss(self) -> None:
        while not self._stop_sampling.wait(self.sample_interval):
            if self.process is None or self.process.poll() is not None:
                return
            try:
                output = subprocess.run(["ps", "-o", "rss=", "-p", str(self.process.pid)],
                                        capture_output=True, text=True, timeout=5).stdout.strip()
                if output:
                    self.peak_rss_kb = max(self.peak_rss_kb, int(output.split()[0]))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, *, timeout: float = 10.0) -> dict:
        if self._stopped:
            return dict(self._stop_payload)
        self._stopped = True
        self._stop_sampling.set()
        payload = {"name": self.name, "log": str(self.log_path), "peak_rss_kb": self.peak_rss_kb}
        if self.process is None:
            payload["stopped"] = True
            self._stop_payload = payload
            return dict(payload)
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.process.kill()
                self.process.wait(timeout=5)
        if self._sampler is not None:
            self._sampler.join(timeout=2)
        if self._log_handle is not None:
            self._log_handle.flush()
            self._log_handle.close()
        payload["returncode"] = self.process.returncode
        payload["stopped"] = True
        self._stop_payload = payload
        return dict(payload)

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


class TCPRelay:
    """Transparent TCP relay that counts bytes per direction and can inject faults.

    ``close_after_up_bytes`` closes both sides once that many bytes travelled
    client->target, which reproduces a peer that dies mid-frame.
    """

    def __init__(self, name: str, target: tuple[str, int], *, close_after_up_bytes: int | None = None):
        self.name = name
        self.target = target
        self.close_after_up_bytes = close_after_up_bytes
        self.host = "127.0.0.1"
        self.port = free_port()
        self.up_bytes = 0
        self.down_bytes = 0
        self.connections = 0
        self.faults = 0
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def snapshot(self) -> dict:
        with self._lock:
            return {"connections": self.connections, "faults": self.faults,
                    "up_bytes": self.up_bytes, "down_bytes": self.down_bytes}

    def start(self) -> "TCPRelay":
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(self.address)
        self._server.listen(16)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, name=f"relay-{self.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self._sockets.add(client)
            threading.Thread(target=self._bridge, args=(client,), daemon=True).start()

    def _bridge(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=5)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            with self._lock:
                self.faults += 1
            client.close()
            return
        with self._lock:
            self._sockets.add(upstream)
        forwarder = threading.Thread(target=self._forward, args=(client, upstream, "up"), daemon=True)
        forwarder.start()
        self._forward(upstream, client, "down")
        forwarder.join(timeout=5)
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                self._sockets.discard(sock)

    def _forward(self, source: socket.socket, sink: socket.socket, direction: str) -> None:
        source.settimeout(0.5)
        while not self._stop.is_set():
            try:
                chunk = source.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                try:
                    sink.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            with self._lock:
                if direction == "up":
                    self.up_bytes += len(chunk)
                    if (self.close_after_up_bytes is not None
                            and self.up_bytes > self.close_after_up_bytes):
                        self.faults += 1
                        self._close_all()
                        return
                else:
                    self.down_bytes += len(chunk)
            try:
                sink.sendall(chunk)
            except OSError:
                return

    def _close_all(self) -> None:
        with self._lock:
            sockets, self._sockets = list(self._sockets), set()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> dict:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        self._close_all()
        if self._thread is not None:
            self._thread.join(timeout=2)
        return {"name": self.name, "connections": self.connections, "faults": self.faults,
                "up_bytes": self.up_bytes, "down_bytes": self.down_bytes}


NODE_SPECS = (
    {"key": "a", "memory_mb": 4096, "weight": 2.0},
    {"key": "b", "memory_mb": 2048, "weight": 1.0},
)

DEFAULT_ADMIN_TOKEN = ""  # generated per run; never hard-coded into a repository file
DEFAULT_REGISTRATION_TOKEN = ""


class LastCoreSystem:
    """Launches Controller + relay + two Node Agents and drives the pipeline."""

    def __init__(self, workdir: Path, *, controller_python: Path, node_python: Path,
                 seed: int = 7, heartbeat_ttl: int = 8, heartbeat_interval: float = 2.0,
                 poll_interval: float = 0.3, complete_after: int = 1, layer_memory_mb: int = 100,
                 max_nodes: int = 2, model_id: str = DEFAULT_MODEL_ID, node_specs=NODE_SPECS,
                 use_client_relay: bool = True):
        self.workdir = Path(workdir)
        self.controller_python = Path(controller_python)
        self.node_python = Path(node_python)
        self.seed = seed
        self.heartbeat_ttl = heartbeat_ttl
        self.heartbeat_interval = heartbeat_interval
        self.poll_interval = poll_interval
        self.complete_after = complete_after
        self.layer_memory_mb = layer_memory_mb
        self.max_nodes = max_nodes
        self.model_id = model_id
        self.node_specs = list(node_specs)
        self.use_client_relay = use_client_relay
        self.admin_token = secrets.token_urlsafe(32)
        self.registration_token = secrets.token_urlsafe(32)
        self.controller_port = free_port()
        self.config_path = self.workdir / "tiny.json"
        self.weights_path = self.workdir / "tiny.npz"
        self.controller: LaunchedProcess | None = None
        self.nodes: dict[str, LaunchedProcess] = {}
        self.node_ports: dict[str, int] = {}
        self.node_ids: dict[str, str] = {}
        self.client_relay: TCPRelay | None = None
        self.relays: list[TCPRelay] = []
        self.model_config = None
        self._started = False

    # ---------------------------------------------------------------- lifecycle
    @property
    def controller_url(self) -> str:
        return f"http://127.0.0.1:{self.controller_port}"

    def prepare(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "inference-engine"))
        sys.path.insert(0, str(REPO_ROOT / "communication"))
        from inference_engine.config import tiny_config
        from inference_engine.weights import generate_weights, save_weights

        self.workdir.mkdir(parents=True, exist_ok=True)
        self.model_config = tiny_config(self.model_id)
        self.model_config.save(self.config_path)
        save_weights(self.weights_path, generate_weights(self.model_config, seed=self.seed))

    def start(self, *, controller_db: Path | None = None) -> None:
        if self.model_config is None:
            self.prepare()
        db_path = controller_db or (self.workdir / "controller.sqlite3")
        base_env = os.environ.copy()
        controller_env = {**base_env,
                          "CONTROLLER_ADMIN_TOKEN": self.admin_token,
                          "CONTROLLER_REGISTRATION_TOKEN": self.registration_token,
                          "CONTROLLER_DATABASE_PATH": str(db_path),
                          "CONTROLLER_HEARTBEAT_TTL_SECONDS": str(self.heartbeat_ttl),
                          "PYTHONPATH": str(REPO_ROOT / "controller-server")}
        self.controller = LaunchedProcess(
            "controller",
            [str(self.controller_python), "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.controller_port)],
            cwd=REPO_ROOT / "controller-server", env=controller_env,
            log_path=self.workdir / "logs" / "controller.log").start()
        wait_until(lambda: http_json(f"{self.controller_url}/healthz")[0] == 200,
                   timeout=30, message="controller healthz")

        node_env = {**base_env,
                    "NODE_AGENT_REGISTRATION_TOKEN": self.registration_token,
                    "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "inference-engine"),
                                                   str(REPO_ROOT / "communication")])}
        for spec in self.node_specs:
            key = spec["key"]
            port = free_port()
            self.node_ports[key] = port
            argv = [str(self.node_python), "-m", "lastcore_node_agent",
                    "--controller-url", self.controller_url,
                    "--endpoint", f"http://127.0.0.1:{port}",
                    "--state-dir", str(self.workdir / f"node-{key}"),
                    "--available-memory-mb", str(spec["memory_mb"]),
                    "--compute-weight", str(spec["weight"]),
                    "--heartbeat-interval", str(self.heartbeat_interval),
                    "--poll-interval", str(self.poll_interval),
                    "--executor", "stage",
                    "--model-config", str(self.config_path),
                    "--weights", str(self.weights_path),
                    "--complete-after", str(self.complete_after)]
            self.nodes[key] = LaunchedProcess(
                f"node-{key}", argv, cwd=REPO_ROOT / "node-agent", env=node_env,
                log_path=self.workdir / "logs" / f"node-{key}.log").start()
        self._wait_for_nodes(len(self.node_specs))
        if self.use_client_relay:
            first = self.first_node_key
            self.client_relay = TCPRelay("client-to-first", ("127.0.0.1", self.node_ports[first])).start()
            self.relays.append(self.client_relay)
        self._started = True

    def _wait_for_nodes(self, expected: int) -> None:
        def registered():
            status, payload = http_json(f"{self.controller_url}/v1/nodes", token=self.admin_token)
            if status != 200:
                return None
            nodes = payload["nodes"]
            if len(nodes) < expected:
                return None
            for spec in self.node_specs:
                endpoint = f"http://127.0.0.1:{self.node_ports[spec['key']]}"
                match = [n for n in nodes if n["endpoint"].rstrip("/") == endpoint]
                if not match:
                    return None
                self.node_ids[spec["key"]] = match[0]["id"]
            return nodes

        wait_until(registered, timeout=60, message="node registration")

    @property
    def first_node_key(self) -> str:
        return self.node_specs[0]["key"]

    def wait_nodes_serving(self, keys=None, *, timeout: float = 30.0) -> None:
        """Wait for each StageServer to log that it is listening.

        Uses the Node Agent log instead of a bare TCP probe: an empty probe
        connection is accepted by ``StageServer`` and logged as a failed peer,
        which would pollute the fault evidence.
        """
        for key in (keys if keys is not None else list(self.node_ports)):
            node = self.nodes[key]
            wait_until(lambda n=node: SERVING_LOG.search(n.log_text()), timeout=timeout,
                       message=f"stage listener on node {key}")
            wait_until(lambda p=self.node_ports[key]: port_open(p), timeout=5,
                       message=f"stage port on node {key}")

    # ------------------------------------------------------------------- tasks
    def admin(self, method: str, path: str, payload: dict | None = None, token: str | None = None):
        return http_json(f"{self.controller_url}{path}", method=method,
                         token=token or self.admin_token, payload=payload)

    def create_task(self, *, num_layers: int | None = None, timeout_seconds: int = 120,
                    model_id: str | None = None) -> dict:
        layers = num_layers if num_layers is not None else self.model_config.n_layers
        status, payload = self.admin("POST", "/v1/tasks",
                                     {"model_id": model_id or self.model_id, "num_layers": layers,
                                      "layer_memory_mb": self.layer_memory_mb,
                                      "max_nodes": self.max_nodes,
                                      "timeout_seconds": timeout_seconds})
        if status != 201:
            raise RuntimeError(f"task creation failed: HTTP {status} {payload}")
        return {**payload, "task_id": payload["id"]}  # convenience alias for callers

    def get_task(self, task_id: str):
        """GET /v1/tasks/{id}; the Controller returns the task object directly."""
        return self.admin("GET", f"/v1/tasks/{task_id}")

    def wait_task_status(self, task_id: str, statuses, *, timeout: float = 60.0) -> dict:
        wanted = {statuses} if isinstance(statuses, str) else set(statuses)

        def poll():
            status, payload = self.get_task(task_id)
            if status == 200 and payload["status"] in wanted:
                return payload
            return None

        return wait_until(poll, timeout=timeout, interval=0.25,
                          message=f"task {task_id} -> {sorted(wanted)}")

    def wait_assignment_serving(self, *, timeout: float = 20.0) -> None:
        self.wait_nodes_serving(timeout=timeout)

    # --------------------------------------------------------------- inference
    def first_stage_address(self) -> tuple[str, int]:
        if self.client_relay is not None:
            return self.client_relay.address
        return ("127.0.0.1", self.node_ports[self.first_node_key])

    def run_request(self, task_id: str, prompt: str = "1 2 3 4") -> dict:
        from inference_engine import PipelineClient

        tokens = np.asarray([[int(part) for part in prompt.split()]], dtype=np.int64)
        relay_before = self._relay_snapshot()
        started = time.monotonic()
        logits = PipelineClient(self.first_stage_address(), model_id=self.model_id,
                                task_id=task_id, timeout=30.0).run(tokens, task_id=task_id)
        elapsed = time.monotonic() - started
        relay_after = self._relay_snapshot()
        return {"latency_seconds": round(elapsed, 6),
                "ttft_seconds": round(elapsed, 6),
                "prompt_tokens": int(tokens.size),
                "prefill_tokens_per_second": round(float(tokens.size) / elapsed, 2),
                "logits": np.asarray(logits),
                "wire_bytes": {name: {"up": after["up_bytes"] - before["up_bytes"],
                                      "down": after["down_bytes"] - before["down_bytes"]}
                               for name, after in relay_after.items()
                               for before in (relay_before[name],)}}

    def _relay_snapshot(self) -> dict:
        return {relay.name: relay.snapshot() for relay in self.relays}

    def reference_logits(self, prompt: str = "1 2 3 4") -> np.ndarray:
        from inference_engine.reference import forward_full
        from inference_engine.weights import load_shard

        tokens = np.asarray([[int(part) for part in prompt.split()]], dtype=np.int64)
        weights = load_shard(self.weights_path, self.model_config, 0, self.model_config.n_layers)
        return forward_full(tokens, weights, self.model_config)

    def stage_log_metrics(self) -> dict:
        metrics: dict[str, list] = {}
        for key, node in self.nodes.items():
            metrics[key] = []
            for line in node.log_text().splitlines():
                match = STAGE_REQUEST_LOG.search(line)
                if not match:
                    continue
                # The Node Agent logs the Python dict repr of the StageServer
                # result, so parse it as a literal (JSON as a fallback).
                try:
                    metrics[key].append(ast.literal_eval(match.group("payload")))
                except (ValueError, SyntaxError):
                    try:
                        metrics[key].append(json.loads(match.group("payload")))
                    except json.JSONDecodeError:
                        continue
        return metrics

    def node_stop_reasons(self) -> dict:
        reasons: dict[str, list] = {}
        for key, node in self.nodes.items():
            reasons[key] = [m.group("reason") for m in
                            (STOPPED_LOG.search(line) for line in node.log_text().splitlines()) if m]
        return reasons

    # ------------------------------------------------------------------ cleanup
    def stop(self) -> dict:
        """Terminate every child, then report peak RSS and whether ports came free.

        Process objects are kept so logs and metrics stay readable afterwards.
        """
        report: dict = {"processes": [], "relays": [], "ports_released": {}}
        for node in self.nodes.values():
            report["processes"].append(node.stop())
        if self.controller is not None:
            report["processes"].append(self.controller.stop())
        for relay in self.relays:
            report["relays"].append(relay.stop())
        for key, port in self.node_ports.items():
            report["ports_released"][f"node-{key}"] = not port_open(port)
        report["ports_released"]["controller"] = not port_open(self.controller_port)
        self.relays = []
        self._started = False
        return report

    def kill_node(self, key: str) -> dict:
        payload = self.nodes[key].stop(timeout=5)
        payload["killed"] = True
        return payload

    def restart_controller(self) -> None:
        assert self.controller is not None
        self.controller.stop()
        db_path = self.workdir / "controller.sqlite3"
        self.controller = LaunchedProcess(
            "controller",
            [str(self.controller_python), "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.controller_port)],
            cwd=REPO_ROOT / "controller-server",
            env={**os.environ.copy(),
                 "CONTROLLER_ADMIN_TOKEN": self.admin_token,
                 "CONTROLLER_REGISTRATION_TOKEN": self.registration_token,
                 "CONTROLLER_DATABASE_PATH": str(db_path),
                 "CONTROLLER_HEARTBEAT_TTL_SECONDS": str(self.heartbeat_ttl),
                 "PYTHONPATH": str(REPO_ROOT / "controller-server")},
            log_path=self.workdir / "logs" / "controller-restart.log").start()
        wait_until(lambda: http_json(f"{self.controller_url}/healthz")[0] == 200,
                   timeout=30, message="controller healthz after restart")

    def __enter__(self) -> "LastCoreSystem":
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
