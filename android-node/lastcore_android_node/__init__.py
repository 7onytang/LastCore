"""Android minimal node for LastCore (LC-13 phase 1).

Additive component: it implements the node-agent ``Executor`` contract and
delegates real compute to a pluggable :class:`BackendAdapter`.  It never imports
or modifies ``controller-server`` / ``inference-engine`` / ``communication``.
"""

from .backend import (
    BackendAdapter,
    BackendCapabilities,
    BackendUnavailable,
    SegmentSpec,
    default_adapters,
    select_backend,
)
from .budget import Decision, MemoryBudget, ModelShape, build_budget, evaluate
from .device import DeviceProfile, ThermalState, detect
from .executor import (
    AndroidExecutor,
    AndroidNodeError,
    MissingModelMetadata,
    SizeRejected,
    executor_factory,
)

__all__ = [
    "AndroidExecutor",
    "AndroidNodeError",
    "BackendAdapter",
    "BackendCapabilities",
    "BackendUnavailable",
    "Decision",
    "DeviceProfile",
    "MemoryBudget",
    "MissingModelMetadata",
    "ModelShape",
    "SegmentSpec",
    "SizeRejected",
    "ThermalState",
    "build_budget",
    "default_adapters",
    "detect",
    "evaluate",
    "executor_factory",
    "select_backend",
]

__version__ = "0.1.0"
