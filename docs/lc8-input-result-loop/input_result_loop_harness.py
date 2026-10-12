#!/usr/bin/env python3
"""LC-8 input -> execution -> result loop sample and checks.

Drives the real node-agent Executor contract (``StageExecutor`` +
``ExecutionContext``) as the first pipeline stage, with a plain
``inference-engine`` ``StageServer`` as the last stage, over the real TCP frame
channel (``communication/``). Nothing in the modules is modified; only their
public interfaces are called.

Checks:
  1. multi-step generation: greedy autoregressive decode, re-feeding the whole
     sequence each step (the current engine has no KV cache);
  2. correctness: every distributed step's logits are byte-identical to the
     single-machine reference for the same checkpoint and input;
  3. idempotency: running the same input twice yields identical tokens and
     identical logits digests;
  4. cancellation and late results: after the cancel event is set the stage stops
     serving, releases its port, and a later request cannot observe a success.

Module locations are read from the environment so that the harness can run
against isolated checkouts:

  LC_REPO_ROOT       default: current working directory
  LC_ENGINE_DIR      default: $LC_REPO_ROOT/inference-engine
  LC_COMM_DIR        default: $LC_REPO_ROOT/communication
  LC_NODE_AGENT_DIR  default: $LC_REPO_ROOT/node-agent

Example (engineer-b engine/communication branch + engineer-a node-agent branch):

  LC_REPO_ROOT=$(pwd) \\
  LC_ENGINE_DIR=$WT/inference-engine \\
  LC_COMM_DIR=$WT/communication \\
  LC_NODE_AGENT_DIR=$NODEWT/node-agent \\
  python input_result_loop_harness.py --out results.json

TODO/known gap this sample deliberately exposes (see gap-analysis.md): the
prompt is submitted by this in-process caller, not by a controller task; the
first stage serves until cancelled instead of until a task-level terminal signal;
and the result is read by this process, not from the Controller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import tempfile
import time
from pathlib import Path

import numpy as np

TASK_ID = "task-lc8"
SEGMENTS = ((0, 2), (2, 4))
PROMPT = [1, 2, 3]
STEPS = 3
SEED = 7


def _bootstrap() -> None:
    repo = Path(os.environ.get("LC_REPO_ROOT", Path.cwd())).expanduser().resolve()
    dirs = {
        "LC_ENGINE_DIR": Path(os.environ.get("LC_ENGINE_DIR", repo / "inference-engine")),
        "LC_COMM_DIR": Path(os.environ.get("LC_COMM_DIR", repo / "communication")),
        "LC_NODE_AGENT_DIR": Path(os.environ.get("LC_NODE_AGENT_DIR", repo / "node-agent")),
    }
    markers = {
        "LC_ENGINE_DIR": "inference_engine",
        "LC_COMM_DIR": "communication",
        "LC_NODE_AGENT_DIR": "lastcore_node_agent",
    }
    for key, directory in dirs.items():
        resolved = directory.expanduser().resolve()
        if not (resolved / markers[key]).is_dir():
            raise SystemExit(
                f"{key}={resolved} does not contain {markers[key]}/; "
                "point it at a checkout of that module")
        if str(resolved) not in sys.path:
            sys.path.insert(0, str(resolved))


_bootstrap()

from inference_engine import (PipelineClient, StageServer, forward_full,  # noqa: E402
                              generate_weights, load_shard, load_stage, save_weights)
from inference_engine.config import tiny_config  # noqa: E402
from lastcore_node_agent.executor import ExecutionCancelled, ExecutionContext  # noqa: E402
from lastcore_node_agent.inference_adapter import StageExecutor  # noqa: E402


def digest(array) -> str:
    data = np.ascontiguousarray(np.asarray(array, dtype=np.float32))
    return hashlib.sha256(data.tobytes()).hexdigest()


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def port_bound(port: int) -> bool:
    """True when something is listening on the port.

    Probes by binding, never by connecting: a raw connect would be accepted by
    the stage server and consume a request slot (and can even kill a server whose
    accept loop is not tolerant of a peer that closes before its HELLO).
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def make_context(last_port: int, deadline: float) -> ExecutionContext:
    next_node = {"node_id": "node-1", "endpoint": f"http://127.0.0.1:{last_port}", "id": "asg-1"}
    raw = {
        "id": "asg-0", "task_id": TASK_ID, "node_id": "node-0", "position": 0,
        "layer_start": SEGMENTS[0][0], "layer_end": SEGMENTS[0][1],
        "previous_node": None, "next_node": next_node,
        "status": "pending", "error": None,
        "request": {"model_id": tiny_config().model_id, "num_layers": SEGMENTS[-1][1],
                    "layer_memory_mb": 512},
        "deadline": deadline,
    }
    return ExecutionContext(
        task_id=TASK_ID, assignment_id="asg-0", model_id=tiny_config().model_id, position=0,
        layer_start=SEGMENTS[0][0], layer_end=SEGMENTS[0][1], previous_node=None,
        next_node=next_node, deadline=deadline, raw=raw)


def run_decode(config, reference_weights, first_port: int, steps: int, prompt: list[int]) -> list[dict]:
    """Greedy multi-step decode through the first stage; each step re-sends the full sequence."""
    records: list[dict] = []
    tokens = list(prompt)
    for _ in range(steps):
        array = np.asarray([tokens], dtype=np.int64)
        logits = PipelineClient(("127.0.0.1", first_port), model_id=config.model_id,
                                task_id=TASK_ID, timeout=30.0).run(array, task_id=TASK_ID)
        reference = forward_full(array, reference_weights, config)
        records.append({
            "input_tokens": list(tokens),
            "output_shape": list(np.asarray(logits).shape),
            "logits_sha256": digest(logits),
            "reference_sha256": digest(reference),
            "bit_identical": bool(np.array_equal(logits, reference)),
            "next_token": int(np.argmax(logits[0, -1])),
        })
        tokens.append(records[-1]["next_token"])
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--steps", type=int, default=STEPS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--workdir", help="keep the checkpoint here; default is a temp dir")
    parser.add_argument("--out", help="also write the JSON report to this path")
    args = parser.parse_args(argv)

    config = tiny_config()
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="lc8-"))
    workdir.mkdir(parents=True, exist_ok=True)
    weights_path = workdir / "tiny.npz"
    if not weights_path.exists():
        save_weights(weights_path, generate_weights(config, seed=args.seed))
    reference_weights = load_shard(weights_path, config, 0, config.n_layers)

    report: dict = {
        "model_id": config.model_id, "seed": args.seed, "checkpoint": str(weights_path),
        "segments": [list(segment) for segment in SEGMENTS],
        "prompt": PROMPT, "steps": args.steps,
    }

    last_port, first_port = free_port(), free_port()
    last_server = StageServer(load_stage(config, weights_path, *SEGMENTS[-1]),
                              address=("127.0.0.1", last_port), timeout=30.0)
    stop = threading.Event()
    last_thread = threading.Thread(
        target=lambda: last_server.serve_forever(should_stop=stop.is_set),
        name="lc8-last-stage", daemon=True)
    last_thread.start()

    deadline = time.time() + 300.0
    context = make_context(last_port, deadline)
    executor = StageExecutor(model_config=config, weights_path=weights_path,
                             endpoint=f"http://127.0.0.1:{first_port}", complete_after=None)
    outcome: dict = {}

    def serve_first() -> None:
        try:
            executor.run(context)
        except BaseException as exc:  # noqa: BLE001 - reported as the cancel evidence
            outcome["error"] = exc

    first_thread = threading.Thread(target=serve_first, name="lc8-first-stage", daemon=True)
    first_thread.start()

    try:
        assert wait_until(lambda: port_bound(first_port)), "first stage never listened"
        assert port_bound(last_port), "last stage never listened"

        first_run = run_decode(config, reference_weights, first_port, args.steps, PROMPT)
        second_run = run_decode(config, reference_weights, first_port, args.steps, PROMPT)
        report["decode_run_1"] = first_run
        report["decode_run_2"] = second_run
        report["all_steps_bit_identical"] = all(step["bit_identical"] for step in first_run)
        report["generated_tokens"] = [step["next_token"] for step in first_run]
        report["idempotent_repeat"] = (
            [step["logits_sha256"] for step in first_run] == [step["logits_sha256"] for step in second_run]
            and [step["next_token"] for step in first_run] == [step["next_token"] for step in second_run])
        report["last_stage_served"] = last_server.processed

        context.request_cancel("task-terminal")
        stopped = wait_until(lambda: not first_thread.is_alive(), timeout=15.0)
        report["cancel_stopped_executor"] = stopped
        error = outcome.get("error")
        report["cancel_error_type"] = type(error).__name__ if error is not None else None
        report["cancel_reason"] = getattr(error, "reason", None)
        report["cancel_is_execution_cancelled"] = isinstance(error, ExecutionCancelled)
        report["port_released"] = wait_until(lambda: not port_bound(first_port), timeout=5.0)

        late: dict = {"attempted": True}
        try:
            PipelineClient(("127.0.0.1", first_port), model_id=config.model_id,
                           task_id=TASK_ID, timeout=5.0).run(
                               np.asarray([PROMPT], dtype=np.int64), task_id=TASK_ID)
            late["raised"] = None
            late["late_success_observed"] = True
        except Exception as exc:  # noqa: BLE001 - any transport/cancel failure is the expected outcome
            late["raised"] = type(exc).__name__
            late["late_success_observed"] = False
        report["late_request"] = late
    finally:
        stop.set()
        last_thread.join(timeout=15.0)
        last_server.close()

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")

    ok = (report.get("all_steps_bit_identical") and report.get("idempotent_repeat")
          and report.get("cancel_is_execution_cancelled") and report.get("port_released")
          and report.get("late_request", {}).get("late_success_observed") is False)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
