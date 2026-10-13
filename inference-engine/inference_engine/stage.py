"""Execution contract for one layer segment of the distributed pipeline.

A :class:`StageRuntime` owns exactly one ``[layer_start, layer_end)`` segment and the
parameters for that segment alone. It is a pure function of its input:

* the stage that owns layer 0 accepts integer token ids ``[batch, seq]`` and applies
  the token/position embedding first;
* every other stage accepts the upstream hidden state ``[batch, seq, d_model]``;
* the stage that owns the last layer applies the final layer norm and the unembedding
  projection, so its output is ``[batch, seq, vocab_size]`` logits;
* a middle stage returns the hidden state for its downstream peer.

Because the math is identical in every path, the pipeline output must equal the
single-process reference bit for bit for the same checkpoint.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import ModelConfig
from .errors import ShardError
from .model import check_batch, embed, run_layers, unembed
from .weights import ShardWeights, load_shard, segment_parameter_names


def segment_plan(num_layers: int, num_stages: int) -> list[tuple[int, int]]:
    """Split ``num_layers`` blocks into contiguous, non-overlapping segments."""
    if num_layers <= 0 or num_stages <= 0:
        raise ShardError("num_layers and num_stages must be positive")
    if num_stages > num_layers:
        raise ShardError(f"cannot split {num_layers} layers across {num_stages} stages")
    base, extra = divmod(num_layers, num_stages)
    plan, start = [], 0
    for index in range(num_stages):
        end = start + base + (1 if index < extra else 0)
        plan.append((start, end))
        start = end
    return plan


class StageRuntime:
    def __init__(self, config: ModelConfig, layer_start: int, layer_end: int,
                 weights: ShardWeights, *, task_id: str | None = None,
                 assignment_id: str | None = None):
        if not 0 <= layer_start < layer_end <= config.n_layers:
            raise ShardError(f"segment [{layer_start}, {layer_end}) is outside 0..{config.n_layers}")
        expected = set(segment_parameter_names(config, layer_start, layer_end))
        provided = set(weights)
        if provided != expected:
            missing, extra = sorted(expected - provided), sorted(provided - expected)
            raise ShardError(f"segment [{layer_start}, {layer_end}) weights mismatch; "
                             f"missing={missing[:3]} unexpected={extra[:3]}")
        self.config = config
        self.layer_start = layer_start
        self.layer_end = layer_end
        self.weights = weights
        self.task_id = task_id
        self.assignment_id = assignment_id

    @property
    def is_first(self) -> bool:
        return self.layer_start == 0

    @property
    def is_last(self) -> bool:
        return self.layer_end == self.config.n_layers

    @property
    def parameter_bytes(self) -> int:
        return self.weights.nbytes

    def describe(self) -> dict:
        return {
            "model_id": self.config.model_id,
            "layer_start": self.layer_start,
            "layer_end": self.layer_end,
            "num_layers": self.config.n_layers,
            "role": self.role,
            "d_model": self.config.d_model,
            "parameter_bytes": self.parameter_bytes,
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
        }

    @property
    def role(self) -> str:
        if self.is_first and self.is_last:
            return "single"
        return "first" if self.is_first else "last" if self.is_last else "middle"

    def execute(self, batch: np.ndarray) -> np.ndarray:
        if self.is_first:
            if batch.dtype.kind not in "iu":
                raise ShardError(f"the first stage expects integer token ids, got {batch.dtype}")
            hidden = embed(np.asarray(batch, dtype=np.int64), self.weights, self.config)
        else:
            hidden = np.asarray(batch, dtype=np.float32)
            if hidden.ndim != 3 or hidden.shape[-1] != self.config.d_model:
                raise ShardError(f"hidden state must be [batch, seq, {self.config.d_model}], "
                                 f"got shape {hidden.shape}")
            check_batch(hidden.shape[0])
            if hidden.shape[1] > self.config.max_seq_len:
                raise ShardError(f"sequence length {hidden.shape[1]} exceeds max_seq_len")
        hidden = run_layers(hidden, self.weights, self.config, self.layer_start, self.layer_end)
        return unembed(hidden, self.weights, self.config) if self.is_last else hidden


def load_stage(config: ModelConfig, weights_path: str | Path, layer_start: int, layer_end: int,
               *, task_id: str | None = None, assignment_id: str | None = None) -> StageRuntime:
    shard = load_shard(weights_path, config, layer_start, layer_end)
    return StageRuntime(config, layer_start, layer_end, shard,
                        task_id=task_id, assignment_id=assignment_id)
