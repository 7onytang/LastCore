"""Single-process reference forward pass used to validate the distributed pipeline."""

from __future__ import annotations

import numpy as np

from .config import ModelConfig
from .model import embed, run_layers, unembed


def forward_full(tokens: np.ndarray, weights, config: ModelConfig) -> np.ndarray:
    hidden = embed(np.asarray(tokens, dtype=np.int64), weights, config)
    hidden = run_layers(hidden, weights, config, 0, config.n_layers)
    return unembed(hidden, weights, config)
