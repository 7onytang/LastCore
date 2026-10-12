"""Persistent internal demo control for LastCore's two-node CPU MVP.

Unlike ``integration.run_scenarios`` (one shot, always cleans up), these
commands launch the real Controller and two real Node Agent processes and keep
them running so a user can talk to the system by hand; a separate ``stop``
command reclaims exactly the PIDs this demo started.

    python -m integration.demo setup     # create the per-module virtualenvs
    python -m integration.demo start     # Controller + node-a + node-b (detached)
    python -m integration.demo status    # recorded PIDs, liveness, listening ports
    python -m integration.demo request   # HTTP task + TCP pipeline vs reference
    python -m integration.demo stop      # ownership-checked termination + port check

The demo never changes product code; it launches and observes the existing
Controller, Node Agent and inference-engine StageServer. Everything it writes
lives under ``--workdir`` (default ``integration/demo-run``, gitignored). The
state file holds the per-run bearer tokens and is created with mode 0600; the
tokens are never printed, logged or committed. Metrics use the same wording as
``integration/README.md``: one request is a single full-forward / prefill pass,
so the wall clock is a prefill round trip, not a real TTFT, and tokens/s is
prefill throughput, not a decode rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = "lastcore/tiny-cpu"
STATE_NAME = "demo-state.json"
RESULT_NAME = "demo-result.json"
CONTROLLER_VENV = REPO_ROOT / ".venv-controller"
NODE_VENV = REPO_ROOT / ".venv-node"
INTEGRATION_VENV = REPO_ROOT / ".venv-integration"
SERVING_LOG = re.compile(r"stage \S+ serving on (?P<host>[^:\s]+):(?P<port>\d+) layers "
                         r"\[(?P<start>\d+), (?P<end>\d+)\) role=(?P<role>\w+)")

# ``setup`` must run under a bare python3, so the runtime imports stay optional.
try:
    import numpy as np

    sys.path.insert(0, str(REPO_ROOT / "inference-engine"))
    sys.path.insert(0, str(REPO_ROOT / "communication"))
    from integration.lastcore_harness import (TCPRelay, free_port, http_json,  # noqa: E402
                                              port_open, wait_until)
    HAVE_RUNTIME = True
except ImportError:  # a bare interpreter: only `setup` is available
    HAVE_RUNTIME = False


# --------------------------------------------------------------------------
# process / port helpers
# --------------------------------------------------------------------------
def ps_info(pid: int) -> dict:
    """Return pid/pgid/command/lstart/state as observed by ``ps`` right now."""
    try:
        out = subprocess.run(["ps", "-ww", "-o", "pid=,pgid=,lstart=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"alive": False}
    if not out:
        return {"alive": False}
    # "  PID  PGID Mon Oct 12 04:24:50 2026  <command...>"
    parts = out.split(None, 7)
    if len(parts) < 8:
        return {"alive": False}
    pid_s, pgid_s, rest = parts[0], parts[1], parts[2:]
    lstart = " ".join(rest[:5])
    command = rest[5]
    return {"alive": True, "pid": int(pid_s), "pgid": int(pgid_s),
            "lstart": lstart, "command": command}


def pid_listening_ports(pid: int) -> list[int]:
    """TCP ports the given PID currently LISTENs on, via lsof (empty if unknown)."""
    try:
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        if line.startswith("n") and ":" in line:
            tail = line.rsplit(":", 1)[-1]
            if tail.isdigit():
                ports.add(int(tail))
    return sorted(ports)


def owned_by_record(rec: dict) -> tuple[bool, str]:
    """Verify a recorded process is still the one we started (never kill by name)."""
    info = ps_info(int(rec["pid"]))
    if not info.get("alive"):
        return False, "not running"
    if info["pgid"] != int(rec["pgid"]):
        return False, f"pgid changed ({info['pgid']} != {rec['pgid']})"
    if info["lstart"] != rec["lstart"]:
        return False, f"start time changed ({info['lstart']!r} != {rec['lstart']!r})"
    marker = rec.get("marker", "")
    if marker and marker not in info["command"]:
        return False, f"command no longer contains {marker!r}"
    return True, "owned"


def launch(name: str, argv: list[str], *, cwd: Path, env: dict, log_path: Path,
           port: int, marker: str) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=handle,
                               stderr=subprocess.STDOUT, start_new_session=True, text=True)
    handle.close()
    info = ps_info(process.pid)
    if not info.get("alive"):
        raise RuntimeError(f"{name} exited immediately; see {log_path}")
    return {"name": name, "pid": process.pid, "pgid": info["pgid"], "lstart": info["lstart"],
            "argv": argv, "cwd": str(cwd), "log": str(log_path), "port": port,
            "marker": marker, "command": info["command"]}


def kill_record(rec: dict, *, timeout: float = 10.0) -> dict:
    """SIGTERM then SIGKILL the recorded process group after an ownership check."""
    result = {"name": rec["name"], "pid": rec["pid"], "port": rec["port"]}
    ok, reason = owned_by_record(rec)
    result["ownership"] = reason
    if not ok:
        # A PID that is already gone is reclaimed; a live PID that is not ours is left alone.
        result["terminated"] = reason == "not running"
        return result
    try:
        os.killpg(int(rec["pgid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(int(rec["pid"]), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            result["terminated"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ps_info(int(rec["pid"])).get("alive"):
            result["terminated"] = True
            result["signal"] = "SIGTERM"
            return result
        time.sleep(0.2)
    try:
        os.killpg(int(rec["pgid"]), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    time.sleep(0.5)
    result["terminated"] = not ps_info(int(rec["pid"])).get("alive")
    result["signal"] = "SIGKILL"
    return result


def read_state(workdir: Path) -> dict:
    path = workdir / STATE_NAME
    if not path.exists():
        raise SystemExit(f"no demo state at {path}; run `python -m integration.demo start` first")
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(workdir: Path, state: dict) -> Path:
    path = workdir / STATE_NAME
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# --------------------------------------------------------------------------
# setup: per-module virtualenvs (their pytest pins conflict)
# --------------------------------------------------------------------------
def venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def ensure_venvs() -> dict:
    plan = [
        (CONTROLLER_VENV, REPO_ROOT / "controller-server" / "requirements-dev.txt", ()),
        (NODE_VENV, REPO_ROOT / "node-agent" / "requirements-dev.txt", ("numpy==2.5.4",)),
        (INTEGRATION_VENV, None, ("numpy==2.5.4",)),
    ]
    results = {}
    for venv, reqs, extra in plan:
        python = venv_python(venv)
        if not python.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        if reqs is not None:
            subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(reqs)], check=True)
        if extra:
            # node-agent/requirements.txt does not pin numpy even though its stage
            # executor imports it; the integration README adds it by hand.
            subprocess.run([str(python), "-m", "pip", "install", "-q", *extra], check=True)
        results[venv.name] = str(python)
    return results


def cmd_setup(args) -> int:
    print(json.dumps({"setup": ensure_venvs()}, indent=2))
    return 0


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------
def cmd_start(args) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    state_path = workdir / STATE_NAME
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        live = [r for r in state["processes"] if ps_info(int(r["pid"])).get("alive")]
        if live and not args.force:
            print(json.dumps({"error": "demo already running",
                              "live": [r["name"] for r in live],
                              "hint": "run `stop` first or pass --force"}, indent=2))
            return 1
        for rec in live:
            kill_record(rec)
    # A start is always a fresh system: free ports change, so persisted node
    # credentials and the previous controller database would point at dead ports.
    for key in ("a", "b"):
        shutil.rmtree(workdir / f"node-{key}", ignore_errors=True)
    for suffix in ("", "-wal", "-shm"):
        Path(str(workdir / "controller.sqlite3") + suffix).unlink(missing_ok=True)
    shutil.rmtree(workdir / "logs", ignore_errors=True)
    ensure_venvs()
    workdir.mkdir(parents=True, exist_ok=True)

    from inference_engine.config import tiny_config
    from inference_engine.weights import generate_weights, save_weights

    config_path = workdir / "tiny.json"
    weights_path = workdir / "tiny.npz"
    model_config = tiny_config(DEFAULT_MODEL_ID)
    model_config.save(config_path)
    save_weights(weights_path, generate_weights(model_config, seed=args.seed))

    admin_token = secrets.token_urlsafe(32)
    registration_token = secrets.token_urlsafe(32)
    controller_port = free_port()
    controller_url = f"http://127.0.0.1:{controller_port}"
    base_env = os.environ.copy()

    controller = launch(
        "controller",
        [str(venv_python(CONTROLLER_VENV)), "-m", "uvicorn", "app.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(controller_port)],
        cwd=REPO_ROOT / "controller-server",
        env={**base_env, "CONTROLLER_ADMIN_TOKEN": admin_token,
             "CONTROLLER_REGISTRATION_TOKEN": registration_token,
             "CONTROLLER_DATABASE_PATH": str(workdir / "controller.sqlite3"),
             "CONTROLLER_HEARTBEAT_TTL_SECONDS": str(args.heartbeat_ttl),
             "PYTHONPATH": str(REPO_ROOT / "controller-server")},
        log_path=workdir / "logs" / "controller.log", port=controller_port,
        marker=f"--port {controller_port}")
    try:
        wait_until(lambda: http_json(f"{controller_url}/healthz")[0] == 200, timeout=30,
                   message="controller healthz")
    except TimeoutError:
        kill_record(controller)
        raise SystemExit(f"controller did not become healthy; see {controller['log']}")

    node_env = {**base_env, "NODE_AGENT_REGISTRATION_TOKEN": registration_token,
                "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "inference-engine"),
                                               str(REPO_ROOT / "communication")])}
    specs = ({"key": "a", "memory_mb": 4096, "weight": 2.0},
             {"key": "b", "memory_mb": 2048, "weight": 1.0})
    processes = [controller]
    nodes: dict[str, dict] = {}
    try:
        for spec in specs:
            key = spec["key"]
            port = free_port()
            state_dir = workdir / f"node-{key}"
            rec = launch(
                f"node-{key}",
                [str(venv_python(NODE_VENV)), "-m", "lastcore_node_agent",
                 "--controller-url", controller_url, "--endpoint", f"http://127.0.0.1:{port}",
                 "--state-dir", str(state_dir), "--available-memory-mb", str(spec["memory_mb"]),
                 "--compute-weight", str(spec["weight"]), "--heartbeat-interval", "2",
                 "--poll-interval", "0.3", "--executor", "stage",
                 "--model-config", str(config_path), "--weights", str(weights_path),
                 "--complete-after", str(args.complete_after)],
                cwd=REPO_ROOT / "node-agent", env=node_env,
                log_path=workdir / "logs" / f"node-{key}.log", port=port,
                marker=f"--endpoint http://127.0.0.1:{port}")
            rec["endpoint"] = f"http://127.0.0.1:{port}"
            nodes[key] = rec
            processes.append(rec)
        _wait_registered(controller_url, admin_token, nodes)
    except BaseException:
        for rec in reversed(processes):
            kill_record(rec, timeout=5)
        raise

    state = {"version": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "workdir": str(workdir), "repo": str(REPO_ROOT), "model_id": DEFAULT_MODEL_ID,
             "controller_url": controller_url, "controller_port": controller_port,
             "tokens": {"admin": admin_token, "registration": registration_token},
             "model": {"config": str(config_path), "weights": str(weights_path),
                       "n_layers": model_config.n_layers, "seed": args.seed},
             "processes": processes, "nodes": {k: v for k, v in nodes.items()}}
    write_state(workdir, state)

    summary = {"status": "started", "state": str(workdir / STATE_NAME),
               "controller_url": controller_url,
               "processes": [{k: r[k] for k in ("name", "pid", "pgid", "port", "log")}
                             for r in processes],
               "listening": {r["name"]: pid_listening_ports(int(r["pid"])) for r in processes},
               "next": "python -m integration.demo request"}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _wait_registered(controller_url: str, admin_token: str, nodes: dict) -> None:
    def registered():
        status, payload = http_json(f"{controller_url}/v1/nodes", token=admin_token)
        if status != 200:
            return None
        found = payload["nodes"]
        for key, rec in nodes.items():
            match = [n for n in found if n["endpoint"].rstrip("/") == rec["endpoint"]]
            if not match:
                return None
            rec["node_id"] = match[0]["id"]
        return found

    wait_until(registered, timeout=60, message="node registration")


def _wait_serving(workdir: Path, nodes: dict, *, timeout: float = 30.0) -> None:
    # Wait on the Node Agent log and the OS listener table; do NOT open a bare TCP
    # probe, which StageServer accepts and logs as a failed peer (fault-matrix noise).
    for key, rec in nodes.items():
        log = Path(rec["log"])
        wait_until(lambda p=log: SERVING_LOG.search(p.read_text(encoding="utf-8", errors="replace")),
                   timeout=timeout, message=f"stage listener on node {key}")
        wait_until(lambda r=rec: r["port"] in pid_listening_ports(int(r["pid"])), timeout=5,
                   message=f"stage port on node {key}")


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    state = read_state(workdir)
    rows = []
    for rec in state["processes"]:
        ok, reason = owned_by_record(rec)
        listening = pid_listening_ports(int(rec["pid"])) if ok else []
        rows.append({"name": rec["name"], "pid": rec["pid"], "port": rec["port"],
                     "recorded_port_listening": rec["port"] in listening,
                     "listening_ports": listening,
                     "ownership": reason})
    print(json.dumps({"controller_url": state["controller_url"], "processes": rows},
                     indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# request: HTTP task + TCP pipeline vs single-machine reference
# --------------------------------------------------------------------------
class _RssSampler:
    def __init__(self, pids: list[int], interval: float = 0.1):
        self.pids = pids
        self.interval = interval
        self.peak = {pid: 0 for pid in pids}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        # A request is over in a few ms, far below any polling interval, so take a
        # baseline sample synchronously; the thread only refines it if it runs.
        self._sample_once()

    def _sample_once(self):
        for pid in self.pids:
            try:
                out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True,
                                     text=True, timeout=5).stdout.strip()
                if out:
                    self.peak[pid] = max(self.peak[pid], int(out.split()[0]))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue

    def _run(self):
        while not self._stop.wait(self.interval):
            self._sample_once()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(timeout=2)


def _sha256(array) -> str:
    return hashlib.sha256(np.ascontiguousarray(np.asarray(array, dtype=np.float32)).tobytes()).hexdigest()


def cmd_request(args) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    state = read_state(workdir)
    admin = state["tokens"]["admin"]
    controller_url = state["controller_url"]
    nodes = state["nodes"]

    status, _ = http_json(f"{controller_url}/healthz")
    if status != 200:
        raise SystemExit(f"controller not healthy (HTTP {status}); run `start` first")

    for key, rec in nodes.items():
        ok, reason = owned_by_record(rec)
        if not ok:
            raise SystemExit(f"node-{key} is not the recorded process ({reason})")

    status, payload = http_json(f"{controller_url}/v1/nodes", token=admin)
    registered = payload["nodes"] if status == 200 else []

    from inference_engine.config import ModelConfig

    model_config = ModelConfig.load(state["model"]["config"])
    n_layers = model_config.n_layers

    create_status, task = http_json(
        f"{controller_url}/v1/tasks", method="POST", token=admin,
        payload={"model_id": state["model_id"], "num_layers": n_layers, "layer_memory_mb": 100,
                 "max_nodes": 2, "timeout_seconds": args.timeout})
    if create_status != 201:
        raise SystemExit(f"task creation failed: HTTP {create_status} {task}")
    task_id = task["id"]
    assignments = [{"node": a["node_id"], "layers": [a["layer_start"], a["layer_end"]],
                    "upstream": bool(a.get("previous_node")), "downstream": bool(a.get("next_node"))}
                   for a in task["assignments"]]

    wait_until(lambda: http_json(f"{controller_url}/v1/tasks/{task_id}", token=admin)[1]["status"]
               == "running", timeout=30, message="task running")
    _wait_serving(workdir, nodes)

    # The registration endpoint must be the address the StageServer really binds:
    # compare the Controller's registered endpoint, the recorded port and the
    # OS-level listeners of each recorded PID while the stages are serving.
    endpoint_check = {}
    for key, rec in nodes.items():
        match = [n for n in registered if n["endpoint"].rstrip("/") == rec["endpoint"]]
        listening = pid_listening_ports(int(rec["pid"]))
        endpoint_check[key] = {"registered_endpoint": match[0]["endpoint"] if match else None,
                               "recorded_port": rec["port"],
                               "os_listening_ports": listening,
                               "consistent": bool(match) and rec["port"] in listening}

    pids = [int(rec["pid"]) for rec in state["processes"]]
    pids_by_name = {rec["name"]: int(rec["pid"]) for rec in state["processes"]}
    first_key = "a"
    relay = TCPRelay("client-to-first", ("127.0.0.1", nodes[first_key]["port"])).start()
    from inference_engine import PipelineClient
    from inference_engine.reference import forward_full
    from inference_engine.weights import load_shard

    tokens = np.asarray([[int(part) for part in args.prompt.split()]], dtype=np.int64)
    try:
        with _RssSampler(pids) as sampler:
            started = time.monotonic()
            logits = PipelineClient(relay.address, model_id=state["model_id"],
                                    task_id=task_id, timeout=30.0).run(tokens, task_id=task_id)
            elapsed = time.monotonic() - started
            peaks = dict(sampler.peak)
    finally:
        relay.stop()

    weights = load_shard(state["model"]["weights"], model_config, 0, model_config.n_layers)
    reference = forward_full(tokens, weights, model_config)
    bit_identical = bool(np.array_equal(np.asarray(logits), np.asarray(reference)))

    terminal = wait_until(
        lambda: (lambda s, p: p if s == 200 and p["status"] in ("completed", "failed") else None)(
            *http_json(f"{controller_url}/v1/tasks/{task_id}", token=admin)),
        timeout=60, message="task terminal").get("status")

    relay_snapshot = {"up_bytes": relay.up_bytes, "down_bytes": relay.down_bytes,
                      "connections": relay.connections}
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workdir": str(workdir), "repo": str(REPO_ROOT), "model_id": state["model_id"],
        "prompt": args.prompt, "prompt_tokens": int(tokens.size),
        "model": {"config": state["model"]["config"], "weights": state["model"]["weights"],
                  "n_layers": model_config.n_layers, "seed": state["model"].get("seed")},
        "controller_url": controller_url, "task_id": task_id, "assignments": assignments,
        "endpoint_vs_listening": endpoint_check,
        "pipe": "HTTP POST /v1/tasks (Controller scheduling) then PipelineClient tokens over TCP "
                "-> stage a -> stage b -> logits (data plane); the two are separate paths",
        "pipeline": {"shape": list(np.asarray(logits).shape), "logits_sha256": _sha256(logits),
                     "next_token": int(np.argmax(np.asarray(logits)[0, -1]))},
        "reference": {"shape": list(np.asarray(reference).shape),
                      "logits_sha256": _sha256(reference),
                      "next_token": int(np.argmax(np.asarray(reference)[0, -1]))},
        "bit_identical": bit_identical,
        "latency_seconds": round(elapsed, 6),
        "prefill_tokens_per_second": round(float(tokens.size) / elapsed, 2),
        "metric_scope": "one full-forward/prefill round trip; not a real TTFT, and tokens/s is "
                        "prefill throughput, not a decode rate; peak_rss_mb is a ps sample. The "
                        "stage-ready wait uses lsof, not a bare TCP probe, so latency excludes "
                        "the ~0.25s serve_poll a probe would trigger",
        "peak_rss_mb": {name: round(peaks.get(pid, 0) / 1024, 1)
                        for name, pid in pids_by_name.items()},
        "wire_bytes_client_to_first_hop": relay_snapshot,
        "task_terminal_status": terminal,
        "ports_released_after_request": {"relay": not port_open(relay.port)},
        "pass": bool(bit_identical and terminal == "completed"
                     and all(v["consistent"] for v in endpoint_check.values())),
    }
    out = workdir / RESULT_NAME
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result["result_file"] = str(out)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["pass"] else 1


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------
def cmd_stop(args) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    state = read_state(workdir)
    order = [r for r in state["processes"] if r["name"].startswith("node-")] + \
            [r for r in state["processes"] if r["name"] == "controller"]
    terminations = [kill_record(rec) for rec in order]
    time.sleep(0.5)
    released = {}
    for rec in state["processes"]:
        listening = pid_listening_ports(int(rec["pid"]))
        released[rec["name"]] = {"port": rec["port"], "port_closed": not port_open(rec["port"]),
                                 "still_listening": listening}
    state["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_state(workdir, state)
    summary = {"terminations": terminations, "reclaimed": released,
               "all_ports_released": all(v["port_closed"] for v in released.values())}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["all_ports_released"] and all(t["terminated"] for t in terminations) else 1


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="integration.demo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default=str(REPO_ROOT / "integration" / "demo-run"))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="create the per-module virtualenvs").set_defaults(func=cmd_setup)

    start = sub.add_parser("start", help="launch Controller + two Node Agents (detached)")
    start.add_argument("--seed", type=int, default=7)
    start.add_argument("--heartbeat-ttl", type=int, default=8)
    start.add_argument("--complete-after", type=int, default=1,
                       help="served requests before a node reports completed (0 = serve forever)")
    start.add_argument("--force", action="store_true", help="start even if a live demo is recorded")
    start.set_defaults(func=cmd_start)

    sub.add_parser("status", help="show recorded PIDs and listening ports").set_defaults(func=cmd_status)

    request = sub.add_parser("request", help="HTTP task + TCP pipeline request vs reference")
    request.add_argument("--prompt", default="1 2 3 4")
    request.add_argument("--timeout", type=int, default=120)
    request.add_argument("--seed", type=int, default=7)
    request.set_defaults(func=cmd_request)

    sub.add_parser("stop", help="ownership-checked termination and port reclaim").set_defaults(func=cmd_stop)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "setup" and not HAVE_RUNTIME:
        venv_python_now = INTEGRATION_VENV / "bin" / "python"
        print("this command needs numpy and the harness modules; run it with the integration "
              f"venv:\n  {venv_python_now} -m integration.demo {args.command}\n"
              f"(create it first with: python3 -m integration.demo setup)", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
