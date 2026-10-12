#!/usr/bin/env python3
"""Self-contained smoke test for the packaged macOS node-agent bundle.

Runs the bundled executables against a controlled local fixture -- an
in-process mock Controller bound to 127.0.0.1 (NOT the real controller-server
deployment) -- and verifies the documented bundle semantics end to end:

  1. both executables answer ``--help`` with exit code 0
  2. bundled preflight passes with env file and state dir under a path
     containing spaces
  3. agent registers (registration token supplied only via the env file),
     sends heartbeats and polls assignments
  4. one demo assignment is executed and reported completed
  5. the state lock is held while the agent runs
  6. SIGINT stops the agent gracefully with exit code 0
  7. the state lock is reacquirable after exit
  8. the registration/node tokens never appear in captured stdout/stderr

Exit code 0 = all steps passed; otherwise 1 with a JSON summary on stdout.
Usage: python smoke_bundle.py [--bundle-dir DIR] [--keep]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REGISTRATION_TOKEN = "registration-smoke-" + "a" * 24


class MockController:
    """Controlled fixture standing in for the controller-server API."""

    def __init__(self):
        self.lock = threading.Lock()
        self.registration_token = REGISTRATION_TOKEN
        self.node_token = None
        self.register_calls = 0
        self.heartbeats = 0
        self.polls = 0
        self.reports = []  # (assignment_id, status)
        self.assignment_reported_running = False

    def assignment_row(self, now: float) -> dict:
        return {
            "id": "asg-smoke-1",
            "task_id": "task-smoke-1",
            "node_id": "node-smoke-1",
            "position": 0,
            "layer_start": 0,
            "layer_end": 4,
            "previous_node": None,
            "next_node": None,
            "status": "pending" if not self.assignment_reported_running else "running",
            "error": None,
            "task_status": "queued" if not self.assignment_reported_running else "running",
            "request": {"model_id": "smoke-model", "num_layers": 4, "layer_memory_mb": 512},
            "deadline": now + 300.0,
        }

    def handler(self):  # noqa: C901
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _authorized(self, expected: str) -> bool:
                return self.headers.get("Authorization", "") == f"Bearer {expected}"

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                with outer.lock:
                    if self.path == "/healthz":
                        self._send(200, {"status": "ok"})
                        return
                    if re.fullmatch(r"/v1/nodes/[^/]+/assignments", self.path):
                        if not outer.node_token or not self._authorized(outer.node_token):
                            self._send(401, {"detail": "invalid node credential"})
                            return
                        outer.polls += 1
                        rows = [] if outer.reports and outer.reports[-1][1] == "completed" \
                            else [outer.assignment_row(time.time())]
                        self._send(200, {"assignments": rows})
                        return
                self._send(404, {"detail": "not found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                with outer.lock:
                    if self.path == "/v1/nodes/register":
                        if not self._authorized(outer.registration_token):
                            self._send(401, {"detail": "invalid registration credential"})
                            return
                        outer.register_calls += 1
                        outer.node_token = "node-token-smoke-" + "b" * 24
                        self._send(201, {"node_id": "node-smoke-1",
                                         "node_token": outer.node_token,
                                         "heartbeat_ttl_seconds": 30})
                        return
                    if re.fullmatch(r"/v1/nodes/[^/]+/heartbeat", self.path):
                        if not outer.node_token or not self._authorized(outer.node_token):
                            self._send(401, {"detail": "invalid node credential"})
                            return
                        outer.heartbeats += 1
                        self._send(200, {"status": "available"})
                        return
                    if re.fullmatch(r"/v1/nodes/[^/]+/assignments/[^/]+/report", self.path):
                        if not outer.node_token or not self._authorized(outer.node_token):
                            self._send(401, {"detail": "invalid node credential"})
                            return
                        status = json.loads(body.decode("utf-8") or "{}").get("status")
                        outer.reports.append((self.path.split("/")[5], status))
                        if status == "running":
                            outer.assignment_reported_running = True
                        self._send(200, {"status": "completed"})
                        return
                self._send(404, {"detail": "not found"})

            def log_message(self, *args):  # silence fixture request logging
                pass

        return Handler


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout: float, what: str, results: dict) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            results[f"wait_{what}"] = "ok"
            return
        time.sleep(0.1)
    results[f"wait_{what}"] = "TIMEOUT"
    raise TimeoutError(f"timed out waiting for {what}")


def spawn_reader(pipe, sink: list) -> threading.Thread:
    def read_all():
        for line in iter(pipe.readline, ""):
            sink.append(line)

    thread = threading.Thread(target=read_all, daemon=True)
    thread.start()
    return thread


def try_flock(state_dir: Path) -> bool:
    """Return True if agent.lock can be exclusively locked (i.e. is free)."""
    handle = (state_dir / "agent.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        handle.close()


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir",
                        default=str(here.parent / "dist" / "lastcore-node-agent"))
    parser.add_argument("--keep", action="store_true",
                        help="keep the smoke work directory (default: removed on exit)")
    args = parser.parse_args()

    bundle = Path(args.bundle_dir).resolve()
    agent_exe = bundle / "lastcore-node-agent"
    preflight_exe = bundle / "lastcore-preflight"
    results: dict = {"bundle_dir": str(bundle)}
    failures: list[str] = []

    def check(step: str, ok: bool, detail) -> None:
        results[step] = detail if ok else f"FAIL: {detail}"
        if not ok:
            failures.append(step)

    workdir = Path(tempfile.mkdtemp(prefix="lastcore-bundle-smoke "))
    # NOTE: workdir contains a space by construction (prefix above).
    results["workdir"] = str(workdir)
    state_dir = workdir / "agent state"
    env_file = workdir / "agent env file"

    fixture = MockController()
    server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.handler())
    controller_port = server.server_address[1]
    results["controller_fixture"] = f"http://127.0.0.1:{controller_port} (local mock, not real controller-server)"

    endpoint_port = free_loopback_port()
    env_values = {
        "NODE_AGENT_CONTROLLER_URL": f"http://127.0.0.1:{controller_port}",
        "NODE_AGENT_ENDPOINT": f"127.0.0.1:{endpoint_port}",
        "NODE_AGENT_AVAILABLE_MEMORY_MB": "2048",
        "NODE_AGENT_NAME": "smoke-bundle-node",
        "NODE_AGENT_STATE_DIR": str(state_dir),
        "NODE_AGENT_HEARTBEAT_INTERVAL": "1",
        "NODE_AGENT_POLL_INTERVAL": "0.5",
        "NODE_AGENT_REQUEST_TIMEOUT": "2",
        "NODE_AGENT_REGISTRATION_TOKEN": REGISTRATION_TOKEN,
    }
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env_values.items()), encoding="utf-8")
    env_file.chmod(0o600)

    agent_out: list[str] = []
    agent_err: list[str] = []
    preflight_out: list[str] = []
    agent_proc: subprocess.Popen | None = None
    server_threads = threading.Thread(target=server.serve_forever, daemon=True)
    server_threads.start()

    try:
        # 1. --help on both executables
        help_agent = subprocess.run([str(agent_exe), "--help"], capture_output=True, text=True, timeout=30)
        check("agent_help_rc", help_agent.returncode == 0, f"rc={help_agent.returncode} stderr={help_agent.stderr[:200]}")
        check("agent_help_usage", "usage:" in help_agent.stdout.lower(), help_agent.stdout[:120])
        help_preflight = subprocess.run([str(preflight_exe), "--help"], capture_output=True, text=True, timeout=30)
        check("preflight_help_rc", help_preflight.returncode == 0, f"rc={help_preflight.returncode} stderr={help_preflight.stderr[:200]}")
        check("preflight_help_usage", "usage:" in help_preflight.stdout.lower(), help_preflight.stdout[:120])

        # 2. bundled preflight with env file under a path containing spaces
        run = subprocess.run([str(preflight_exe), "--env-file", str(env_file), "--json"],
                             capture_output=True, text=True, timeout=60)
        preflight_out.append(run.stdout)
        ok_json = None
        try:
            ok_json = json.loads(run.stdout)
        except json.JSONDecodeError:
            pass
        check("preflight_rc", run.returncode == 0, f"rc={run.returncode} stderr={run.stderr[:200]}")
        check("preflight_json_ok", bool(ok_json and ok_json.get("ok")), str(ok_json)[:200] if ok_json else run.stdout[:200])
        if ok_json:
            check("preflight_space_paths", " " in str(ok_json.get("state_dir", "")),
                  f"state_dir={ok_json.get('state_dir')!r} must contain a space")
        results["preflight_platform"] = ok_json.get("platform") if ok_json else None

        # 3-4. agent lifecycle against the local mock fixture
        agent_proc = subprocess.Popen([str(agent_exe), "--env-file", str(env_file)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, cwd=str(workdir))
        spawn_reader(agent_proc.stdout, agent_out)
        spawn_reader(agent_proc.stderr, agent_err)
        wait_for(lambda: fixture.register_calls >= 1, 60, "register", results)
        wait_for(lambda: fixture.heartbeats >= 2, 60, "heartbeats>=2", results)
        wait_for(lambda: fixture.polls >= 3, 60, "polls>=3", results)
        wait_for(lambda: any(s == "running" for _, s in fixture.reports), 60, "report_running", results)
        wait_for(lambda: any(s == "completed" for _, s in fixture.reports), 90, "report_completed", results)

        # 5. state lock held while running
        check("lock_held_while_running", not try_flock(state_dir),
              "agent.lock must NOT be lockable while the agent runs")

        # 6. SIGINT graceful stop with exit code 0
        agent_proc.send_signal(signal.SIGINT)
        try:
            agent_rc = agent_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            agent_proc.kill()
            agent_rc = "killed-after-timeout"
        check("agent_sigint_rc0", agent_rc == 0, f"rc={agent_rc}")
        check("agent_stats_line", any("node agent stopped:" in line for line in agent_out),
              "".join(agent_out)[-200:])

        # 7. lock reacquirable after exit
        check("lock_reacquirable_after_exit", try_flock(state_dir),
              "agent.lock must be lockable after the agent exits")
        creds = state_dir / "node-credentials.json"
        check("credentials_persisted_0600",
              creds.exists() and stat.S_IMODE(creds.stat().st_mode) == 0o600,
              f"exists={creds.exists()} mode={oct(stat.S_IMODE(creds.stat().st_mode)) if creds.exists() else '-'}")
        check("state_dir_0700", stat.S_IMODE(state_dir.stat().st_mode) == 0o700,
              oct(stat.S_IMODE(state_dir.stat().st_mode)))

        # 8. token never in captured output
        captured = "".join(agent_out) + "".join(agent_err) + "".join(preflight_out)
        check("token_not_in_output",
              REGISTRATION_TOKEN not in captured and fixture.node_token not in captured,
              "registration/node token leaked" if (REGISTRATION_TOKEN in captured
                                                   or (fixture.node_token or "") in captured) else "clean")
    except Exception as exc:  # noqa: BLE001 - report any failure through the summary
        results["exception"] = f"{type(exc).__name__}: {exc}"
        failures.append("exception")
    finally:
        if agent_proc is not None and agent_proc.poll() is None:
            agent_proc.kill()
            agent_proc.wait(timeout=10)
        server.shutdown()
        server.server_close()
        results["fixture_stopped"] = True

    results["agent_stdout_tail"] = "".join(agent_out)[-500:]
    results["agent_stderr_tail"] = "".join(agent_err)[-500:]
    results["fixture_counts"] = {
        "register_calls": fixture.register_calls,
        "heartbeats": fixture.heartbeats,
        "polls": fixture.polls,
        "reports": fixture.reports,
    }
    results["passed"] = not failures
    results["failed_steps"] = failures
    print(json.dumps(results, ensure_ascii=False, indent=2))

    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"kept workdir: {workdir}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
