"""Windows/Linux linear client, reusing the existing node lifecycle and state lock."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "node-agent", ROOT / "node-agent/desktop"):
    sys.path.insert(0, str(directory))

from env_file import read_env_file
from lastcore_node_agent.agent import NodeAgent
from lastcore_node_agent.config import AgentConfig
from lastcore_node_agent.controller import ControllerClient
from lastcore_node_agent.credentials import _atomic_write_json, acquire_state_lock, load_credentials
from lastcore_node_agent.executor import Executor, ExecutionCancelled

VERSION = "0.2.0"
MODEL = "lastcore/linear-demo-v1"


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Controller address must be a plain HTTPS origin")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote Controller requires HTTPS")
    parsed.port
    return value.rstrip("/")


def result_file(state, assignment_id):
    name = hashlib.sha256(assignment_id.encode()).hexdigest() + ".json"
    return Path(state) / "linear-results" / name


def weight(seed, row, column):
    value = seed ^ ((row * 0x9E3779B1) & 0xFFFFFFFF) ^ ((column * 0x85EBCA77) & 0xFFFFFFFF)
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    value ^= value >> 16
    return value / 0xFFFFFFFF * 2 - 1


class LinearExecutor(Executor):
    name = "linear-v1"

    def __init__(self, state):
        self.state = Path(state)

    def run(self, context):
        request = (context.raw or {}).get("request", {})
        vector, seed = request.get("input_vector"), request.get("weight_seed")
        if (context.model_id != MODEL or not isinstance(vector, list) or not 1 <= len(vector) <= 256
                or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1_000_000 for v in vector)
                or type(seed) is not int or not 0 <= seed <= 2_147_483_647
                or not 0 <= context.layer_start < context.layer_end <= 4096):
            raise ValueError("Unsupported or invalid bounded linear workload")
        binding = hashlib.sha256(json.dumps([context.task_id, context.assignment_id, request,
                                            context.layer_start, context.layer_end], sort_keys=True).encode()).hexdigest()
        target = result_file(self.state, context.assignment_id)
        if target.exists():
            saved = json.loads(target.read_text())
            if saved.get("binding") != binding:
                raise ValueError("Saved computation does not match this assignment")
            return  # Durable outcome is replayed, never recomputed after a report outage.
        values = []
        for row in range(context.layer_start, context.layer_end):
            if context.cancel.is_set() or time.time() >= context.deadline:
                raise ExecutionCancelled(context.cancel_reason or "deadline")
            total = weight(seed, row, len(vector))
            for column, value in enumerate(vector):
                total += weight(seed, row, column) * value
            values.append(math.tanh(total))
        if context.cancel.is_set() or time.time() >= context.deadline:
            raise ExecutionCancelled(context.cancel_reason or "deadline")
        _atomic_write_json(target, {"binding": binding, "task_id": context.task_id,
                                    "assignment_id": context.assignment_id, "result": values})


class LinearController(ControllerClient):
    def __init__(self, url, state, publish=print, **kwargs):
        super().__init__(url, **kwargs)
        self.state = Path(state)
        self.publish = publish
        self.last_heartbeat = 0.0
        self.last_task = None
        self.last_result = None

    def heartbeat(self, *args, **kwargs):
        value = super().heartbeat(*args, **kwargs)
        self.last_heartbeat = time.monotonic()
        return value

    def report(self, node_id, node_token, assignment_id, status, error=None):
        payload = {"status": status, "error": error}
        cached = None
        if status == "completed":
            cached = json.loads(result_file(self.state, assignment_id).read_text())
            payload["result"] = cached["result"]
        response = self._client.post(f"/v1/nodes/{node_id}/assignments/{assignment_id}/report",
                                     json=payload, headers=self._headers(node_token))
        self._check(response)
        task = response.json()
        if cached is not None:
            assignment = next(a for a in task["assignments"] if a["id"] == assignment_id)
            if assignment["status"] != "completed" or assignment.get("result") != cached["result"]:
                raise RuntimeError("Controller has not acknowledged the computed values")
            self.last_task, self.last_result = task["id"], cached["result"]
            _atomic_write_json(self.state / "last-linear-result.json", {
                "task_id": self.last_task, "result": self.last_result, "server_acknowledged": True})
            self.publish(json.dumps({"task_id": self.last_task, "result": self.last_result,
                                     "status": assignment["status"]}))
        return task


class Session:
    def __init__(self, env_file, bind_existing_origin=None):
        values = read_env_file(env_file)
        self.config = AgentConfig.from_env(values)
        current_origin = origin(self.config.controller_url)
        self.lock = acquire_state_lock(self.config.state_dir)
        if self.lock is None:
            raise ValueError("State directory is already in use; exit the old client first")
        self.client = None
        try:
            credentials = load_credentials(self.config.state_dir)
            if credentials is None:
                raise ValueError("Choose the existing node configuration/state; registration is not performed here")
            binding = self.config.state_dir / "linear-origin.json"
            if binding.exists():
                if json.loads(binding.read_text()).get("origin") != current_origin:
                    raise ValueError("Existing node identity belongs to a different Controller origin")
            elif origin(bind_existing_origin or "") == current_origin:
                _atomic_write_json(binding, {"origin": current_origin})
            else:
                raise ValueError("Confirm the original issuing origin before using existing credentials")
            self.node_id = credentials.node_id
            self.client = LinearController(current_origin, self.config.state_dir)
            self.agent = NodeAgent(self.config, self.client,
                                   executor_factory=lambda: LinearExecutor(self.config.state_dir))
        except Exception:
            self.lock.close()
            raise

    def close(self):
        if self.client is not None:
            self.client.close()
        self.lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"LastCore Linear Edge {VERSION}")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--bind-existing-origin", help="Independently verified original issuing HTTPS origin")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The business display is Beijing time, independent of the host's local timezone.
    logging.Formatter.converter = lambda *_: time.gmtime(time.time() + 8 * 3600)
    session = None
    try:
        session = Session(args.env_file, args.bind_existing_origin)
        print(json.dumps({"version": VERSION, "node_id": session.node_id, "executor": MODEL}), flush=True)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: session.agent.shutdown())
        session.agent.run_forever()
        return 0
    except Exception as exc:
        # Do not echo private config, raw provider bodies, tokens or exception arguments.
        print(f"Client stopped: {type(exc).__name__}. Check the original node configuration and HTTPS origin.", file=sys.stderr)
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
