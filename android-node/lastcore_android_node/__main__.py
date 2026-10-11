"""CLI: print an admission/budget report for a candidate Android shard.

    python -m lastcore_android_node plan --layers 2:4 --layers-total 4 \
        --d-model 896 --heads 14 --vocab 151936 --weights-bytes 320000000 \
        --tokens 128 --battery 45 --thermal nominal

Runs with no device, no model file and no third-party dependency, so the
admission policy can be inspected on any dev machine or CI.
"""

from __future__ import annotations

import argparse
import json
import sys

from .backend import BackendUnavailable, default_adapters, select_backend
from .budget import ModelShape, build_budget, evaluate
from .device import ThermalState, detect
from .executor import DEFAULT_DTYPE_BYTES


def _parse_layers(value: str) -> tuple[int, int]:
    start, _, end = value.partition(":")
    if not start.strip().isdigit() or not end.strip().isdigit():
        raise argparse.ArgumentTypeError(f"layers must look like start:end, got {value!r}")
    start_i, end_i = int(start), int(end)
    if end_i <= start_i:
        raise argparse.ArgumentTypeError(f"empty layer segment {value!r}")
    return start_i, end_i


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lastcore_android_node", description=__doc__)
    parser.add_argument("command", nargs="?", choices=["plan"], default="plan",
                        help="report the admission decision for a candidate shard")
    parser.add_argument("--layers", type=_parse_layers, required=True, help="segment as start:end, e.g. 2:4")
    parser.add_argument("--layers-total", type=int, required=True)
    parser.add_argument("--d-model", type=int, required=True)
    parser.add_argument("--heads", type=int, required=True)
    parser.add_argument("--vocab", type=int, required=True)
    parser.add_argument("--weights-bytes", type=int, default=0, help="bytes of this shard's weights")
    parser.add_argument("--tokens", type=int, default=1)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--dtype", choices=sorted(DEFAULT_DTYPE_BYTES), default="float16")
    parser.add_argument("--runtime-overhead-mb", type=int, default=128)
    parser.add_argument("--battery", type=int, default=None)
    parser.add_argument("--thermal", choices=[state.value for state in ThermalState], default=None)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = detect()
    if args.battery is not None or args.thermal is not None:
        from dataclasses import replace

        profile = replace(
            profile,
            battery_percent=args.battery if args.battery is not None else profile.battery_percent,
            thermal=ThermalState.parse(args.thermal) if args.thermal is not None else profile.thermal,
        )

    shape = ModelShape(num_layers=args.layers_total, d_model=args.d_model,
                       num_heads=args.heads, vocab=args.vocab)
    layer_start, layer_end = args.layers
    budget = build_budget(
        layer_end - layer_start,
        shard_weight_bytes=args.weights_bytes,
        shape=shape,
        batch=args.batch,
        tokens=args.tokens,
        dtype_bytes=DEFAULT_DTYPE_BYTES[args.dtype],
        runtime_overhead_mb=args.runtime_overhead_mb,
    )
    decision, reason = evaluate(profile, budget)
    try:
        backend = select_backend(profile, default_adapters(), dtype=args.dtype)
        backend_name = backend.capabilities.name
    except BackendUnavailable as exc:
        backend_name = f"unavailable: {exc}"

    report = {
        "device": {
            "platform": profile.platform_name,
            "machine": profile.machine,
            "is_android": profile.is_android,
            "is_arm64": profile.is_arm64,
            "cpu_count": profile.cpu_count,
            "total_memory_mb": profile.total_memory_mb,
            "available_memory_mb": profile.available_memory_mb,
            "battery_percent": profile.battery_percent,
            "thermal": profile.thermal.value,
        },
        "segment": {"layer_start": layer_start, "layer_end": layer_end, "num_layers": args.layers_total},
        "budget_mb": {
            "weights": budget.weights_bytes / 1048576,
            "activation": budget.activation_bytes / 1048576,
            "kv": budget.kv_bytes / 1048576,
            "runtime_overhead": budget.runtime_overhead_bytes / 1048576,
            "total": budget.total_mb,
        },
        "decision": decision.value,
        "reason": reason,
        "backend": backend_name,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0 if decision.value == "accept" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
