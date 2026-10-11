"""Memory, battery and thermal admission model for an Android shard.

Only the arithmetic that decides *whether this device may accept a segment*
lives here.  It mirrors the segment convention of the controller and engine:
left-closed, right-open, zero-based ``[layer_start, layer_end)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .device import DeviceProfile, ThermalState


class AdmissionError(ValueError):
    pass


class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT_MEMORY = "reject_memory"
    REJECT_BATTERY = "reject_battery"
    REJECT_THERMAL = "reject_thermal"
    REJECT_BACKGROUND = "reject_background"


@dataclass(frozen=True)
class ModelShape:
    num_layers: int
    d_model: int
    num_heads: int
    vocab: int

    def __post_init__(self) -> None:
        for name in ("num_layers", "d_model", "num_heads", "vocab"):
            if getattr(self, name) <= 0:
                raise AdmissionError(f"{name} must be positive")
        if self.d_model % self.num_heads:
            raise AdmissionError("d_model must be divisible by num_heads")


@dataclass(frozen=True)
class MemoryBudget:
    weights_bytes: int
    activation_bytes: int
    kv_bytes: int
    runtime_overhead_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.weights_bytes + self.activation_bytes + self.kv_bytes + self.runtime_overhead_bytes

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)


def estimate_activation_bytes(batch: int, tokens: int, d_model: int, dtype_bytes: int) -> int:
    return batch * tokens * d_model * dtype_bytes


def estimate_kv_bytes(shard_layers: int, batch: int, tokens: int, d_model: int, dtype_bytes: int) -> int:
    # K and V per layer, for every token seen so far.
    return 2 * shard_layers * batch * tokens * d_model * dtype_bytes


def build_budget(
    shard_layers: int,
    shard_weight_bytes: int,
    shape: ModelShape,
    *,
    batch: int = 1,
    tokens: int = 1,
    dtype_bytes: int = 2,
    runtime_overhead_mb: int = 128,
) -> MemoryBudget:
    if shard_layers <= 0:
        raise AdmissionError("a segment must carry at least one layer")
    if batch <= 0 or tokens <= 0 or dtype_bytes <= 0:
        raise AdmissionError("batch, tokens and dtype_bytes must be positive")
    return MemoryBudget(
        weights_bytes=int(shard_weight_bytes),
        activation_bytes=estimate_activation_bytes(batch, tokens, shape.d_model, dtype_bytes),
        kv_bytes=estimate_kv_bytes(shard_layers, batch, tokens, shape.d_model, dtype_bytes),
        runtime_overhead_bytes=runtime_overhead_mb * 1024 * 1024,
    )


def evaluate(
    profile: DeviceProfile,
    budget: MemoryBudget,
    *,
    max_memory_ratio: float = 0.6,
    headroom: float = 0.15,
    min_battery_percent: int = 20,
) -> tuple[Decision, str]:
    """Return ``(decision, human-readable reason)`` for accepting this shard."""
    if not 0 < max_memory_ratio <= 1:
        raise AdmissionError("max_memory_ratio must be in (0, 1]")
    if headroom < 0:
        raise AdmissionError("headroom must be non-negative")
    if profile.foreground_only is False:
        return Decision.REJECT_BACKGROUND, "node runs foreground-only; no background execution"

    if profile.thermal in (ThermalState.SERIOUS, ThermalState.CRITICAL):
        return Decision.REJECT_THERMAL, f"thermal state {profile.thermal.value} blocks acceptance"

    if (
        profile.battery_percent is not None
        and profile.battery_percent < min_battery_percent
        and profile.charging is not True
    ):
        return (
            Decision.REJECT_BATTERY,
            f"battery {profile.battery_percent}% below {min_battery_percent}% and not charging",
        )

    usable = profile.usable_memory_mb
    if usable <= 0:
        return Decision.REJECT_MEMORY, "device memory is unknown; refusing to accept blindly"

    ceiling_mb = usable * max_memory_ratio
    required_mb = budget.total_mb * (1.0 + headroom)
    if required_mb > ceiling_mb:
        return (
            Decision.REJECT_MEMORY,
            f"needs {math.ceil(required_mb)}MB incl. headroom, ceiling is {math.floor(ceiling_mb)}MB",
        )
    return Decision.ACCEPT, f"peak {math.ceil(budget.total_mb)}MB within {math.floor(ceiling_mb)}MB ceiling"
