"""Real-subprocess tests for the admission planner CLI.

The CLI is the off-device entrypoint reviewers and CI run, so the metadata gate
is asserted by launching ``python -m lastcore_android_node plan`` in a child
process: a missing/pseudo ``--weights-bytes`` or ``--tokens`` must exit non-zero
and never print an ``accept`` decision, while an explicitly supplied positive
context length (including ``1``) stays a real, accepted value.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SHAPE_ARGS = [
    "--layers", "0:1",
    "--layers-total", "4",
    "--d-model", "32",
    "--heads", "4",
    "--vocab", "64",
]

ANDROID_ENV = {
    "LASTCORE_ANDROID_MACHINE": "arm64",
    "LASTCORE_ANDROID_PLATFORM": "Android",
    "LASTCORE_ANDROID_IS_ANDROID": "true",
    "LASTCORE_ANDROID_THERMAL": "nominal",
    "LASTCORE_ANDROID_TOTAL_MEMORY_MB": "6144",
    "LASTCORE_ANDROID_AVAILABLE_MEMORY_MB": "2560",
}


def run_plan(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(ANDROID_ENV)
    return subprocess.run(
        [sys.executable, "-m", "lastcore_android_node", "plan", "--json", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def assert_rejected(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode != 0, f"expected non-zero rc, got {result.returncode}: {result.stdout}{result.stderr}"
    assert "accept" not in result.stdout, f"a rejected command must not print accept: {result.stdout!r}"


def test_missing_both_metadata_flags_is_rejected():
    # The exact reproduction from the LC-61 review: previously rc 0 / accept.
    assert_rejected(run_plan(*SHAPE_ARGS))


def test_missing_weights_bytes_is_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--tokens", "2048"))


def test_missing_tokens_is_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "4096"))


def test_explicit_zero_weights_bytes_is_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "0", "--tokens", "2048"))


def test_explicit_zero_tokens_is_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "4096", "--tokens", "0"))


def test_negative_metadata_values_are_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "-1", "--tokens", "2048"))
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "4096", "--tokens", "-5"))


def test_non_integer_metadata_values_are_rejected():
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "many", "--tokens", "2048"))
    assert_rejected(run_plan(*SHAPE_ARGS, "--weights-bytes", "4096", "--tokens", "1.5"))


def test_explicit_tokens_of_one_is_a_real_accepted_context_length():
    result = run_plan(*SHAPE_ARGS, "--weights-bytes", "4096", "--tokens", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["decision"] == "accept"
    assert report["budget_mb"]["weights"] > 0


def test_complete_metadata_keeps_structured_decision_and_exit_codes():
    accept = run_plan("--layers", "0:12", "--layers-total", "24", "--d-model", "896",
                      "--heads", "14", "--vocab", "151936", "--weights-bytes", "616000000",
                      "--tokens", "2048")
    assert accept.returncode == 0, accept.stdout + accept.stderr
    assert json.loads(accept.stdout)["decision"] == "accept"

    low_memory = dict(ANDROID_ENV)
    low_memory["LASTCORE_ANDROID_AVAILABLE_MEMORY_MB"] = "700"
    env = dict(os.environ)
    env.update(low_memory)
    rejected = subprocess.run(
        [sys.executable, "-m", "lastcore_android_node", "plan", "--json",
         "--layers", "0:12", "--layers-total", "24", "--d-model", "896", "--heads", "14",
         "--vocab", "151936", "--weights-bytes", "616000000", "--tokens", "2048"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert json.loads(rejected.stdout)["decision"] == "reject_memory"
