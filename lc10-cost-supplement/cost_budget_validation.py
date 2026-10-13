#!/usr/bin/env python3
"""Independent byte-budget examples for the LC-10 scheduler boundary.

This is a static planning sample, not a replacement scheduler and not wired to
Controller reservations or a production ModelManifest.  It checks its source
provenance against the two requested clean revisions, then evaluates one
hand-assigned feasible non-uniform split and one expected fail-fast case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


MIB = 1024 * 1024
DTYPE_BYTES = {"fp32": 4, "fp16": 2, "bf16": 2, "int8": 1}
EXPECTED_SOURCES = {
    "e0f463e883d18e6fab25795804cd503527686d57": {
        "tree": "ed1b45afc1e195c075e9619c577b674641b2a1ff",
        "parent": "672b4f7474e424380645deaef2c1e8e68a7c2771",
    },
    "a878d6388cdd955df4bf3953619e3f056f201391": {
        "tree": "3745d77ded34cfca5409522b8e1f1c16607cf8c7",
        "parent": "e0f463e883d18e6fab25795804cd503527686d57",
    },
}
SCHEDULER_INTERFACE = (
    "def allocate(nodes: list[dict], num_layers: int, "
    "layer_memory_mb: int, max_nodes: int):"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def source_provenance(repo: Path) -> dict:
    commit = git(repo, "rev-parse", "HEAD")
    expected = EXPECTED_SOURCES.get(commit)
    if expected is None:
        raise ValueError(f"unsupported source commit: {commit}")

    tree = git(repo, "rev-parse", "HEAD^{tree}")
    parent = git(repo, "rev-parse", "HEAD^")
    if tree != expected["tree"] or parent != expected["parent"]:
        raise ValueError("source tree or direct parent does not match the pin")

    dirty = git(repo, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise ValueError("source checkout is dirty; refusing to label the sample")

    scheduler = git(repo, "show", f"{commit}:controller-server/app/scheduler.py")
    if SCHEDULER_INTERFACE not in scheduler:
        raise ValueError("pinned scheduler interface differs from the recorded boundary")

    return {
        "commit": commit,
        "tree": tree,
        "parent": parent,
        "clean_checkout": True,
        "current_scheduler_interface": "uniform layer_memory_mb; sample is not integrated",
    }


def stage_costs(
    model: dict,
    start: int,
    end: int,
    seq_len: int,
    batch: int,
    activation_factor: int = 1,
) -> dict:
    layers = model["layer_bytes"]
    if not (0 <= start < end <= model["n_layers"]):
        raise ValueError("stage range must be a non-empty half-open layer interval")

    dtype_bytes = DTYPE_BYTES[model["dtype"]]
    layer_weights = sum(layers[start:end])
    embedding = model["embedding_bytes"] if start == 0 else 0
    head = model["head_bytes"] if end == model["n_layers"] else 0
    weights = layer_weights + embedding + head
    kv_per_token_per_layer = (
        2 * model["n_kv_heads"] * model["head_dim"] * dtype_bytes
    )
    kv = kv_per_token_per_layer * (end - start) * seq_len * batch
    activation = (
        batch * seq_len * model["d_model"] * dtype_bytes * activation_factor
    )
    return {
        "layer_range": [start, end],
        "layer_weights_bytes": layer_weights,
        "embedding_bytes": embedding,
        "head_bytes": head,
        "weights_bytes": weights,
        "kv_bytes_per_token_per_layer": kv_per_token_per_layer,
        "kv_cache_bytes": kv,
        "activation_bytes": activation,
        "peak_bytes": weights + kv + activation,
    }


def feasible_nonuniform_example() -> dict:
    # A synthetic fp16 manifest-shaped input.  The two equal-memory stages
    # receive different layer weights; embedding and head live at the ends.
    model = {
        "dtype": "fp16",
        "d_model": 512,
        "n_layers": 4,
        "n_kv_heads": 8,
        "head_dim": 64,
        "embedding_bytes": 4096 * 512 * 2,
        "head_bytes": 4096 * 512 * 2,
        "layer_bytes": [8 * MIB, 24 * MIB, 12 * MIB, 20 * MIB],
    }
    seq_len = 2048
    batch = 1
    budget = 64 * MIB
    assignments = [("node-a", 0, 2), ("node-b", 2, 4)]
    stages = []
    for node_id, start, end in assignments:
        costs = stage_costs(model, start, end, seq_len, batch)
        costs.update(
            {
                "node_id": node_id,
                "available_memory_bytes": budget,
                "fits": costs["peak_bytes"] <= budget,
                "headroom_bytes": budget - costs["peak_bytes"],
            }
        )
        stages.append(costs)

    contiguous = assignments[0][1] == 0 and assignments[-1][2] == model["n_layers"]
    contiguous = contiguous and all(
        assignments[i][2] == assignments[i + 1][1]
        for i in range(len(assignments) - 1)
    )
    nonuniform = len(set(model["layer_bytes"])) > 1
    accepted = contiguous and nonuniform and all(stage["fits"] for stage in stages)
    if not accepted:
        raise AssertionError("feasible sample did not fit its declared byte budgets")

    return {
        "fixture": "synthetic_nonuniform_two_stage_fit",
        "model": model,
        "seq_len": seq_len,
        "batch": batch,
        "activation_factor": 1,
        "assignments_are_contiguous": contiguous,
        "layer_weights_are_nonuniform": nonuniform,
        "stages": stages,
        "accepted_as_expected": accepted,
        "limitation": "illustrative byte arithmetic; not runtime or production capacity evidence",
    }


def expected_over_budget_example() -> dict:
    # This independently derives the historical layer-0 failure in the
    # preserved cost_model_samples.py fixture without running or changing it.
    dtype_bytes = DTYPE_BYTES["fp16"]
    model = {
        "dtype": "fp16",
        "d_model": 1024,
        "n_layers": 8,
        "n_kv_heads": 8,
        "head_dim": 128,
        "embedding_bytes": 32_000 * 1024 * dtype_bytes,
        "head_bytes": 32_000 * 1024 * dtype_bytes,
        "layer_bytes": [8 * MIB, 8 * MIB, 8 * MIB, 64 * MIB]
        + [8 * MIB] * 4,
    }
    seq_len = 512
    batch = 1
    budget = 64 * MIB
    costs = stage_costs(model, 0, 1, seq_len, batch)
    deficit = max(0, costs["peak_bytes"] - budget)
    rejected_as_expected = deficit > 0
    if not rejected_as_expected:
        raise AssertionError("over-budget first layer was not rejected")

    return {
        "fixture": "wide_model_first_layer_fail_fast",
        "model": model,
        "seq_len": seq_len,
        "batch": batch,
        "activation_factor": 1,
        "node_budget_bytes": budget,
        "diagnostic": {
            **costs,
            "fits": False,
            "deficit_bytes": deficit,
            "fail_fast": True,
            "reason": "no node can hold the next layer plus embedding, KV, and activation bytes",
        },
        "rejected_as_expected": rejected_as_expected,
        "historical_comparison": {
            "preserved_original": "lc10/cost_model_samples.py",
            "recorded_original_result": "exit 1 at layer 0; no cost JSON was produced",
            "reran_or_modified_original": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path, help="clean pinned LastCore checkout")
    parser.add_argument("--output", required=True, type=Path, help="JSON output outside the source checkout")
    args = parser.parse_args()

    try:
        provenance = source_provenance(args.repo.resolve())
        result = {
            "schema": "lc10-cost-budget-validation-v1",
            "source": provenance,
            "formula": {
                "weights": "sum(assigned layer bytes) + embedding on first stage + head on last stage",
                "kv": "2 * n_kv_heads * head_dim * dtype_bytes * assigned_layers * seq_len * batch",
                "activation": "batch * seq_len * d_model * dtype_bytes * activation_factor",
                "peak": "weights + kv + activation",
                "units": "integer bytes; MiB = 1024 * 1024 bytes",
            },
            "feasible_sample": feasible_nonuniform_example(),
            "over_budget_sample": expected_over_budget_example(),
            "scope": {
                "exit_zero_means": "both arithmetic expectations passed: the synthetic split fits and the oversized layer is rejected",
                "controller_changed": False,
                "production_manifest_integrated": False,
                "snapshot_concurrency_or_reservation_tested": False,
                "lc6_lc9_integration_tested": False,
            },
        }
        payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        sys.stdout.write(payload)
        return 0
    except (OSError, subprocess.CalledProcessError, ValueError, AssertionError) as exc:
        print(f"cost-budget-validation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
