"""把校验后的本地 revision 接给现有引擎 loader。

本模块不复制引擎的算子或权重映射，只做三件事：

* 用引擎自己的 ``ModelConfig`` / ``parameter_shapes`` / ``all_parameter_names`` 交叉核对
  本地 checkpoint 的内容（参数齐全、形状相符、dtype 相符、无多余张量）；
* 用引擎现有的 ``load_shard`` / ``load_stage`` 加载请求的层区间，因此节点永远只读自己那一段；
* 校验加载出来的字节数与本清单声明的层字节一致，把"清单说的"和"loader 实际读的"绑在一起。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from inference_engine.config import ModelConfig
from inference_engine.errors import ShardError
from inference_engine.stage import StageRuntime, load_stage
from inference_engine.weights import (ShardWeights, all_parameter_names, load_shard,
                                      parameter_shapes)

from .cache import VerifiedModel
from .errors import EngineAdapterError
from .manifest import Artifact, ModelManifest


def model_config(manifest: ModelManifest) -> ModelConfig:
    """清单里的模型形状已在解析时经 ``inference_engine.config.ModelConfig`` 校验。"""
    return manifest.model


def expected_arrays(manifest: ModelManifest) -> dict[str, tuple[int, ...]]:
    return parameter_shapes(model_config(manifest))


def verify_checkpoint_contents(manifest: ModelManifest, path: str | Path) -> dict:
    """用引擎的参数表核对 checkpoint 内容；损坏 npz 或内容不符都明确失败。"""
    config = model_config(manifest)
    shapes = parameter_shapes(config)
    expected = set(all_parameter_names(config))
    try:
        archive = np.load(path)
    except (OSError, ValueError) as exc:
        raise EngineAdapterError(f"checkpoint {path} is not a readable npz archive: {exc}") from exc
    try:
        names = set(archive.files)
        missing = sorted(expected - names)
        if missing:
            raise EngineAdapterError(f"checkpoint {path} is missing {len(missing)} parameter(s), "
                                     f"first: {missing[0]}")
        unexpected = sorted(names - expected)
        if unexpected:
            raise EngineAdapterError(f"checkpoint {path} has {len(unexpected)} unexpected "
                                     f"tensor(s), first: {unexpected[0]}")
        tensor_bytes = 0
        for name in sorted(expected):
            array = archive[name]
            if tuple(array.shape) != shapes[name]:
                raise EngineAdapterError(
                    f"parameter {name} has shape {tuple(array.shape)}, expected {shapes[name]}")
            if array.dtype.name != manifest.dtype:
                raise EngineAdapterError(
                    f"parameter {name} has dtype {array.dtype.name}, manifest declares "
                    f"{manifest.dtype}")
            tensor_bytes += int(array.nbytes)
    finally:
        archive.close()
    if tensor_bytes != manifest.resources.parameter_bytes:
        raise EngineAdapterError(
            f"checkpoint {path} holds {tensor_bytes} parameter bytes but the manifest declares "
            f"{manifest.resources.parameter_bytes}")
    return {"path": str(path), "tensors": len(expected), "parameter_bytes": tensor_bytes,
            "dtype": manifest.dtype}


def checkpoint_artifact(manifest: ModelManifest, layer_start: int, layer_end: int) -> Artifact:
    """找出唯一覆盖该层区间的 checkpoint artifact。"""
    if not 0 <= layer_start < layer_end <= manifest.n_layers:
        raise EngineAdapterError(f"segment [{layer_start}, {layer_end}) is outside "
                                 f"0..{manifest.n_layers}")
    candidates = [artifact for artifact in manifest.checkpoints
                  if (artifact.covers() or (0, manifest.n_layers))[0] <= layer_start
                  and (artifact.covers() or (0, manifest.n_layers))[1] >= layer_end]
    if not candidates:
        raise EngineAdapterError(f"no checkpoint artifact covers segment [{layer_start}, "
                                 f"{layer_end})")
    if len(candidates) > 1:
        raise EngineAdapterError(f"{len(candidates)} checkpoint artifacts cover segment "
                                 f"[{layer_start}, {layer_end}); the engine loader needs exactly one")
    return candidates[0]


def _load_shard(manifest: ModelManifest, path: Path, layer_start: int, layer_end: int) -> ShardWeights:
    try:
        shard = load_shard(path, model_config(manifest), layer_start, layer_end,
                           dtype=manifest.dtype)
    except ShardError as exc:
        raise EngineAdapterError(f"engine loader rejected {path}: {exc}") from exc
    expected = manifest.layout.segment_bytes(layer_start, layer_end)
    if shard.nbytes != expected:
        raise EngineAdapterError(
            f"segment [{layer_start}, {layer_end}) loaded {shard.nbytes} bytes but the manifest "
            f"declares {expected} bytes for dtype {manifest.dtype}")
    return shard


def load_validated_shard(manifest: ModelManifest, verified: VerifiedModel, layer_start: int,
                         layer_end: int) -> ShardWeights:
    """核对 checkpoint 内容后，用引擎现有 ``load_shard`` 只读该段权重。"""
    artifact = checkpoint_artifact(manifest, layer_start, layer_end)
    path = verified.path_of(artifact.path)
    verify_checkpoint_contents(manifest, path)
    return _load_shard(manifest, path, layer_start, layer_end)


def load_validated_stage(manifest: ModelManifest, verified: VerifiedModel, layer_start: int,
                         layer_end: int, *, task_id: str | None = None,
                         assignment_id: str | None = None) -> StageRuntime:
    """核对内容后走引擎现有的 ``load_stage``，得到一个可直接执行的段。"""
    artifact = checkpoint_artifact(manifest, layer_start, layer_end)
    path = verified.path_of(artifact.path)
    verify_checkpoint_contents(manifest, path)
    shard = _load_shard(manifest, path, layer_start, layer_end)
    try:
        runtime = load_stage(model_config(manifest), path, layer_start, layer_end,
                             task_id=task_id, assignment_id=assignment_id)
    except ShardError as exc:
        raise EngineAdapterError(f"engine loader rejected {path}: {exc}") from exc
    if runtime.parameter_bytes != shard.nbytes:
        raise EngineAdapterError("stage parameter bytes differ from the validated shard bytes")
    return runtime


__all__ = ["model_config", "expected_arrays", "verify_checkpoint_contents",
           "checkpoint_artifact", "load_validated_shard", "load_validated_stage"]
