"""生成 tiny 模型样例资产：``tiny.npz`` + ``tokenizer.json`` + ``tiny_manifest.json``。

不下载任何权重：所有权重由 inference-engine 的 ``generate_weights(seed)`` 确定性生成，
因此同一 numpy 版本下 checkpoint 逐字节可复现，样例清单里的 sha256 可被任何人重算核对。
"""

from __future__ import annotations

import json
from pathlib import Path

from inference_engine.weights import generate_weights, save_weights

from model_manager.layout import layout_for
from model_manager.manifest import ModelManifest
from model_manager.publish import ArtifactInput, build_manifest
from inference_engine.config import tiny_config

SEED = 7
REVISION = "tiny-20261011-seed7"
MODEL_ID = "lastcore/tiny-cpu"


def tokenizer_payload() -> dict:
    return {
        "kind": "integer-token-ids",
        "special_tokens": {"pad": 0, "eos": 1},
        "vocab_size": 64,
        "notes": "tiny 模型直接接受 0..63 的整数 token id；本文件只声明词表大小，"
                 "不含字节对编码表。",
    }


def build(asset_dir: str | Path, *, seed: int = SEED, revision: str = REVISION) -> ModelManifest:
    target = Path(asset_dir)
    target.mkdir(parents=True, exist_ok=True)
    config = tiny_config(MODEL_ID)
    save_weights(target / "tiny.npz", generate_weights(config, seed=seed))
    (target / "tokenizer.json").write_text(
        json.dumps(tokenizer_payload(), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    layout = layout_for(config, "float32")
    manifest = build_manifest(
        model_id=MODEL_ID,
        revision=revision,
        dtype="float32",
        model=config,
        artifacts=[
            ArtifactInput(file=target / "tiny.npz", role="checkpoint", path="tiny.npz",
                          layer_start=0, layer_end=config.n_layers,
                          media_type="application/x-npz"),
            ArtifactInput(file=target / "tokenizer.json", role="tokenizer",
                          path="tokenizer.json", media_type="application/json"),
        ],
        license={
            "name": "LastCore generated tiny weights (no third-party weights)",
            "identifier": None,
            "source_url": None,
            "redistributable": True,
            "notes": "由 generate_weights(seed=7) 确定性生成，不含任何第三方权重；"
                     "仓库级许可证未在本任务中选定，这里只如实记录来源与可再分发性。",
        },
        backends={
            "reference": {"dtypes": ["float32", "float16"], "max_seq_len": config.max_seq_len,
                          "notes": "inference-engine 单机对照实现"},
            "inference-engine.stage": {"dtypes": ["float32", "float16"],
                                       "max_seq_len": config.max_seq_len,
                                       "runtime_memory_bytes": 1 << 20,
                                       "notes": "inference-engine StageServer 分段执行"},
        },
        publisher="engineer-b / LC-9",
        notes=f"tiny 样例 revision；{layout.n_tensors} 个参数张量，"
              f"{layout.parameter_bytes} 参数字节（float32）。",
    )
    manifest.save(target / "tiny_manifest.json")
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="生成 tiny 模型样例资产与样例清单")
    parser.add_argument("--out", default="examples/assets")
    args = parser.parse_args(argv)
    manifest = build(args.out)
    print(json.dumps({"asset_dir": args.out, "model_id": manifest.model_id,
                      "revision": manifest.revision,
                      "parameter_bytes": manifest.resources.parameter_bytes,
                      "container_bytes": manifest.resources.container_bytes},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
