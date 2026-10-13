"""从真实文件生成清单。

发布侧（谁导出模型谁负责）用本模块生成清单：sha256、容器大小、层/embedding/head 字节、
参数总字节都由引擎布局和真实文件算出来，不靠手写，因此不会出现"清单写 155392、
实际导出 200000"这类漂移。生成完立即用 :meth:`ModelManifest.from_payload` 自校验，
保证产出一定是可被 model-manager 接受的清单。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from inference_engine.config import ModelConfig

from .cache import sha256_file
from .layout import check_dtype, layout_for
from .manifest import (MANIFEST_VERSION, SUPPORTED_ARCHITECTURES, Artifact, Backend, License,
                       ModelManifest)
from .manifest import normalize_artifact_path

DEFAULT_ARCHITECTURE = SUPPORTED_ARCHITECTURES[0]
DEFAULT_DISK_MARGIN = 1.25


@dataclass(frozen=True)
class ArtifactInput:
    """一个待发布的本地文件：``file`` 是源文件，``path`` 是缓存内的相对路径。"""

    file: str | Path
    role: str
    path: str | None = None
    source_path: str | None = None
    layer_start: int | None = None
    layer_end: int | None = None
    media_type: str | None = None


def describe_artifact(item: ArtifactInput, *, dtype: str | None = None) -> Artifact:
    source = Path(item.file)
    if not source.is_file():
        raise FileNotFoundError(f"artifact file {source} does not exist")
    digest, size = sha256_file(source)
    payload: dict[str, Any] = {"path": normalize_artifact_path(item.path or source.name),
                               "role": item.role, "sha256": digest, "size_bytes": size,
                               "source_path": item.source_path,
                               "layer_start": item.layer_start, "layer_end": item.layer_end,
                               "media_type": item.media_type}
    if item.role == "checkpoint" and dtype is not None:
        payload["dtype"] = dtype
    return Artifact.from_payload(payload, f"artifacts[{item.role}:{payload['path']}]")


def build_manifest(*, model_id: str, revision: str, dtype: str, model: ModelConfig,
                   artifacts: Sequence[ArtifactInput], license: License | Mapping[str, Any],
                   backends: Mapping[str, Backend | Mapping[str, Any]],
                   architecture: str = DEFAULT_ARCHITECTURE, publisher: str | None = None,
                   notes: str | None = None, min_free_disk_bytes: int | None = None,
                   runtime_memory_bytes: int | None = None,
                   disk_margin: float = DEFAULT_DISK_MARGIN) -> ModelManifest:
    storage = check_dtype(dtype)
    if model.model_id != model_id:
        raise ValueError(f"model.model_id {model.model_id!r} does not match model_id {model_id!r}")
    layout = layout_for(model, storage)
    described = [describe_artifact(item, dtype=storage) for item in artifacts]
    container_bytes = sum(artifact.size_bytes for artifact in described)
    declared_license = license if isinstance(license, License) else License.from_payload(license)
    declared_backends = {name: (backend if isinstance(backend, Backend)
                                else Backend.from_payload(name, backend))
                         for name, backend in backends.items()}
    resources = {
        "parameter_bytes": layout.parameter_bytes,
        "container_bytes": container_bytes,
        "runtime_memory_bytes": runtime_memory_bytes or _runtime_estimate(model, layout),
        "min_free_disk_bytes": min_free_disk_bytes or max(container_bytes,
                                                          int(container_bytes * disk_margin)),
    }
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "model_id": model_id,
        "revision": revision,
        "architecture": architecture,
        "dtype": storage,
        "model": model.to_dict(),
        "layer_bytes": list(layout.per_layer_bytes),
        "module_bytes": layout.module_bytes,
        "license": declared_license.to_payload(),
        "resources": resources,
        "artifacts": [artifact.to_payload() for artifact in described],
        "backends": {name: backend.to_payload() for name, backend in declared_backends.items()},
        "publisher": publisher,
        "notes": notes,
    }
    return ModelManifest.from_payload(payload)


def _runtime_estimate(model: ModelConfig, layout) -> int:
    """参数字节 + 一份最大序列的 hidden state 与 logits，作为运行时内存声明。"""
    per_token = model.d_model + model.vocab_size
    activations = model.max_seq_len * per_token * layout.dtype_bytes
    return int(layout.parameter_bytes + activations)
