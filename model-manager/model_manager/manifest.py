"""模型 revision 清单：严格解析、白名单校验与声明值交叉核对。

清单是"已获批的小模型 revision"的输入。它只描述数据——相对路径、sha256、形状、
字节数、层边界、后端兼容范围与权重许可——不携带任何可执行内容。解析器 fail closed：
任何未声明的键都会让清单被拒绝（例如 ``entrypoint`` / ``python`` / ``command`` /
``script`` 这类字段必然不在允许集合内），因此远端清单无法借 model-manager 执行自定义代码；
产物源只按 artifact 的相对路径取字节，取回的字节只做哈希与落盘，从不被解释或执行。

校验顺序：schema → 架构白名单 → dtype → 模型形状（复用 ``inference_engine.config.ModelConfig``）
→ 许可 → 资源 → 产物 → 后端 → 与引擎实际参数布局交叉核对。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

from inference_engine.config import ModelConfig
from inference_engine.errors import ConfigError

from .errors import (BackendCapabilityError, ManifestError, PathTraversalError,
                     UnsupportedArchitectureError)
from .layout import (MODULE_KEYS, Layout, check_dtype, layout_for)

MANIFEST_VERSION = "1"
SUPPORTED_ARCHITECTURES = ("lastcore.tiny-decoder",)
ARTIFACT_ROLES = ("tokenizer", "config", "checkpoint")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_MANIFEST_FIELDS = ("manifest_version", "model_id", "revision", "architecture", "dtype", "model",
                    "layer_bytes", "module_bytes", "license", "resources", "artifacts", "backends",
                    "publisher", "notes")
_MANIFEST_REQUIRED = ("manifest_version", "model_id", "revision", "architecture", "dtype", "model",
                      "layer_bytes", "module_bytes", "license", "resources", "artifacts", "backends")
_LICENSE_FIELDS = ("name", "identifier", "source_url", "redistributable", "notes")
_LICENSE_REQUIRED = ("name", "redistributable")
_RESOURCE_FIELDS = ("parameter_bytes", "container_bytes", "runtime_memory_bytes",
                    "min_free_disk_bytes")
_ARTIFACT_FIELDS = ("path", "role", "sha256", "size_bytes", "dtype", "layer_start", "layer_end",
                    "source_path", "media_type")
_ARTIFACT_REQUIRED = ("path", "role", "sha256", "size_bytes")
_BACKEND_FIELDS = ("dtypes", "max_seq_len", "runtime_memory_bytes", "notes")
_BACKEND_REQUIRED = ("dtypes",)


def _fail(message: str) -> None:
    raise ManifestError(message)


def _mapping(payload: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        _fail(f"{where} must be a JSON object, got {type(payload).__name__}")
    return payload


def _check_keys(payload: Any, allowed: tuple[str, ...], required: tuple[str, ...],
                where: str) -> Mapping[str, Any]:
    obj = _mapping(payload, where)
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        _fail(f"{where} declares unsupported field(s) {unknown}; "
              f"allowed fields: {list(allowed)}")
    missing = sorted(set(required) - set(obj))
    if missing:
        _fail(f"{where} is missing required field(s) {missing}")
    return obj


def _text(payload: Mapping[str, Any], key: str, where: str, *, minimum: int = 1) -> str:
    value = payload[key]
    if not isinstance(value, str) or len(value) < minimum:
        _fail(f"{where}.{key} must be a string of at least {minimum} character(s)")
    return value


def _integer(payload: Mapping[str, Any], key: str, where: str, *, minimum: int = 0) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _boolean(payload: Mapping[str, Any], key: str, where: str) -> bool:
    value = payload[key]
    if not isinstance(value, bool):
        _fail(f"{where}.{key} must be a boolean, got {value!r}")
    return value


def _optional_text(payload: Mapping[str, Any], key: str, where: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where}.{key} must be a non-empty string when present")
    return value


def normalize_artifact_path(value: Any, where: str = "artifact") -> str:
    """把清单里的相对路径规范化为 posix 相对路径，拒绝一切越界写法。"""
    if not isinstance(value, str) or not value:
        raise PathTraversalError(value, "artifact path must be a non-empty string")
    if "\x00" in value:
        raise PathTraversalError(value, "artifact path contains a NUL byte")
    candidate = value.replace("\\", "/")
    if candidate.startswith("/"):
        raise PathTraversalError(value, "absolute paths are not allowed")
    if _DRIVE_RE.match(candidate):
        raise PathTraversalError(value, "drive-letter paths are not allowed")
    parts = PurePosixPath(candidate).parts
    if not parts:
        raise PathTraversalError(value, "artifact path is empty after normalisation")
    for part in parts:
        if part in ("..", "."):
            raise PathTraversalError(value, f"path segment {part!r} is not allowed")
    if any(part == "" for part in parts):
        raise PathTraversalError(value, "artifact path contains an empty segment")
    normalized = "/".join(parts)
    if normalized != candidate:
        raise PathTraversalError(
            value, f"artifact path {where!r} is not a clean relative path; "
                   f"remove redundant './' or '//' segments")
    return normalized


@dataclass(frozen=True)
class License:
    """模型权重的许可声明。本模块只记录与展示，不代替人类选择仓库许可证。"""

    name: str
    redistributable: bool
    identifier: str | None = None
    source_url: str | None = None
    notes: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "License":
        obj = _check_keys(payload, _LICENSE_FIELDS, _LICENSE_REQUIRED, "license")
        return cls(name=_text(obj, "name", "license"),
                   redistributable=_boolean(obj, "redistributable", "license"),
                   identifier=_optional_text(obj, "identifier", "license"),
                   source_url=_optional_text(obj, "source_url", "license"),
                   notes=_optional_text(obj, "notes", "license"))

    def to_payload(self) -> dict:
        return {"name": self.name, "identifier": self.identifier, "source_url": self.source_url,
                "redistributable": self.redistributable, "notes": self.notes}


@dataclass(frozen=True)
class Resources:
    """资源需求声明；所有数值都会与实际参数布局交叉核对。"""

    parameter_bytes: int
    container_bytes: int
    runtime_memory_bytes: int
    min_free_disk_bytes: int

    @classmethod
    def from_payload(cls, payload: Any) -> "Resources":
        obj = _check_keys(payload, _RESOURCE_FIELDS, _RESOURCE_FIELDS, "resources")
        return cls(parameter_bytes=_integer(obj, "parameter_bytes", "resources", minimum=1),
                   container_bytes=_integer(obj, "container_bytes", "resources", minimum=1),
                   runtime_memory_bytes=_integer(obj, "runtime_memory_bytes", "resources",
                                                 minimum=1),
                   min_free_disk_bytes=_integer(obj, "min_free_disk_bytes", "resources",
                                                minimum=1))

    def to_payload(self) -> dict:
        return {"parameter_bytes": self.parameter_bytes, "container_bytes": self.container_bytes,
                "runtime_memory_bytes": self.runtime_memory_bytes,
                "min_free_disk_bytes": self.min_free_disk_bytes}


@dataclass(frozen=True)
class Artifact:
    """一个待校验落盘的本地文件。``path`` 是缓存内的相对路径，``source_path`` 是源侧相对路径。"""

    path: str
    role: str
    sha256: str
    size_bytes: int
    dtype: str | None = None
    layer_start: int | None = None
    layer_end: int | None = None
    source_path: str | None = None
    media_type: str | None = None

    @classmethod
    def from_payload(cls, payload: Any, where: str) -> "Artifact":
        obj = _check_keys(payload, _ARTIFACT_FIELDS, _ARTIFACT_REQUIRED, where)
        role = _text(obj, "role", where)
        if role not in ARTIFACT_ROLES:
            _fail(f"{where}.role must be one of {list(ARTIFACT_ROLES)}, got {role!r}")
        digest = _text(obj, "sha256", where)
        if not _SHA256_RE.match(digest):
            _fail(f"{where}.sha256 must be 64 lowercase hex characters")
        dtype = obj.get("dtype")
        if dtype is not None:
            dtype = check_dtype(dtype)
        start, end = obj.get("layer_start"), obj.get("layer_end")
        if (start is None) != (end is None):
            _fail(f"{where}.layer_start and layer_end must be provided together")
        if start is not None:
            for name, value in (("layer_start", start), ("layer_end", end)):
                if isinstance(value, bool) or not isinstance(value, int):
                    _fail(f"{where}.{name} must be an integer")
            if start < 0 or end <= start:
                _fail(f"{where} declares layer segment [{start}, {end}) which is empty or negative")
        return cls(path=normalize_artifact_path(obj["path"]),
                   role=role, sha256=digest,
                   size_bytes=_integer(obj, "size_bytes", where),
                   dtype=dtype, layer_start=start, layer_end=end,
                   source_path=(normalize_artifact_path(obj["source_path"], where)
                                if obj.get("source_path") is not None else None),
                   media_type=_optional_text(obj, "media_type", where))

    def covers(self) -> tuple[int, int] | None:
        return None if self.layer_start is None else (self.layer_start, self.layer_end)

    def to_payload(self) -> dict:
        return {"path": self.path, "role": self.role, "sha256": self.sha256,
                "size_bytes": self.size_bytes, "dtype": self.dtype,
                "layer_start": self.layer_start, "layer_end": self.layer_end,
                "source_path": self.source_path, "media_type": self.media_type}


@dataclass(frozen=True)
class Backend:
    """一个后端的能力声明。dtype 与序列长度都必须落在声明范围内才允许启动。"""

    name: str
    dtypes: tuple[str, ...]
    max_seq_len: int | None = None
    runtime_memory_bytes: int | None = None
    notes: str | None = None

    @classmethod
    def from_payload(cls, name: Any, payload: Any) -> "Backend":
        where = f"backends.{name}"
        if not isinstance(name, str) or not name.strip():
            _fail("backends keys must be non-empty backend names")
        obj = _check_keys(payload, _BACKEND_FIELDS, _BACKEND_REQUIRED, where)
        dtypes = obj["dtypes"]
        if not isinstance(dtypes, list) or not dtypes:
            _fail(f"{where}.dtypes must be a non-empty list")
        for dtype in dtypes:
            check_dtype(dtype)
        if len(set(dtypes)) != len(dtypes):
            _fail(f"{where}.dtypes contains duplicates")
        max_seq_len = obj.get("max_seq_len")
        if max_seq_len is not None and (isinstance(max_seq_len, bool)
                                        or not isinstance(max_seq_len, int) or max_seq_len <= 0):
            _fail(f"{where}.max_seq_len must be a positive integer when present")
        runtime = obj.get("runtime_memory_bytes")
        if runtime is not None and (isinstance(runtime, bool) or not isinstance(runtime, int)
                                    or runtime <= 0):
            _fail(f"{where}.runtime_memory_bytes must be a positive integer when present")
        return cls(name=name, dtypes=tuple(dtypes), max_seq_len=max_seq_len,
                   runtime_memory_bytes=runtime,
                   notes=_optional_text(obj, "notes", where))

    def to_payload(self) -> dict:
        return {"dtypes": list(self.dtypes), "max_seq_len": self.max_seq_len,
                "runtime_memory_bytes": self.runtime_memory_bytes, "notes": self.notes}


@dataclass(frozen=True)
class ModelManifest:
    manifest_version: str
    model_id: str
    revision: str
    architecture: str
    dtype: str
    model: ModelConfig
    layer_bytes: tuple[int, ...]
    module_bytes: Mapping[str, int]
    license: License
    resources: Resources
    artifacts: tuple[Artifact, ...]
    backends: Mapping[str, Backend] = field(default_factory=dict)
    publisher: str | None = None
    notes: str | None = None

    # ---- 解析 ----------------------------------------------------------------

    @classmethod
    def from_payload(cls, payload: Any) -> "ModelManifest":
        obj = _check_keys(payload, _MANIFEST_FIELDS, _MANIFEST_REQUIRED, "manifest")
        version = _text(obj, "manifest_version", "manifest")
        if version != MANIFEST_VERSION:
            _fail(f"manifest_version {version!r} is not supported, expected {MANIFEST_VERSION!r}")
        architecture = _text(obj, "architecture", "manifest")
        if architecture not in SUPPORTED_ARCHITECTURES:
            raise UnsupportedArchitectureError(architecture, SUPPORTED_ARCHITECTURES)
        dtype = check_dtype(obj["dtype"])
        model_id = _text(obj, "model_id", "manifest")
        revision = _text(obj, "revision", "manifest")

        model_payload = _mapping(obj["model"], "model")
        try:
            model = ModelConfig.from_dict(dict(model_payload))
        except ConfigError as exc:
            raise ManifestError(f"model is not a valid engine ModelConfig: {exc}") from exc
        if model.model_id != model_id:
            _fail(f"model.model_id {model.model_id!r} does not match manifest model_id "
                  f"{model_id!r}")

        layout = layout_for(model, dtype)
        layer_bytes = _integer_list(obj["layer_bytes"], "layer_bytes")
        module_bytes = _module_bytes(obj["module_bytes"])
        license_ = License.from_payload(obj["license"])
        resources = Resources.from_payload(obj["resources"])
        artifacts_payload = obj["artifacts"]
        if not isinstance(artifacts_payload, list) or not artifacts_payload:
            _fail("artifacts must be a non-empty list")
        artifacts = tuple(Artifact.from_payload(item, f"artifacts[{index}]")
                          for index, item in enumerate(artifacts_payload))
        backends_payload = _mapping(obj["backends"], "backends")
        if not backends_payload:
            _fail("backends must declare at least one compatible backend")
        backends = {name: Backend.from_payload(name, value)
                    for name, value in backends_payload.items()}

        manifest = cls(manifest_version=version, model_id=model_id, revision=revision,
                       architecture=architecture, dtype=dtype, model=model,
                       layer_bytes=layer_bytes, module_bytes=module_bytes, license=license_,
                       resources=resources, artifacts=artifacts, backends=backends,
                       publisher=_optional_text(obj, "publisher", "manifest"),
                       notes=_optional_text(obj, "notes", "manifest"))
        manifest._cross_check(layout)
        return manifest

    @classmethod
    def load(cls, path: Any) -> "ModelManifest":
        from pathlib import Path

        text = Path(path).read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
        return cls.from_payload(payload)

    # ---- 校验 ----------------------------------------------------------------

    def _cross_check(self, layout: Layout) -> None:
        if len(self.layer_bytes) != layout.n_layers:
            _fail(f"layer_bytes has {len(self.layer_bytes)} entries but the model has "
                  f"{layout.n_layers} layers")
        if self.layer_bytes != layout.per_layer_bytes:
            _fail(f"layer_bytes {list(self.layer_bytes)} does not match the engine layout "
                  f"{list(layout.per_layer_bytes)} for dtype {self.dtype}")
        for key in MODULE_KEYS:
            if key not in self.module_bytes:
                _fail(f"module_bytes is missing {key!r}")
            if self.module_bytes[key] != layout.module_bytes[key]:
                _fail(f"module_bytes.{key} claims {self.module_bytes[key]} bytes but the engine "
                      f"layout has {layout.module_bytes[key]}")
        if self.resources.parameter_bytes != layout.parameter_bytes:
            _fail(f"resources.parameter_bytes claims {self.resources.parameter_bytes} but the "
                  f"engine layout has {layout.parameter_bytes} for dtype {self.dtype}")
        declared_container = sum(artifact.size_bytes for artifact in self.artifacts)
        if self.resources.container_bytes != declared_container:
            _fail(f"resources.container_bytes claims {self.resources.container_bytes} but the "
                  f"artifacts sum to {declared_container}")
        if self.resources.min_free_disk_bytes < self.resources.container_bytes:
            _fail("resources.min_free_disk_bytes must be at least container_bytes")
        if self.resources.runtime_memory_bytes < self.resources.parameter_bytes:
            _fail("resources.runtime_memory_bytes must be at least parameter_bytes")
        paths = [artifact.path for artifact in self.artifacts]
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            _fail(f"artifacts declare duplicate path(s) {duplicates}")
        self._check_layer_coverage()
        for artifact in self.artifacts:
            if artifact.role == "checkpoint" and artifact.dtype is not None \
                    and artifact.dtype != self.dtype:
                _fail(f"checkpoint artifact {artifact.path!r} declares dtype {artifact.dtype!r} "
                      f"but the manifest dtype is {self.dtype!r}")
        if not any(self.dtype in backend.dtypes for backend in self.backends.values()):
            _fail(f"no declared backend accepts the storage dtype {self.dtype!r}")

    def _check_layer_coverage(self) -> None:
        checkpoints = [artifact for artifact in self.artifacts if artifact.role == "checkpoint"]
        if not checkpoints:
            _fail("artifacts must contain at least one checkpoint artifact")
        ranges = [artifact.covers() or (0, self.model.n_layers) for artifact in checkpoints]
        for start, end in ranges:
            if end > self.model.n_layers:
                _fail(f"checkpoint artifact covers layers [{start}, {end}) but the model has "
                      f"{self.model.n_layers} layers")
        ranges.sort()
        cursor = 0
        for start, end in ranges:
            if start != cursor:
                _fail(f"checkpoint layer segments must tile [0, {self.model.n_layers}) exactly; "
                      f"got a gap or overlap at layer {cursor} (segment starts at {start})")
            cursor = end
        if cursor != self.model.n_layers:
            _fail(f"checkpoint layer segments cover [0, {cursor}) but the model has "
                  f"{self.model.n_layers} layers")

    # ---- 查询 ----------------------------------------------------------------

    @property
    def n_layers(self) -> int:
        return self.model.n_layers

    @property
    def layout(self) -> Layout:
        return layout_for(self.model, self.dtype)

    @property
    def checkpoints(self) -> tuple[Artifact, ...]:
        return tuple(artifact for artifact in self.artifacts if artifact.role == "checkpoint")

    def artifacts_by_role(self, role: str) -> tuple[Artifact, ...]:
        return tuple(artifact for artifact in self.artifacts if artifact.role == role)

    def backend(self, name: str) -> Backend:
        try:
            return self.backends[name]
        except KeyError as exc:
            raise BackendCapabilityError(
                f"backend {name!r} is not declared compatible by this revision; declared: "
                f"{sorted(self.backends)}", backend=name,
                available=sorted(self.backends)) from exc

    def to_payload(self) -> dict:
        return {"manifest_version": self.manifest_version, "model_id": self.model_id,
                "revision": self.revision, "architecture": self.architecture, "dtype": self.dtype,
                "model": self.model.to_dict(), "layer_bytes": list(self.layer_bytes),
                "module_bytes": dict(self.module_bytes), "license": self.license.to_payload(),
                "resources": self.resources.to_payload(),
                "artifacts": [artifact.to_payload() for artifact in self.artifacts],
                "backends": {name: backend.to_payload() for name, backend in self.backends.items()},
                "publisher": self.publisher, "notes": self.notes}

    def save(self, path: Any) -> None:
        from pathlib import Path

        Path(path).write_text(json.dumps(self.to_payload(), indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")


def check_backend_capability(manifest: ModelManifest, backend: str | None = None,
                            dtype: str | None = None) -> None:
    """启动前的能力检查：后端、dtype、序列长度与运行时内存必须都在声明范围内。"""
    requested_dtype = check_dtype(dtype) if dtype is not None else manifest.dtype
    if backend is None:
        return
    declared = manifest.backend(backend)
    if requested_dtype not in declared.dtypes:
        raise BackendCapabilityError(
            f"backend {backend!r} does not support dtype {requested_dtype!r}; it declares "
            f"{list(declared.dtypes)}", backend=backend, dtype=requested_dtype,
            available=list(declared.dtypes))
    if declared.max_seq_len is not None and manifest.model.max_seq_len > declared.max_seq_len:
        raise BackendCapabilityError(
            f"backend {backend!r} supports max_seq_len {declared.max_seq_len} but the model "
            f"declares {manifest.model.max_seq_len}", backend=backend)
    if declared.runtime_memory_bytes is not None \
            and manifest.resources.runtime_memory_bytes > declared.runtime_memory_bytes:
        raise BackendCapabilityError(
            f"backend {backend!r} allows {declared.runtime_memory_bytes} runtime bytes but the "
            f"model declares {manifest.resources.runtime_memory_bytes}", backend=backend)


def _integer_list(payload: Any, where: str) -> tuple[int, ...]:
    if not isinstance(payload, list) or not payload:
        _fail(f"{where} must be a non-empty list")
    for index, value in enumerate(payload):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _fail(f"{where}[{index}] must be a positive integer, got {value!r}")
    return tuple(payload)


def _module_bytes(payload: Any) -> Mapping[str, int]:
    obj = _check_keys(payload, MODULE_KEYS, MODULE_KEYS, "module_bytes")
    return {key: _integer(obj, key, "module_bytes", minimum=1) for key in MODULE_KEYS}
