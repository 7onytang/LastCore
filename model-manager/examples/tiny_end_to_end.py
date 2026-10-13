"""端到端演示：生成 tiny 资产 → 物化到内容寻址缓存 → 校验 → 复用引擎 loader 只读本段 → 与单机结果逐字节比对。

运行（不联网、不下载任何外部权重）：

```bash
cd model-manager
PYTHONPATH=.:../inference-engine:../communication python examples/tiny_end_to_end.py
```
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_tiny_asset import build  # noqa: E402

from inference_engine.reference import forward_full  # noqa: E402
from inference_engine.stage import segment_plan  # noqa: E402
from inference_engine.weights import generate_weights  # noqa: E402

from model_manager import (ModelCache, LocalArtifactSource, cost_report,  # noqa: E402
                           load_validated_stage, materialize_model, verify_checkpoint_contents)

PROMPTS = ([1, 2, 3, 4], [7], [0, 63, 5, 8])


def run(workspace: str | Path, *, stages: int = 2, dtype: str = "float32") -> dict:
    workspace = Path(workspace)
    assets = workspace / "assets"
    cache_dir = workspace / "cache"
    manifest = build(assets)
    cache = ModelCache(cache_dir)
    result = materialize_model(manifest, LocalArtifactSource(assets), cache,
                              backend="inference-engine.stage", dtype=dtype)
    verified = result.verified
    checkpoint_info = verify_checkpoint_contents(manifest, verified.checkpoint)
    reference_weights = generate_weights(manifest.model, seed=7)

    comparisons = []
    for prompt in PROMPTS:
        tokens = np.array([prompt], dtype=np.int64)
        expected = forward_full(tokens, reference_weights, manifest.model)
        activation = tokens
        for start, end in segment_plan(manifest.n_layers, stages):
            stage = load_validated_stage(manifest, verified, start, end)
            activation = stage.execute(activation)
        comparisons.append({"prompt": list(prompt),
                            "shape": list(activation.shape),
                            "bit_identical": bool(np.array_equal(activation, expected))})
    report = cost_report(manifest)
    return {
        "cache_root": str(verified.root),
        "revision": manifest.revision,
        "dtype": dtype,
        "fetched": list(result.fetched),
        "checkpoint": checkpoint_info,
        "cost": report.as_dict(),
        "segments": [{"layers": [start, end],
                      "parameter_bytes": manifest.layout.segment_bytes(start, end)}
                     for start, end in segment_plan(manifest.n_layers, stages)],
        "comparisons": comparisons,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lastcore-lc9-") as tmp:
        payload = run(tmp, stages=2)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    ok = all(item["bit_identical"] for item in payload["comparisons"])
    print(f"bit-identical to the single-machine reference: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
