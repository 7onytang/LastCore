from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import ConfigError


@dataclass(frozen=True)
class ModelConfig:
    """Shape of the CPU-verifiable tiny transformer used for local validation.

    ``n_layers`` counts transformer blocks only. Embedding runs on the stage whose
    segment starts at layer 0; the final norm and the unembedding projection run on
    the stage whose segment ends at ``n_layers``.
    """

    model_id: str
    vocab_size: int
    d_model: int
    n_heads: int
    n_layers: int
    d_ff: int
    max_seq_len: int
    layer_norm_eps: float = 1e-5

    def __post_init__(self):
        if not self.model_id:
            raise ConfigError("model_id must not be empty")
        for name, value in (("vocab_size", self.vocab_size), ("d_model", self.d_model),
                            ("n_heads", self.n_heads), ("n_layers", self.n_layers),
                            ("d_ff", self.d_ff), ("max_seq_len", self.max_seq_len)):
            if not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if self.d_model % self.n_heads:
            raise ConfigError(f"d_model {self.d_model} must be divisible by n_heads {self.n_heads}")
        if not 0 < self.layer_norm_eps < 1:
            raise ConfigError("layer_norm_eps must be between 0 and 1")

    @property
    def head_size(self) -> int:
        return self.d_model // self.n_heads

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "ModelConfig":
        try:
            return cls(**payload)
        except TypeError as exc:
            raise ConfigError(f"invalid model config: {exc}") from exc

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ModelConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def tiny_config(model_id: str = "lastcore/tiny-cpu", *, n_layers: int = 4, d_model: int = 32,
                n_heads: int = 4, d_ff: int = 64, vocab_size: int = 64,
                max_seq_len: int = 16) -> ModelConfig:
    return ModelConfig(model_id=model_id, vocab_size=vocab_size, d_model=d_model, n_heads=n_heads,
                       n_layers=n_layers, d_ff=d_ff, max_seq_len=max_seq_len)
