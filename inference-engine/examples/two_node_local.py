"""Local two-node integration demo.

Starts the last segment and the first segment as two separate processes, sends a
prompt through them, and compares the returned logits with the single-process
reference for the same checkpoint.

    python examples/two_node_local.py
    python examples/two_node_local.py --layers 0:1,1:3,3:4 --prompt "5 6 7" --workdir ./run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READY = re.compile(r"READY\s+(\S+)\s+(\d+)")


def run_node(args, env, *, ready=False):
    process = subprocess.Popen([sys.executable, "-m", "inference_engine.node", *args], cwd=ROOT,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not ready:
        output, _ = process.communicate(timeout=120)
        if process.returncode != 0:
            raise SystemExit(f"node failed: {' '.join(args)}\n{output}")
        return json.loads(output.strip().splitlines()[-1])
    assert process.stdout is not None
    for line in process.stdout:
        match = READY.search(line)
        if match:
            return process, (match.group(1), int(match.group(2)))
    process.kill()
    raise SystemExit(f"node never became ready: {' '.join(args)}")


def stop(processes):
    for process in processes[::-1]:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layers", default="0:2,2:4", help="comma separated start:end segments")
    parser.add_argument("--prompt", default="1 2 3 4")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--model-id", default="lastcore/tiny-cpu")
    parser.add_argument("--workdir", help="keep the checkpoint and stats here; default is a temp dir")
    args = parser.parse_args()

    segments = [tuple(int(part) for part in value.split(":")) for value in args.layers.split(",")]
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="lastcore-"))
    workdir.mkdir(parents=True, exist_ok=True)
    config_path, weights_path = workdir / "tiny.json", workdir / "tiny.npz"
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT.parent / "communication")])}
    print(f"workdir: {workdir}")

    generated = run_node(["generate", "--write-config", str(config_path), "--out", str(weights_path),
                          "--seed", str(args.seed)], env)
    print(f"checkpoint: {generated['parameters']} parameters, {generated['bytes']} bytes")

    reference = run_node(["reference", "--config", str(config_path), "--weights", str(weights_path),
                          "--prompt", args.prompt], env)
    print(f"single process: shape {reference['shape']} next_token={reference['next_token']} "
          f"sha256 {reference['logits_sha256'][:16]}...")

    processes, addresses = [], {}
    try:
        downstream = None
        for start, end in reversed(segments):
            stats_path = workdir / f"stage-{start}-{end}.json"
            command = ["serve", "--config", str(config_path), "--weights", str(weights_path),
                       "--layers", f"{start}:{end}", "--listen", "127.0.0.1:0", "--timeout", "60",
                       "--stats-out", str(stats_path)]
            if downstream is not None:
                command += ["--downstream", f"{downstream[0]}:{downstream[1]}"]
            process, address = run_node(command, env, ready=True)
            processes.append(process)
            addresses[(start, end)] = address
            print(f"stage [{start}, {end}) listening on {address[0]}:{address[1]}")
            downstream = address

        first = addresses[segments[0]]
        distributed = run_node(["run", "--first-stage", f"{first[0]}:{first[1]}",
                                "--prompt", args.prompt, "--model-id", args.model_id], env)
        print(f"distributed:    shape {distributed['shape']} next_token={distributed['next_token']} "
              f"sha256 {distributed['logits_sha256'][:16]}...")
    finally:
        stop(processes)

    for start, end in addresses:
        stats = json.loads((workdir / f"stage-{start}-{end}.json").read_text())
        print(f"stage [{start}, {end}): served {stats['served']} task(s), "
              f"loaded {stats['parameter_bytes']} parameter bytes (only this segment)")

    identical = distributed["logits_sha256"] == reference["logits_sha256"]
    print(f"bit-identical to the single-machine result: {identical}")
    if not args.workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
