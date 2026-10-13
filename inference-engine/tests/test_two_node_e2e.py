"""Two-node local integration: each stage runs in its own OS process.

Node B starts the last segment ``[2, 4)`` and listens; node A starts ``[0, 2)`` and
forwards to B; a caller sends a prompt to A. The logits that come back must hash to
exactly the single-process reference for the same checkpoint and prompt.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = os.pathsep.join([str(ROOT), str(ROOT.parent / "communication")])
READY = re.compile(r"READY\s+(\S+)\s+(\d+)")
PROMPT = "1 2 3 4"
TIMEOUT = 60


def run_node(args, *, wait_for_ready=False, timeout=TIMEOUT):
    env = {**os.environ, "PYTHONPATH": PYTHONPATH}
    process = subprocess.Popen([sys.executable, "-m", "inference_engine.node", *args],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    if not wait_for_ready:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output
    assert process.stdout is not None
    for line in process.stdout:
        match = READY.search(line)
        if match:
            return process, (match.group(1), int(match.group(2)))
    process.kill()
    raise AssertionError("node never reported READY")


def wait_for_stats(paths, *, served, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payloads = []
        for path in paths:
            try:
                payloads.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                break
        if len(payloads) == len(paths) and all(item.get("served") == served for item in payloads):
            return payloads
        time.sleep(0.02)
    raise AssertionError(f"stats files were not flushed within {timeout}s: {paths}")


@pytest.fixture
def checkpoint(tmp_path):
    config_path = tmp_path / "tiny.json"
    weights_path = tmp_path / "tiny.npz"
    code, output = run_node(["generate", "--write-config", str(config_path), "--out",
                             str(weights_path), "--seed", "7"])
    assert code == 0, output
    return config_path, weights_path, json.loads(output.strip().splitlines()[-1])


def test_two_os_processes_reproduce_the_single_process_logits(checkpoint, tmp_path):
    config_path, weights_path, generated = checkpoint
    assert generated["bytes"] > 0

    code, reference = run_node(["reference", "--config", str(config_path), "--weights",
                                str(weights_path), "--prompt", PROMPT])
    assert code == 0, reference
    expected = json.loads(reference.strip().splitlines()[-1])

    node_b, address_b = run_node(["serve", "--config", str(config_path), "--weights",
                                  str(weights_path), "--layers", "2:4", "--listen",
                                  "127.0.0.1:0", "--timeout", "30", "--stats-out",
                                  str(tmp_path / "b.json")], wait_for_ready=True)
    node_a = None
    try:
        node_a, address_a = run_node(["serve", "--config", str(config_path), "--weights",
                                      str(weights_path), "--layers", "0:2", "--listen",
                                      "127.0.0.1:0", "--downstream",
                                      f"{address_b[0]}:{address_b[1]}", "--timeout", "30",
                                      "--stats-out", str(tmp_path / "a.json")],
                                     wait_for_ready=True)
        code, distributed = run_node(["run", "--first-stage", f"{address_a[0]}:{address_a[1]}",
                                      "--prompt", PROMPT, "--model-id",
                                      "lastcore/tiny-cpu", "--out", str(tmp_path / "d.json")])
        assert code == 0, distributed
        result = json.loads(distributed.strip().splitlines()[-1])
        stats_a, stats_b = wait_for_stats([tmp_path / "a.json", tmp_path / "b.json"], served=1)
    finally:
        for process in (node_a, node_b):
            if process is not None:
                process.terminate()
                process.wait(timeout=30)

    assert result["logits_sha256"] == expected["logits_sha256"]
    assert result["next_token"] == expected["next_token"]
    assert result["shape"] == expected["shape"]

    assert stats_a["served"] == 1 and stats_b["served"] == 1
    assert [stats_a["layers"], stats_b["layers"]] == [[0, 2], [2, 4]]
    assert stats_a["parameter_bytes"] + stats_b["parameter_bytes"] == generated["bytes"]
    assert stats_a["parameter_bytes"] != stats_b["parameter_bytes"]


def test_three_stage_chain_agrees_with_the_reference(checkpoint, tmp_path):
    config_path, weights_path, _ = checkpoint
    code, reference = run_node(["reference", "--config", str(config_path), "--weights",
                                str(weights_path), "--prompt", "5 6 7"])
    expected = json.loads(reference.strip().splitlines()[-1])

    processes = []
    try:
        node_c, address = run_node(["serve", "--config", str(config_path), "--weights",
                                    str(weights_path), "--layers", "3:4", "--listen",
                                    "127.0.0.1:0", "--timeout", "30"], wait_for_ready=True)
        processes.append(node_c)
        node_b, address = run_node(["serve", "--config", str(config_path), "--weights",
                                    str(weights_path), "--layers", "1:3", "--listen",
                                    "127.0.0.1:0", "--downstream", f"{address[0]}:{address[1]}",
                                    "--timeout", "30"], wait_for_ready=True)
        processes.append(node_b)
        node_a, address = run_node(["serve", "--config", str(config_path), "--weights",
                                    str(weights_path), "--layers", "0:1", "--listen",
                                    "127.0.0.1:0", "--downstream", f"{address[0]}:{address[1]}",
                                    "--timeout", "30"], wait_for_ready=True)
        processes.append(node_a)
        code, distributed = run_node(["run", "--first-stage", f"{address[0]}:{address[1]}",
                                      "--prompt", "5 6 7"])
        assert code == 0, distributed
        result = json.loads(distributed.strip().splitlines()[-1])
    finally:
        for process in processes[::-1]:
            process.terminate()
            process.wait(timeout=30)

    assert result["logits_sha256"] == expected["logits_sha256"]
