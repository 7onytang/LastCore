"""model-manager：模型 revision、层边界与后端能力清单（严格解析 + 交叉核对）。

面向使用者的入口（本提交只含清单与布局层）：

* :func:`load_manifest` / :class:`ModelManifest` —— 严格解析并校验清单；
* :func:`build_manifest` / :func:`describe_artifact` —— 由本地文件生成清单（只记录哈希与字节）。
"""

from __future__ import annotations

from .errors import (BackendCapabilityError, EngineAdapterError, IntegrityError,
                     InsufficientDiskError, LockTimeoutError, ManifestError,
                     ModelManagerError, PathTraversalError, SourceError,
                     UnsupportedArchitectureError)
from .layout import Layout, layout_for
from .manifest import (Artifact, Backend, License, ModelManifest, Resources,
                       check_backend_capability)
from .publish import ArtifactInput, build_manifest, describe_artifact

load_manifest = ModelManifest.load

__all__ = [
    "ModelManifest", "load_manifest", "Artifact", "ArtifactInput", "Backend", "License",
    "Resources", "Layout", "layout_for", "check_backend_capability",
    "build_manifest", "describe_artifact",
    "ModelManagerError", "ManifestError", "UnsupportedArchitectureError",
    "BackendCapabilityError", "PathTraversalError", "IntegrityError", "InsufficientDiskError",
    "LockTimeoutError", "SourceError", "EngineAdapterError",
]
