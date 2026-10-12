"""Drive the LastCore system scenarios and emit a machine-readable report.

    python -m integration.run_scenarios \
        --controller-python /path/to/.venv-controller/bin/python \
        --node-python /path/to/.venv-node/bin/python

Every scenario starts real processes, records observed behaviour, and always
cleans up. Results are printed as a table and written to ``report.json`` so the
fault-regression evidence can be re-generated on demand.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "inference-engine"))
sys.path.insert(0, str(REPO_ROOT / "communication"))

from integration.lastcore_harness import (LastCoreSystem, http_json, port_open)  # noqa: E402

SCENARIOS: dict[str, dict] = {}


def scenario(name: str, purpose: str):
    def register(func):
        SCENARIOS[name] = {"run": func, "purpose": purpose}
        return func
    return register


class Ctx:
    def __init__(self, root: Path, controller_python: str, node_python: str):
        self.root = root
        self.controller_python = controller_python
        self.node_python = node_python


def new_system(ctx: Ctx, name: str, **kwargs) -> LastCoreSystem:
    return LastCoreSystem(ctx.root / name, controller_python=ctx.controller_python,
                          node_python=ctx.node_python, **kwargs)


def process_metrics(system: LastCoreSystem) -> dict:
    metrics = {"peak_rss_mb": {}, "stage_requests": {}, "stop_reasons": {}}
    for key, node in system.nodes.items():
        metrics["peak_rss_mb"][f"node-{key}"] = round(node.peak_rss_kb / 1024, 1)
    if system.controller is not None:
        metrics["peak_rss_mb"]["controller"] = round(system.controller.peak_rss_kb / 1024, 1)
    for key, entries in system.stage_log_metrics().items():
        metrics["stage_requests"][f"node-{key}"] = entries
    for key, reasons in system.node_stop_reasons().items():
        if reasons:
            metrics["stop_reasons"][f"node-{key}"] = reasons
    return metrics


def identical(logits, reference) -> bool:
    return bool(np.array_equal(np.asarray(logits), np.asarray(reference)))


@scenario("normal_e2e", "one command: Controller + 2 Node Agents -> bit-identical logits")
def normal_e2e(ctx: Ctx) -> dict:
    system = new_system(ctx, "normal_e2e", complete_after=1)
    result: dict = {"expected": "task completed; logits bit-identical to single-machine reference"}
    try:
        system.start()
        task = system.create_task(timeout_seconds=120)
        result["task_id"] = task["task_id"]
        result["assignments"] = [{"node": a["node_id"], "layers": [a["layer_start"], a["layer_end"]],
                                  "upstream": bool(a.get("previous_node")),
                                  "downstream": bool(a.get("next_node"))}
                                 for a in task["assignments"]]
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        request = system.run_request(task["task_id"], "1 2 3 4")
        result["bit_identical"] = identical(request.pop("logits"), system.reference_logits("1 2 3 4"))
        result["request"] = request
        system.wait_task_status(task["task_id"], {"completed", "failed"}, timeout=60)
        result["task_terminal"] = system.get_task(task["task_id"])[1]["status"]
        result["metrics"] = process_metrics(system)
        result["pass"] = bool(result["bit_identical"] and result["task_terminal"] == "completed")
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
    return result


@scenario("multi_step_generation", "three sequential requests on one task, then cancel")
def multi_step_generation(ctx: Ctx) -> dict:
    system = new_system(ctx, "multi_step_generation", complete_after=0)
    result: dict = {"expected": "3 requests serve identical logits; cancel stops both agents locally"}
    try:
        system.start()
        task = system.create_task(timeout_seconds=180)
        result["task_id"] = task["task_id"]
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        requests = []
        for prompt in ("1 2 3 4", "5 6 7", "2 4 6 8 10"):
            request = system.run_request(task["task_id"], prompt)
            request["bit_identical"] = identical(request.pop("logits"), system.reference_logits(prompt))
            requests.append(request)
        result["requests"] = requests
        status, _ = system.admin("POST", f"/v1/tasks/{task['task_id']}/cancel")
        result["cancel_http_status"] = status
        terminal = system.wait_task_status(task["task_id"], {"cancelled", "failed"}, timeout=30)
        result["task_terminal"] = terminal["status"]
        result["all_identical"] = all(r["bit_identical"] for r in requests)
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
        # "stopped locally" is only logged while the agent tears the run down, which
        # can land just after the Controller reports the terminal task status.
        if "task_terminal" in result:
            result["pass"] = bool(result.get("all_identical")
                                  and result["task_terminal"] == "cancelled"
                                  and result["metrics"]["stop_reasons"])
    return result


@scenario("deadline_timeout", "task deadline expires -> controller fails it and agents stop locally")
def deadline_timeout(ctx: Ctx) -> dict:
    system = new_system(ctx, "deadline_timeout", complete_after=0)
    result: dict = {"expected": "task failed with 'task deadline exceeded'; agents log stopped locally"}
    try:
        system.start()
        task = system.create_task(timeout_seconds=10)
        result["task_id"] = task["task_id"]
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        terminal = system.wait_task_status(task["task_id"], {"failed", "cancelled"}, timeout=40)
        result["task_terminal"] = terminal["status"]
        result["task_error"] = terminal.get("error")
        time.sleep(2)
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
        if "task_terminal" in result:
            result["pass"] = (result["task_terminal"] == "failed"
                              and "deadline" in (result["task_error"] or "")
                              and bool(result["metrics"]["stop_reasons"]))
    return result


@scenario("node_disconnect", "kill the last-stage node mid-task: request fails, task expires")
def node_disconnect(ctx: Ctx) -> dict:
    system = new_system(ctx, "node_disconnect", complete_after=0, heartbeat_ttl=8)
    result: dict = {"expected": "client request errors; controller fails the task after heartbeat TTL"}
    try:
        system.start()
        task = system.create_task(timeout_seconds=180)
        result["task_id"] = task["task_id"]
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        result["killed"] = system.kill_node("b")
        try:
            request = system.run_request(task["task_id"], "1 2 3 4")
            result["request_error"] = None
            result["request_logits_sha"] = None
            request.pop("logits")
            result["request"] = request
        except Exception as exc:  # noqa: BLE001 - the failure itself is the observation
            result["request_error"] = f"{type(exc).__name__}: {exc}"
        terminal = system.wait_task_status(task["task_id"], {"failed", "cancelled"}, timeout=30)
        result["task_terminal"] = terminal["status"]
        result["task_error"] = terminal.get("error")
        result["pass"] = bool(result["request_error"]) and result["task_terminal"] == "failed"
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
    return result


@scenario("controller_restart", "restart the Controller: credentials persist, new task still serves")
def controller_restart(ctx: Ctx) -> dict:
    system = new_system(ctx, "controller_restart", complete_after=1)
    result: dict = {"expected": "nodes keep identity after restart; a new task completes identically"}
    try:
        system.start()
        before = http_json(f"{system.controller_url}/v1/nodes", token=system.admin_token)[1]["nodes"]
        result["nodes_before"] = sorted(n["id"] for n in before)
        system.restart_controller()
        deadline = time.monotonic() + 30
        nodes_after = []
        while time.monotonic() < deadline:
            status, payload = http_json(f"{system.controller_url}/v1/nodes", token=system.admin_token)
            if status == 200:
                nodes_after = payload["nodes"]
                if len(nodes_after) == len(system.nodes) and all(n["status"] != "offline" for n in nodes_after):
                    break
            time.sleep(0.5)
        result["nodes_after"] = sorted(n["id"] for n in nodes_after)
        task = system.create_task(timeout_seconds=120)
        result["task_id"] = task["task_id"]
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        request = system.run_request(task["task_id"], "3 1 4 1 5")
        result["bit_identical"] = identical(request.pop("logits"), system.reference_logits("3 1 4 1 5"))
        result["request"] = request
        terminal = system.wait_task_status(task["task_id"], {"completed", "failed"}, timeout=60)
        result["task_terminal"] = terminal["status"]
        result["metrics"] = process_metrics(system)
        result["pass"] = (result["nodes_before"] == result["nodes_after"]
                          and result["bit_identical"] and result["task_terminal"] == "completed")
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
    return result


@scenario("auth_failure", "wrong credentials and a mismatched model id are rejected")
def auth_failure(ctx: Ctx) -> dict:
    system = new_system(ctx, "auth_failure", complete_after=1)
    result: dict = {"expected": "401 for bad tokens; client request rejected on model mismatch"}
    try:
        system.start()
        bad_admin = system.admin("POST", "/v1/tasks",
                                 {"model_id": system.model_id, "num_layers": 4,
                                  "layer_memory_mb": 100, "timeout_seconds": 60},
                                 token="wrong-admin-token-with-at-least-32-characters")
        result["bad_admin_create_task"] = bad_admin[0]
        bad_register = http_json(f"{system.controller_url}/v1/nodes/register", method="POST",
                                 token="wrong-registration-token-with-32-characters",
                                 payload={"name": "x", "platform": "y",
                                          "endpoint": "http://127.0.0.1:1",
                                          "available_memory_mb": 1024, "compute_weight": 1})
        result["bad_registration_token"] = bad_register[0]
        node_id = system.node_ids[system.first_node_key]
        bad_heartbeat = http_json(
            f"{system.controller_url}/v1/nodes/{node_id}/heartbeat", method="POST",
            token="wrong-node-token-with-at-least-32-characters",
            payload={"available_memory_mb": 1024, "accepting_tasks": True})
        result["bad_node_token_heartbeat"] = bad_heartbeat[0]
        task = system.create_task(timeout_seconds=120)
        system.wait_task_status(task["task_id"], "running", timeout=30)
        system.wait_nodes_serving(timeout=30)
        from inference_engine import PipelineClient

        tokens = np.asarray([[1, 2, 3, 4]], dtype=np.int64)
        try:
            PipelineClient(system.first_stage_address(), model_id="wrong/model", task_id=task["task_id"],
                           timeout=10).run(tokens, task_id=task["task_id"])
            result["model_mismatch_error"] = None
        except Exception as exc:  # noqa: BLE001 - the rejection is the observation
            result["model_mismatch_error"] = f"{type(exc).__name__}: {exc}"
        result["pass"] = (result["bad_admin_create_task"] == 401
                          and result["bad_registration_token"] == 401
                          and result["bad_node_token_heartbeat"] == 401
                          and bool(result["model_mismatch_error"]))
    finally:
        result["cleanup"] = system.stop()
        result["metrics"] = process_metrics(system)
        result["ports_released"] = all(result["cleanup"]["ports_released"].values())
    return result


@scenario("bad_tensor", "tampered and truncated tensor frames are rejected before use")
def bad_tensor(ctx: Ctx) -> dict:
    from communication import Frame, FrameType, encode_frame, encode_tensor
    from communication.errors import ProtocolError
    from communication.protocol import PREFIX_SIZE, decode_header, decode_tensor, parse_prefix

    header, payload = encode_tensor("hidden", np.arange(128, dtype=np.float32).reshape(1, 4, 32))
    frame = encode_frame(Frame(FrameType.TENSOR, header, payload))

    def split(raw: bytes):
        kind, header_len, _payload_len = parse_prefix(raw[:PREFIX_SIZE])
        decoded_header = decode_header(raw[PREFIX_SIZE:PREFIX_SIZE + header_len])
        return decoded_header, raw[PREFIX_SIZE + header_len:]

    observations = {}
    corrupted = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    try:
        decode_tensor(*split(corrupted))
        observations["corrupted_payload"] = "ACCEPTED (unexpected)"
    except ProtocolError as exc:
        observations["corrupted_payload"] = f"rejected: {exc}"
    truncated = frame[:PREFIX_SIZE + len(json.dumps(header)) + 8]
    try:
        decoded_header, body = split(truncated)
        decode_tensor(decoded_header, body)
        observations["truncated_payload"] = "ACCEPTED (unexpected)"
    except ProtocolError as exc:
        observations["truncated_payload"] = f"rejected: {exc}"
    try:
        decode_tensor({**header, "shape": [1, 4, 31]}, payload)
        observations["wrong_shape"] = "ACCEPTED (unexpected)"
    except ProtocolError as exc:
        observations["wrong_shape"] = f"rejected: {exc}"
    return {"expected": "all three malformed frames rejected with ProtocolError",
            "observations": observations,
            "pass": all("rejected" in value for value in observations.values()),
            "cleanup": {"processes": [], "relays": [], "ports_released": {}},
            "ports_released": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", default=str(REPO_ROOT / "integration" / "runs"))
    parser.add_argument("--report", default=str(REPO_ROOT / "integration" / "runs" / "report.json"))
    parser.add_argument("--controller-python",
                        default=os.environ.get("LASTCORE_CONTROLLER_PYTHON", sys.executable))
    parser.add_argument("--node-python",
                        default=os.environ.get("LASTCORE_NODE_PYTHON", sys.executable))
    parser.add_argument("--scenario", action="append", default=[],
                        help="run only these scenarios (repeatable); default all")
    parser.add_argument("--keep", action="store_true", help="keep scenario work directories")
    args = parser.parse_args(argv)

    selected = args.scenario or list(SCENARIOS)
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    root = Path(args.workdir)
    if root.exists() and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    ctx = Ctx(root, args.controller_python, args.node_python)
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "host": {"platform": platform.platform(), "machine": platform.machine(),
                       "python": sys.version.split()[0], "repo": str(REPO_ROOT)},
              "runtimes": {"controller_python": args.controller_python,
                           "node_python": args.node_python,
                           "harness_python": sys.executable},
              "scenarios": []}
    failures = 0
    for name in selected:
        entry: dict = {"name": name, "purpose": SCENARIOS[name]["purpose"]}
        started = time.monotonic()
        try:
            entry.update(SCENARIOS[name]["run"](ctx))
            entry["status"] = "pass" if entry.get("pass") else "fail"
        except Exception as exc:  # noqa: BLE001 - a scenario failure is data, keep going
            entry["status"] = "fail"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()
        entry["duration_seconds"] = round(time.monotonic() - started, 2)
        if entry["status"] != "pass":
            failures += 1
        report["scenarios"].append(entry)
        print(f"[{entry['status']:>4}] {name} ({entry['duration_seconds']}s) - {entry.get('purpose','')}",
              flush=True)
        if entry["status"] != "pass":
            print(f"       error: {entry.get('error', 'assertion failed')}", flush=True)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8")
    summary = {"pass": sum(1 for s in report["scenarios"] if s["status"] == "pass"),
               "fail": failures, "report": str(report_path)}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
