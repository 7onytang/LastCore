"""Numerical definition of the tiny transformer, shared by every code path.

The same functions run in the single-process reference and inside each shard
runtime, so a split pipeline performs the identical operation sequence as the
monolithic model and its output is bit-for-bit comparable.
"""

from __future__ import annotations

import math

import numpy as np

from .config import ModelConfig
from .errors import ShardError

GELU_C = 0.7978845608028654
GELU_COEF = 0.044715
MAX_BATCH_SIZE = 64


def check_batch(batch: int) -> None:
    """Reject oversized batches before embedding/attention allocations."""
    if batch > MAX_BATCH_SIZE:
        raise ShardError(f"batch size {batch} exceeds MAX_BATCH_SIZE {MAX_BATCH_SIZE}")


def layer_norm(x: np.ndarray, gain: np.ndarray, bias: np.ndarray, eps: float) -> np.ndarray:
    mean = x.mean(axis=-1, keepdims=True)
    variance = x.var(axis=-1, keepdims=True)
    return ((x - mean) / np.sqrt(variance + eps)) * gain + bias


def gelu(x: np.ndarray) -> np.ndarray:
    return 0.5 * x * (1.0 + np.tanh(GELU_C * (x + GELU_COEF * x ** 3)))


def embed(tokens: np.ndarray, weights, config: ModelConfig) -> np.ndarray:
    if tokens.ndim != 2:
        raise ShardError(f"token ids must be 2-D [batch, seq], got shape {tokens.shape}")
    check_batch(tokens.shape[0])
    if tokens.size and (tokens.min() < 0 or tokens.max() >= config.vocab_size):
        raise ShardError(f"token ids must be within [0, {config.vocab_size})")
    sequence = tokens.shape[1]
    if sequence > config.max_seq_len:
        raise ShardError(f"sequence length {sequence} exceeds max_seq_len {config.max_seq_len}")
    table = weights["embed.token"][tokens]
    positions = weights["embed.position"][:sequence]
    return (table + positions).astype(np.float32, copy=False)


def _split_heads(qkv: np.ndarray, config: ModelConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    batch, sequence, _ = qkv.shape
    reshaped = qkv.reshape(batch, sequence, 3, config.n_heads, config.head_size)
    query, key, value = np.moveaxis(reshaped, 2, 0)
    return (np.moveaxis(query, 2, 1), np.moveaxis(key, 2, 1), np.moveaxis(value, 2, 1))


def attention(x: np.ndarray, weights, layer: int, config: ModelConfig) -> np.ndarray:
    prefix = f"layer.{layer}.attn."
    query, key, value = _split_heads(x @ weights[f"{prefix}wqkv"] + weights[f"{prefix}bqkv"], config)
    scores = query @ np.swapaxes(key, -1, -2) / math.sqrt(config.head_size)
    sequence = x.shape[1]
    mask = np.triu(np.ones((sequence, sequence), dtype=bool), k=1)
    scores = np.where(mask, -np.inf, scores)
    probabilities = np.exp(scores - scores.max(axis=-1, keepdims=True))
    probabilities = probabilities / probabilities.sum(axis=-1, keepdims=True)
    context = probabilities @ value
    batch, _, sequence, _ = context.shape
    merged = context.transpose(0, 2, 1, 3).reshape(batch, sequence, config.d_model)
    return merged @ weights[f"{prefix}wo"] + weights[f"{prefix}bo"]


def block(x: np.ndarray, weights, layer: int, config: ModelConfig) -> np.ndarray:
    prefix = f"layer.{layer}."
    normalised = layer_norm(x, weights[f"{prefix}ln1.g"], weights[f"{prefix}ln1.b"], config.layer_norm_eps)
    x = x + attention(normalised, weights, layer, config)
    normalised = layer_norm(x, weights[f"{prefix}ln2.g"], weights[f"{prefix}ln2.b"], config.layer_norm_eps)
    hidden = gelu(normalised @ weights[f"{prefix}ffn.w1"] + weights[f"{prefix}ffn.b1"])
    return x + hidden @ weights[f"{prefix}ffn.w2"] + weights[f"{prefix}ffn.b2"]


def unembed(x: np.ndarray, weights, config: ModelConfig) -> np.ndarray:
    normalised = layer_norm(x, weights["head.ln.g"], weights["head.ln.b"], config.layer_norm_eps)
    return normalised @ weights["head.proj"].T


def run_layers(x: np.ndarray, weights, config: ModelConfig, layer_start: int, layer_end: int) -> np.ndarray:
    for layer in range(layer_start, layer_end):
        x = block(x, weights, layer, config)
    return x
