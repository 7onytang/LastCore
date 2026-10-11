"""LC-11 集成客户端：把 prompt 送进首段节点的真实 StageServer，并与单机参考对照。

用法：
    PYTHONPATH=node-agent:inference-engine:communication \
      python integration/pipeline_client.py --endpoint 127.0.0.1:9000 \
        --config run/tiny.json --weights run/tiny.npz --prompt "1 2 3 4" --out run/result.json

输出 JSON：logits 摘要、单机对照结果（逐字节）、TTFT/总耗时、tokens/s 与传输字节估算。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for relative in ("inference-engine", "communication"):
    candidate = str(ROOT / relative)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import numpy as np  # noqa: E402

from inference_engine import PipelineClient, forward_full, load_shard  # noqa: E402
from inference_engine.config import ModelConfig  # noqa: E402


def parse_tokens(prompt: str) -> list[int]:
    values = [int(part) for part in prompt.replace(",", " ").split()]
    if not values:
        raise SystemExit("prompt must contain at least one token id")
    return values


def endpoint_of(value: str) -> tuple[str, int]:
    parsed = urlparse(value if "://" in value else f"//{value}")
    if not parsed.hostname or not parsed.port:
        raise SystemExit(f"endpoint must be host:port or http://host:port, got {value!r}")
    return parsed.hostname, parsed.port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="首段节点注册的 endpoint")
    parser.add_argument("--config", required=True, help="tiny 模型配置 JSON")
    parser.add_argument("--weights", required=True, help="tiny 权重 .npz")
    parser.add_argument("--prompt", default="1 2 3 4")
    parser.add_argument("--out", help="结果 JSON 输出路径")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    config = ModelConfig.load(args.config)
    tokens = np.array([parse_tokens(args.prompt)], dtype=np.int64)
    weights = load_shard(args.weights, config, 0, config.n_layers)

    client = PipelineClient(endpoint_of(args.endpoint), timeout=args.timeout, model_id=config.model_id)
    started = time.monotonic()
    logits = client.run(tokens)
    elapsed = time.monotonic() - started

    expected = forward_full(tokens, weights, config)
    hidden_bytes = int(tokens.shape[0] * tokens.shape[1] * config.d_model * 4)
    payload_bytes = {
        "tokens": int(tokens.nbytes),
        "hidden_hop_estimate": hidden_bytes,
        "logits": int(np.asarray(logits).nbytes),
    }
    report = {
        "endpoint": args.endpoint,
        "model_id": config.model_id,
        "prompt": tokens.tolist(),
        "shape": list(np.asarray(logits).shape),
        "next_token": int(np.argmax(logits[0, -1])),
        "bit_identical_to_single_machine": bool(np.array_equal(logits, expected)),
        "max_abs_diff": float(np.max(np.abs(np.asarray(logits) - expected))),
        "ttft_seconds": elapsed,
        "total_seconds": elapsed,
        "tokens_per_second": float(tokens.shape[1] / elapsed) if elapsed > 0 else None,
        "payload_bytes": payload_bytes,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0 if report["bit_identical_to_single_machine"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
