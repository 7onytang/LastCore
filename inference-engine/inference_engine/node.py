"""Command line entry point for local two-node integration.

    # one-time: create a tiny CPU-verifiable checkpoint
    python -m inference_engine.node generate --config tiny.json --out tiny.npz

    # node B: last stage, listens for its upstream peer
    python -m inference_engine.node serve --config tiny.json --weights tiny.npz \
        --layers 2:4 --listen 127.0.0.1:0

    # node A: first stage, forwards to node B
    python -m inference_engine.node serve --config tiny.json --weights tiny.npz \
        --layers 0:2 --listen 127.0.0.1:0 --downstream 127.0.0.1:<portB>

    # caller: send a prompt to node A and print the resulting logits
    python -m inference_engine.node run --first-stage 127.0.0.1:<portA> --prompt "1 2 3"

    # single-machine reference for the same checkpoint and prompt
    python -m inference_engine.node reference --config tiny.json --weights tiny.npz --prompt "1 2 3"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
from pathlib import Path

import numpy as np

from .config import ModelConfig, tiny_config
from .errors import TaskCancelled
from .pipeline import PipelineClient, StageServer
from .reference import forward_full
from .stage import load_stage
from .weights import load_shard, generate_weights, save_weights


def _config(args) -> ModelConfig:
    return ModelConfig.load(args.config) if args.config else tiny_config()


def _tokens(prompt: str) -> np.ndarray:
    values = [int(part) for part in prompt.replace(",", " ").split()]
    if not values:
        raise SystemExit("prompt must contain at least one token id")
    return np.asarray([values], dtype=np.int64)


def _summary(logits: np.ndarray) -> dict:
    array = np.asarray(logits, dtype=np.float32)
    digest = hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()
    return {
        "shape": list(array.shape),
        "logits_sha256": digest,
        "next_token": int(np.argmax(array[0, -1])),
        "head": [round(float(value), 6) for value in array[0, -1, :4]],
    }


def _parse_ranges(value: str) -> tuple[int, int]:
    start, _, end = value.partition(":")
    if not start.isdigit() or not end.isdigit():
        raise SystemExit(f"layers must look like start:end, got {value!r}")
    return int(start), int(end)


def _address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"address must look like host:port, got {value!r}")
    return host, int(port)


def _emit(payload: dict, path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print(text, flush=True)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")


def command_generate(args) -> int:
    config = _config(args)
    if args.write_config:
        config.save(args.write_config)
    weights = generate_weights(config, seed=args.seed, dtype=args.dtype)
    save_weights(args.out, weights)
    _emit({"config": config.to_dict(), "parameters": len(weights),
           "bytes": int(sum(value.nbytes for value in weights.values())),
           "path": str(args.out)}, None)
    return 0


def command_serve(args) -> int:
    config = _config(args)
    layer_start, layer_end = _parse_ranges(args.layers)
    runtime = load_stage(config, args.weights, layer_start, layer_end,
                         task_id=args.task_id, assignment_id=args.assignment_id)
    downstream = _address(args.downstream) if args.downstream else None
    host, port = _address(args.listen)
    server = StageServer(runtime, address=(host, port), downstream=downstream, timeout=args.timeout)
    stop = {"requested": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    print(f"READY {server.address[0]} {server.address[1]} layers={layer_start}:{layer_end} "
          f"role={runtime.role} parameters={runtime.parameter_bytes}B", flush=True)
    exit_code = 0
    def emit_stats() -> None:
        payload = {"served": server.processed, "layers": [layer_start, layer_end],
                   "parameter_bytes": runtime.parameter_bytes,
                   "description": runtime.describe()}
        if args.stats_out:
            Path(args.stats_out).write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8")
    try:
        server.serve_forever(max_requests=args.max_requests,
                             should_stop=lambda: stop["requested"],
                             on_served=emit_stats)
    except (KeyboardInterrupt, TaskCancelled):
        pass
    except Exception as exc:  # noqa: BLE001 - report the failure instead of a traceback
        _emit({"error": f"{type(exc).__name__}: {exc}"}, None)
        exit_code = 1
    finally:
        server.close()
    _emit({"served": server.processed, "layers": [layer_start, layer_end],
           "parameter_bytes": runtime.parameter_bytes,
           "description": runtime.describe()}, args.stats_out)
    return exit_code


def command_run(args) -> int:
    client = PipelineClient(_address(args.first_stage), timeout=args.timeout,
                            model_id=args.model_id, task_id=args.task_id,
                            assignment_id=args.assignment_id)
    logits = client.run(_tokens(args.prompt))
    _emit(_summary(logits), args.out)
    return 0


def command_reference(args) -> int:
    config = _config(args)
    weights = load_shard(args.weights, config, 0, config.n_layers)
    logits = forward_full(_tokens(args.prompt), weights, config)
    payload = _summary(logits)
    payload["parameter_bytes"] = weights.nbytes
    _emit(payload, args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inference_engine.node", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="create a deterministic tiny checkpoint")
    generate.add_argument("--config", help="model config JSON; defaults to the built-in tiny model")
    generate.add_argument("--write-config", help="also write the model config to this path")
    generate.add_argument("--out", required=True, help="checkpoint path (.npz)")
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--dtype", default="float32", choices=("float32", "float16"))
    generate.set_defaults(func=command_generate)

    serve = sub.add_parser("serve", help="serve one layer segment over TCP")
    serve.add_argument("--config")
    serve.add_argument("--weights", required=True)
    serve.add_argument("--layers", required=True, help="inclusive start, exclusive end, e.g. 0:2")
    serve.add_argument("--listen", default="127.0.0.1:0", help="address other nodes can reach")
    serve.add_argument("--downstream", help="next stage's endpoint; omit on the last stage")
    serve.add_argument("--timeout", type=float, default=30.0,
                       help="per-request timeout once a peer connected; idle polling is separate")
    serve.add_argument("--max-requests", type=int)
    serve.add_argument("--stats-out")
    serve.add_argument("--task-id")
    serve.add_argument("--assignment-id")
    serve.set_defaults(func=command_serve)

    run = sub.add_parser("run", help="send a prompt to the first stage")
    run.add_argument("--first-stage", required=True)
    run.add_argument("--prompt", default="1 2 3 4")
    run.add_argument("--timeout", type=float, default=30.0)
    run.add_argument("--out")
    run.add_argument("--model-id")
    run.add_argument("--task-id")
    run.add_argument("--assignment-id")
    run.set_defaults(func=command_run)

    reference = sub.add_parser("reference", help="single-process forward pass")
    reference.add_argument("--config")
    reference.add_argument("--weights", required=True)
    reference.add_argument("--prompt", default="1 2 3 4")
    reference.add_argument("--out")
    reference.set_defaults(func=command_reference)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
