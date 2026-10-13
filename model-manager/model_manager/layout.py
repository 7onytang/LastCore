"""基于引擎参数布局的字节核算。

本模块只依赖 ``inference_engine`` 的形状定义（``parameter_shapes`` /
``segment_parameter_names``），把"一份 checkpoint 有多大、每层多大、某个层区间多大"
算成可核对的数字。清单里的所有字节声明都必须与这里的计算结果一致，任何一方被改动
都会被交叉核对发现，而不是被静默接受。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod
from typing import Sequence

from inference_engine.config import ModelConfig
from inference_engine.weights import (SHARD_DTYPES, all_parameter_names, parameter_shapes,
                                      segment_parameter_names)

from .errors import ManifestError

DTYPE_BYTES = {"float32": 4, "float16": 2}
MODULE_KEYS = ("embedding", "head", "layers")


def dtype_bytes(dtype: str) -> int:
    try:
        return DTYPE_BYTES[dtype]
    except KeyError as exc:
        raise ManifestError(f"dtype {dtype!r} is not one of {sorted(DTYPE_BYTES)}") from exc


def check_dtype(dtype: object) -> str:
    if not isinstance(dtype, str) or dtype not in SHARD_DTYPES:
        raise ManifestError(f"dtype must be one of {list(SHARD_DTYPES)}, got {dtype!r}")
    return dtype


@dataclass(frozen=True)
class Layout:
    """一份完整 checkpoint 的字节分布（不含 npz 容器自身的开销）。"""

    config: ModelConfig = field(compare=False, repr=False)
    dtype: str = "float32"
    dtype_bytes: int = 4
    n_tensors: int = 0
    per_layer_bytes: tuple[int, ...] = ()
    embedding_bytes: int = 0
    head_bytes: int = 0
    parameter_bytes: int = 0
    tensor_names: tuple[str, ...] = ()

    @property
    def n_layers(self) -> int:
        return self.config.n_layers

    @property
    def layers_bytes(self) -> int:
        return sum(self.per_layer_bytes)

    @property
    def module_bytes(self) -> dict[str, int]:
        return {"embedding": self.embedding_bytes, "head": self.head_bytes,
                "layers": self.layers_bytes}

    @property
    def shapes(self) -> dict[str, tuple[int, ...]]:
        return parameter_shapes(self.config)

    def segment_names(self, layer_start: int, layer_end: int) -> Sequence[str]:
        return segment_parameter_names(self.config, layer_start, layer_end)

    def segment_bytes(self, layer_start: int, layer_end: int) -> int:
        shapes = self.shapes
        return sum(prod(shapes[name]) for name in self.segment_names(layer_start, layer_end)) \
            * self.dtype_bytes


def layout_for(config: ModelConfig, dtype: str) -> Layout:
    """按引擎的参数形状表算出整份 checkpoint 的字节分布。"""
    size = dtype_bytes(check_dtype(dtype))
    shapes = parameter_shapes(config)
    names = all_parameter_names(config)
    if set(shapes) != set(names):
        raise ManifestError("engine parameter shapes do not cover every parameter name")
    per_layer: list[int] = []
    for layer in range(config.n_layers):
        prefix = f"layer.{layer}."
        per_layer.append(
            sum(prod(shapes[name]) for name in names if name.startswith(prefix)) * size)
    embedding = sum(prod(shapes[name]) for name in names if name.startswith("embed.")) * size
    head = sum(prod(shapes[name]) for name in names if name.startswith("head.")) * size
    parameter_bytes = sum(prod(shapes[name]) for name in names) * size
    return Layout(config=config, dtype=dtype, dtype_bytes=size, n_tensors=len(names),
                  per_layer_bytes=tuple(per_layer), embedding_bytes=embedding, head_bytes=head,
                  parameter_bytes=parameter_bytes, tensor_names=tuple(names))


def segment_bytes(config: ModelConfig, dtype: str, layer_start: int, layer_end: int) -> int:
    """层区间 ``[layer_start, layer_end)`` 需要的参数字节（含首段 embedding、末段 head）。"""
    return layout_for(config, dtype).segment_bytes(layer_start, layer_end)
