"""测试与示例共用的 tiny 资产构造。

所有 checkpoint 都由 inference-engine 的 ``generate_weights`` 确定性生成，不下载任何外部权重。
"""

from __future__ import annotations

import json
from pathlib import Path

from inference_engine.config import ModelConfig, tiny_config
from inference_engine.weights import generate_weights, save_weights

from model_manager.manifest import ModelManifest
from model_manager.publish import ArtifactInput, build_manifest

SEED = 5
MODEL_ID = "lastcore/tiny-cpu"
DEFAULT_BACKENDS = {
    "reference": {"dtypes": ["float32", "float16"], "max_seq_len": 16},
    "inference-engine.stage": {"dtypes": ["float32", "float16"], "max_seq_len": 16,
                               "runtime_memory_bytes": 1 << 20},
}
DEFAULT_LICENSE = {
    "name": "LastCore generated tiny weights (no third-party weights)",
    "redistributable": True,
    "notes": "由 generate_weights 确定性生成，不含第三方权重。",
}


def write_checkpoint(path: str | Path, *, seed: int = SEED, config: ModelConfig | None = None,
                     dtype: str = "float32") -> ModelConfig:
    config = config or tiny_config(MODEL_ID)
    save_weights(path, generate_weights(config, seed=seed, dtype=dtype))
    return config


def tokenizer_payload(vocab_size: int = 64) -> dict:
    return {"kind": "integer-token-ids", "vocab_size": vocab_size,
            "special_tokens": {"pad": 0, "eos": 1}}


def write_tokenizer(path: str | Path, *, vocab_size: int = 64) -> dict:
    payload = tokenizer_payload(vocab_size)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")
    return payload


def build_assets(directory: str | Path, *, seed: int = SEED, revision: str = "tiny-test",
                 model_id: str = MODEL_ID, dtype: str = "float32",
                 config: ModelConfig | None = None, include_tokenizer: bool = True,
                 backends=None, license=None, min_free_disk_bytes: int | None = None,
                 runtime_memory_bytes: int | None = None,
                 source_paths: bool = False) -> ModelManifest:
    """在 ``directory`` 下生成 tiny checkpoint（+ tokenizer）并返回配套清单。"""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    config = config or tiny_config(model_id)
    checkpoint = target / "tiny.npz"
    write_checkpoint(checkpoint, seed=seed, config=config, dtype=dtype)
    artifacts = [ArtifactInput(file=checkpoint, role="checkpoint", path="tiny.npz",
                              source_path="tiny.npz" if source_paths else None,
                              layer_start=0, layer_end=config.n_layers,
                              media_type="application/x-npz")]
    if include_tokenizer:
        tokenizer = target / "tokenizer.json"
        write_tokenizer(tokenizer, vocab_size=config.vocab_size)
        artifacts.append(ArtifactInput(file=tokenizer, role="tokenizer", path="tokenizer.json",
                                       source_path="tokenizer.json" if source_paths else None,
                                       media_type="application/json"))
    return build_manifest(model_id=model_id, revision=revision, dtype=dtype, model=config,
                          artifacts=artifacts, license=license or DEFAULT_LICENSE,
                          backends=backends or DEFAULT_BACKENDS,
                          min_free_disk_bytes=min_free_disk_bytes,
                          runtime_memory_bytes=runtime_memory_bytes,
                          publisher="tests", notes="LC-9 test fixture")


class FlakyStream:
    """读到 ``limit`` 字节后抛错，用来模拟写入过程被中断。"""

    def __init__(self, data: bytes, limit: int):
        self._data = data
        self._limit = limit
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if self._position >= self._limit:
            raise OSError("simulated interrupted read")
        end = min(self._limit, self._position + (size if size > 0 else self._limit))
        chunk = self._data[self._position:end]
        self._position = end
        return chunk

    def close(self) -> None:
        return None


class TamperedStream:
    """大小正确但内容被改写，用来验证哈希校验真的生效。"""

    def __init__(self, data: bytes):
        self._data = bytearray(data)
        if self._data:
            self._data[0] ^= 0xFF
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if self._position >= len(self._data):
            return b""
        end = min(len(self._data), self._position + (size if size > 0 else len(self._data)))
        chunk = bytes(self._data[self._position:end])
        self._position = end
        return chunk

    def close(self) -> None:
        return None


class OversizedStream:
    """比声明的 size_bytes 更长，用来验证超额写入被拒绝。"""

    def __init__(self, data: bytes):
        self._data = data
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if self._position >= len(self._data):
            return b""
        end = min(len(self._data), self._position + (size if size > 0 else len(self._data)))
        chunk = self._data[self._position:end]
        self._position = end
        return chunk

    def close(self) -> None:
        return None
