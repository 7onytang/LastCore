"""Weight layout, generation and per-segment loading.

A checkpoint is one ``.npz`` holding every parameter under a stable name. A stage
opens the checkpoint lazily and reads only the names its layer segment needs, so a
node never materialises the layers it does not run.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import ModelConfig
from .errors import ShardError

INIT_STD = 0.02
SHARD_DTYPES = ("float32", "float16")


def layer_parameter_names(layer: int) -> tuple[str, ...]:
    prefix = f"layer.{layer}."
    return (f"{prefix}ln1.g", f"{prefix}ln1.b",
            f"{prefix}attn.wqkv", f"{prefix}attn.bqkv", f"{prefix}attn.wo", f"{prefix}attn.bo",
            f"{prefix}ffn.w1", f"{prefix}ffn.b1", f"{prefix}ffn.w2", f"{prefix}ffn.b2",
            f"{prefix}ln2.g", f"{prefix}ln2.b")


EMBED_PARAMETER_NAMES = ("embed.token", "embed.position")
HEAD_PARAMETER_NAMES = ("head.ln.g", "head.ln.b", "head.proj")


def segment_parameter_names(config: ModelConfig, layer_start: int, layer_end: int) -> list[str]:
    """Parameter names required by the segment ``[layer_start, layer_end)``."""
    if not 0 <= layer_start < layer_end <= config.n_layers:
        raise ShardError(f"segment [{layer_start}, {layer_end}) is outside 0..{config.n_layers}")
    names: list[str] = []
    if layer_start == 0:
        names.extend(EMBED_PARAMETER_NAMES)
    for layer in range(layer_start, layer_end):
        names.extend(layer_parameter_names(layer))
    if layer_end == config.n_layers:
        names.extend(HEAD_PARAMETER_NAMES)
    return names


def all_parameter_names(config: ModelConfig) -> list[str]:
    return segment_parameter_names(config, 0, config.n_layers)


def parameter_shapes(config: ModelConfig) -> dict[str, tuple[int, ...]]:
    shapes = {
        "embed.token": (config.vocab_size, config.d_model),
        "embed.position": (config.max_seq_len, config.d_model),
        "head.ln.g": (config.d_model,),
        "head.ln.b": (config.d_model,),
        "head.proj": (config.vocab_size, config.d_model),
    }
    for layer in range(config.n_layers):
        prefix = f"layer.{layer}."
        shapes.update({
            f"{prefix}ln1.g": (config.d_model,),
            f"{prefix}ln1.b": (config.d_model,),
            f"{prefix}attn.wqkv": (config.d_model, 3 * config.d_model),
            f"{prefix}attn.bqkv": (3 * config.d_model,),
            f"{prefix}attn.wo": (config.d_model, config.d_model),
            f"{prefix}attn.bo": (config.d_model,),
            f"{prefix}ffn.w1": (config.d_model, config.d_ff),
            f"{prefix}ffn.b1": (config.d_ff,),
            f"{prefix}ffn.w2": (config.d_ff, config.d_model),
            f"{prefix}ffn.b2": (config.d_model,),
            f"{prefix}ln2.g": (config.d_model,),
            f"{prefix}ln2.b": (config.d_model,),
        })
    return shapes


def generate_weights(config: ModelConfig, seed: int = 0, dtype: str = "float32") -> dict[str, np.ndarray]:
    if dtype not in SHARD_DTYPES:
        raise ShardError(f"dtype must be one of {SHARD_DTYPES}")
    shapes = parameter_shapes(config)
    weights: dict[str, np.ndarray] = {}
    for index, name in enumerate(all_parameter_names(config)):
        rng = np.random.default_rng([seed, index])
        if name.endswith(".g") or name == "head.ln.g":
            values = np.ones(shapes[name], dtype=np.float32)
        elif name.endswith(".b") or name == "head.ln.b":
            values = np.zeros(shapes[name], dtype=np.float32)
        else:
            values = rng.normal(0.0, INIT_STD, size=shapes[name]).astype(np.float32)
        weights[name] = values.astype(dtype, copy=False)
    return weights


def save_weights(path: str | Path, weights: dict[str, np.ndarray]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, **weights)


class ShardWeights(dict):
    """Only the parameters the local segment executes, with a byte budget."""

    def __init__(self, values: dict[str, np.ndarray], layer_start: int, layer_end: int):
        super().__init__(values)
        self.layer_start = layer_start
        self.layer_end = layer_end

    @property
    def nbytes(self) -> int:
        return int(sum(value.nbytes for value in self.values()))

    def require(self, name: str) -> np.ndarray:
        try:
            return self[name]
        except KeyError as exc:
            raise ShardError(f"missing parameter {name} for segment "
                             f"[{self.layer_start}, {self.layer_end})") from exc


def load_shard(path: str | Path, config: ModelConfig, layer_start: int, layer_end: int,
               dtype: str | None = None) -> ShardWeights:
    names = segment_parameter_names(config, layer_start, layer_end)
    shapes = parameter_shapes(config)
    values: dict[str, np.ndarray] = {}
    try:
        archive = np.load(path)
    except OSError as exc:
        raise ShardError(f"cannot open checkpoint {path}: {exc}") from exc
    try:
        missing = [name for name in names if name not in archive.files]
        if missing:
            raise ShardError(f"checkpoint {path} is missing {len(missing)} parameter(s), "
                             f"first: {missing[0]}")
        for name in names:
            array = archive[name]
            if array.shape != shapes[name]:
                raise ShardError(f"parameter {name} has shape {array.shape}, expected {shapes[name]}")
            if dtype is not None and array.dtype.name != dtype:
                array = array.astype(dtype)
            values[name] = np.ascontiguousarray(array)
    finally:
        archive.close()
    return ShardWeights(values, layer_start, layer_end)
