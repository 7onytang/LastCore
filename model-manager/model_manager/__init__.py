"""model-manager：模型 revision、层边界、后端能力与成本清单。

面向使用者的入口（本提交加入成本与引擎适配）：

* :func:`load_manifest` / :class:`ModelManifest` —— 严格解析并校验清单；
* :func:`model_cost` / :func:`segment_cost` / :func:`disk_requirement` —— 层 / embedding /
  head / dtype / 哈希成本；
* :func:`load_shard_for` / :func:`load_stage_for` —— 复用 inference-engine 的 loader 只读本段权重。
"""

from __future__ import annotations

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
from .publish import ArtifactInput, build_manifest, describe_artifact

load_manifest = ModelManifest.load
model_cost = cost_report

__all__ = [
    "ModelManifest", "load_manifest", "Artifact", "ArtifactInput", "Backend", "License",
    "Resources", "Layout", "layout_for", "check_backend_capability",
    "build_manifest", "describe_artifact",
    "model_cost", "cost_report", "CostReport", "SegmentCost", "segment_cost",
    "disk_requirement",
    "load_shard_for", "load_stage_for", "model_config", "expected_arrays",
    "checkpoint_artifact", "verify_checkpoint_contents",
    "ModelManagerError", "ManifestError", "UnsupportedArchitectureError",
    "BackendCapabilityError", "PathTraversalError", "IntegrityError", "InsufficientDiskError",
    "LockTimeoutError", "SourceError", "EngineAdapterError",
]
