"""成本清单：层 / embedding / head / dtype / 哈希。

清单只声明数字，本模块负责把数字摊开成可核对的成本表，并按层区间给出容量估算，
供调度侧（LC-10 的模型容量估算）和节点侧（落盘与内存预算）直接使用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .layout import Layout
from .manifest import ModelManifest

SHA256_HEX_CHARS = 64
DEFAULT_HASH_BUFFER = 1 << 20


@dataclass(frozen=True)
class CostReport:
    """一份 revision 的完整字节与哈希成本。"""

    model_id: str
    revision: str
    architecture: str
    dtype: str
    dtype_bytes: int
    n_layers: int
    n_tensors: int
    embedding_bytes: int
    head_bytes: int
    per_layer_bytes: tuple[int, ...]
    layers_bytes: int
    parameter_bytes: int
    container_bytes: int
    runtime_memory_bytes: int
    min_free_disk_bytes: int
    sha256_hex_chars: int = SHA256_HEX_CHARS

    @property
    def container_overhead_bytes(self) -> int:
        return self.container_bytes - self.parameter_bytes

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id, "revision": self.revision,
            "architecture": self.architecture, "dtype": self.dtype,
            "dtype_bytes": self.dtype_bytes, "n_layers": self.n_layers,
            "n_tensors": self.n_tensors, "embedding_bytes": self.embedding_bytes,
            "head_bytes": self.head_bytes, "per_layer_bytes": list(self.per_layer_bytes),
            "layers_bytes": self.layers_bytes, "parameter_bytes": self.parameter_bytes,
            "container_bytes": self.container_bytes,
            "container_overhead_bytes": self.container_overhead_bytes,
            "runtime_memory_bytes": self.runtime_memory_bytes,
            "min_free_disk_bytes": self.min_free_disk_bytes,
            "sha256_hex_chars": self.sha256_hex_chars,
        }


@dataclass(frozen=True)
class SegmentCost:
    """一个层区间的容量估算。"""

    layer_start: int
    layer_end: int
    parameter_bytes: int
    parameter_names: int
    includes_embedding: bool
    includes_head: bool
    activation_bytes: int
    runtime_memory_bytes: int

    def as_dict(self) -> dict:
        return {"layer_start": self.layer_start, "layer_end": self.layer_end,
                "parameter_bytes": self.parameter_bytes, "parameter_names": self.parameter_names,
                "includes_embedding": self.includes_embedding, "includes_head": self.includes_head,
                "activation_bytes": self.activation_bytes,
                "runtime_memory_bytes": self.runtime_memory_bytes}


def cost_report(manifest: ModelManifest) -> CostReport:
    layout: Layout = manifest.layout
    return CostReport(model_id=manifest.model_id, revision=manifest.revision,
                      architecture=manifest.architecture, dtype=manifest.dtype,
                      dtype_bytes=layout.dtype_bytes, n_layers=layout.n_layers,
                      n_tensors=layout.n_tensors, embedding_bytes=layout.embedding_bytes,
                      head_bytes=layout.head_bytes, per_layer_bytes=layout.per_layer_bytes,
                      layers_bytes=layout.layers_bytes, parameter_bytes=layout.parameter_bytes,
                      container_bytes=manifest.resources.container_bytes,
                      runtime_memory_bytes=manifest.resources.runtime_memory_bytes,
                      min_free_disk_bytes=manifest.resources.min_free_disk_bytes)


def activation_bytes(manifest: ModelManifest, layer_start: int, layer_end: int, *,
                     batch: int = 1, seq_len: int | None = None) -> int:
    """粗略的中间激活占用：hidden state 一份，末段再加一份 logits。"""
    sequence = seq_len or manifest.model.max_seq_len
    width = batch * sequence * manifest.model.d_model * manifest.layout.dtype_bytes
    if layer_end == manifest.n_layers:
        width += batch * sequence * manifest.model.vocab_size * manifest.layout.dtype_bytes
    return width


def segment_cost(manifest: ModelManifest, layer_start: int, layer_end: int, *,
                 batch: int = 1, seq_len: int | None = None) -> SegmentCost:
    if not 0 <= layer_start < layer_end <= manifest.n_layers:
        raise ValueError(f"segment [{layer_start}, {layer_end}) is outside 0..{manifest.n_layers}")
    layout = manifest.layout
    names = layout.segment_names(layer_start, layer_end)
    parameters = layout.segment_bytes(layer_start, layer_end)
    activations = activation_bytes(manifest, layer_start, layer_end, batch=batch, seq_len=seq_len)
    return SegmentCost(layer_start=layer_start, layer_end=layer_end, parameter_bytes=parameters,
                       parameter_names=len(names), includes_embedding=layer_start == 0,
                       includes_head=layer_end == manifest.n_layers,
                       activation_bytes=activations,
                       runtime_memory_bytes=parameters + activations)


def disk_requirement(manifest: ModelManifest) -> int:
    """落盘前需要的最小可用空间（清单声明与容器大小取较大者）。"""
    return max(manifest.resources.min_free_disk_bytes, manifest.resources.container_bytes)


def sha256_file(path: str | Path, *, buffer: int = DEFAULT_HASH_BUFFER) -> tuple[str, int]:
    """流式计算文件的 sha256 与字节数，返回 ``(hexdigest, size_bytes)``。"""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(buffer)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def measure_hash_throughput(path: str | Path, *, repeats: int = 3) -> float:
    """实测 sha256 吞吐（字节/秒），用于把哈希成本换算成时间。"""
    import time

    best = 0.0
    size = Path(path).stat().st_size
    for _ in range(max(1, repeats)):
        started = time.perf_counter()
        sha256_file(path)
        elapsed = time.perf_counter() - started
        if elapsed > 0:
            best = max(best, size / elapsed)
    return best
