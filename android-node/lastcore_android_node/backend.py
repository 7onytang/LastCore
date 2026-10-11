"""Backend abstraction for the Android node.

A backend is only responsible for ``load`` / ``run`` / ``release`` of one
*already selected* segment.  Selection and admission live one layer up so the
same adapter works for ONNX Runtime Mobile, ExecuTorch or a test double, and so
nothing here duplicates ``inference-engine``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from .budget import ModelShape
from .device import DeviceProfile


class BackendUnavailable(RuntimeError):
    """Raised when a backend's runtime is missing or not usable on this device."""


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    license: str
    model_forms: tuple[str, ...]
    dtypes: tuple[str, ...]
    quantization: tuple[str, ...] = ()
    max_sequence: int = 0
    arm64: bool = True
    supports_cancellation: bool = True
    apk_size_mb: float | None = None
    maintenance: str = "active"


@dataclass(frozen=True)
class SegmentSpec:
    model_id: str
    layer_start: int
    layer_end: int
    shape: ModelShape
    weights_path: str | None = None

    def __post_init__(self) -> None:
        if self.layer_start < 0 or self.layer_end <= self.layer_start:
            raise ValueError(f"invalid layer segment [{self.layer_start}, {self.layer_end})")
        if self.layer_end > self.shape.num_layers:
            raise ValueError(f"segment ends at {self.layer_end} but model has {self.shape.num_layers} layers")

    @property
    def shard_layers(self) -> int:
        return self.layer_end - self.layer_start

    @property
    def is_first(self) -> bool:
        return self.layer_start == 0

    @property
    def is_last(self) -> bool:
        return self.layer_end == self.shape.num_layers


@runtime_checkable
class BackendAdapter(Protocol):
    capabilities: BackendCapabilities

    def load(self, segment: SegmentSpec) -> None: ...

    def run(self, tokens: Sequence[int]) -> list[float]: ...

    def release(self) -> None: ...


def select_backend(
    profile: DeviceProfile,
    adapters: Sequence[BackendAdapter],
    *,
    dtype: str = "float16",
) -> BackendAdapter:
    """Pick the first adapter that can run on this device, or raise.

    ``adapters`` is ordered by preference; the caller (or :func:`default_adapters`)
    encodes the priority that ``FEASIBILITY.md`` recommends.
    """
    if not adapters:
        raise BackendUnavailable("no backends are registered")
    reasons: list[str] = []
    for adapter in adapters:
        caps = adapter.capabilities
        if profile.is_arm64 and not caps.arm64:
            reasons.append(f"{caps.name}: no arm64 support")
            continue
        if dtype not in caps.dtypes:
            reasons.append(f"{caps.name}: dtype {dtype} unsupported ({', '.join(caps.dtypes)})")
            continue
        return adapter
    raise BackendUnavailable("no backend fits this device: " + "; ".join(reasons))


def default_adapters() -> list[BackendAdapter]:
    """Preferred backends in priority order, best-effort constructed.

    Backends whose runtime is absent are skipped here; the reference adapter is
    always present so the executor lifecycle stays exercisable off-device.
    """
    from .backends.onnxruntime_mobile import OnnxRuntimeMobileBackend, backend_available
    from .backends.reference import ReferenceBackend

    adapters: list[BackendAdapter] = []
    if backend_available():
        adapters.append(OnnxRuntimeMobileBackend())
    adapters.append(ReferenceBackend())
    return adapters
