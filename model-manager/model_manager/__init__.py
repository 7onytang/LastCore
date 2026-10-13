"""model-manager：模型 revision、层边界、后端能力与缓存校验。

面向使用者的入口：

* :func:`load_manifest` / :class:`ModelManifest` —— 严格解析并校验清单；
* :func:`model_summary` / :func:`model_cost` —— 层 / embedding / head / dtype / 哈希成本；
* :class:`ModelCache` + :func:`materialize_model` —— 内容寻址、原子落盘、坏文件与并发校验；
* :func:`load_shard_for` / :func:`load_stage_for` —— 复用 inference-engine 的 loader 只读本段权重。
"""

from __future__ import annotations

from .cache import ModelCache, VerifiedArtifact, VerifiedModel, resolve_within, safe_component
from .cost import CostReport, SegmentCost, cost_report, disk_requirement, segment_cost
from .engine_adapter import (checkpoint_artifact, expected_arrays, load_validated_shard,
                            load_validated_stage, model_config, verify_checkpoint_contents)
from .errors import (BackendCapabilityError, EngineAdapterError, IntegrityError,
                     InsufficientDiskError, LockTimeoutError, ManifestError,
                     ModelManagerError, PathTraversalError, SourceError,
                     UnsupportedArchitectureError)
from .layout import Layout, layout_for
from .manifest import (Artifact, Backend, License, ModelManifest, Resources,
                       check_backend_capability)
from .materialize import (MaterializeResult, materialize, segment_costs, summary,
                         verify_materialized)
from .publish import ArtifactInput, build_manifest, describe_artifact
from .sources import ArtifactSource, HttpArtifactSource, LocalArtifactSource, fetch_bytes

load_manifest = ModelManifest.load
model_summary = summary
model_cost = cost_report
materialize_model = materialize
load_shard_for = load_validated_shard
load_stage_for = load_validated_stage

__all__ = [
    "ModelManifest", "load_manifest", "Artifact", "ArtifactInput", "Backend", "License",
    "Resources", "Layout", "layout_for", "check_backend_capability",
    "build_manifest", "describe_artifact",
    "ModelCache", "VerifiedModel", "VerifiedArtifact", "resolve_within", "safe_component",
    "materialize_model", "MaterializeResult", "verify_materialized", "model_summary",
    "model_cost", "cost_report", "CostReport", "SegmentCost", "segment_cost", "segment_costs",
    "disk_requirement",
    "load_shard_for", "load_stage_for", "model_config", "expected_arrays",
    "checkpoint_artifact", "verify_checkpoint_contents",
    "ArtifactSource", "LocalArtifactSource", "HttpArtifactSource", "fetch_bytes",
    "ModelManagerError", "ManifestError", "UnsupportedArchitectureError",
    "BackendCapabilityError", "PathTraversalError", "IntegrityError", "InsufficientDiskError",
    "LockTimeoutError", "SourceError", "EngineAdapterError",
]
