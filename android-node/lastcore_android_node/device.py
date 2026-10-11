"""Permission-free device probing for the Android minimal node.

The node must decide *before* accepting an assignment whether the device can
hold the assigned segment, so probing here is deliberately cheap and
permission-free: platform, architecture, CPU count, memory, battery and thermal
state.  Nothing runs in the background and no Android runtime permission is
requested.

Every value can be overridden via ``LASTCORE_ANDROID_*`` environment variables
so the admission logic is testable off-device (no emulator, no real phone).
"""

from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass
from enum import Enum

ARM64_MACHINES = frozenset({"arm64", "aarch64", "armv8", "armv8l", "arm64-v8a"})
_ENV_PREFIX = "LASTCORE_ANDROID_"


class ThermalState(str, Enum):
    NOMINAL = "nominal"
    FAIR = "fair"
    SERIOUS = "serious"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | "ThermalState | None") -> "ThermalState":
        if value is None:
            return cls.UNKNOWN
        if isinstance(value, ThermalState):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True)
class DeviceProfile:
    platform_name: str
    machine: str
    is_android: bool
    is_arm64: bool
    cpu_count: int
    total_memory_mb: int
    available_memory_mb: int | None = None
    battery_percent: int | None = None
    charging: bool | None = None
    thermal: ThermalState = ThermalState.UNKNOWN
    foreground_only: bool = True

    @property
    def usable_memory_mb(self) -> int:
        """Memory the node may plan against (measured, else total)."""
        if self.available_memory_mb is not None and self.available_memory_mb > 0:
            return self.available_memory_mb
        return self.total_memory_mb


def _env(name: str) -> str | None:
    value = os.environ.get(_ENV_PREFIX + name)
    return value if value not in (None, "") else None


def _env_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_bool(name: str) -> bool | None:
    raw = _env(name)
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _read_proc_meminfo() -> tuple[int | None, int | None]:
    """Return ``(total_mb, available_mb)`` from /proc/meminfo when present."""
    path = "/proc/meminfo"
    if not os.path.exists(path):
        return None, None
    total_kb = available_kb = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split()
                if not value:
                    continue
                if key == "MemTotal":
                    total_kb = int(value[0])
                elif key == "MemAvailable":
                    available_kb = int(value[0])
    except (OSError, ValueError):
        return None, None
    total = total_kb // 1024 if total_kb is not None else None
    available = available_kb // 1024 if available_kb is not None else None
    return total, available


def _sysconf_memory_mb() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return (pages * page_size) // (1024 * 1024)


def detect() -> DeviceProfile:
    """Probe the local device, honouring ``LASTCORE_ANDROID_*`` overrides."""
    machine = _env("MACHINE") or _platform.machine()
    platform_name = _env("PLATFORM") or _platform.system()

    proc_total, proc_available = _read_proc_meminfo() if platform_name.lower() == "linux" else (None, None)
    total = _env_int("TOTAL_MEMORY_MB") or proc_total or _sysconf_memory_mb() or 0
    available = _env_int("AVAILABLE_MEMORY_MB")
    if available is None:
        available = proc_available

    is_android = _env_bool("IS_ANDROID")
    if is_android is None:
        is_android = "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ

    battery = _env_int("BATTERY_PERCENT")
    if battery is not None:
        battery = max(0, min(100, battery))

    return DeviceProfile(
        platform_name=platform_name,
        machine=machine,
        is_android=is_android,
        is_arm64=machine.strip().lower() in ARM64_MACHINES,
        cpu_count=_env_int("CPU_COUNT") or os.cpu_count() or 1,
        total_memory_mb=total,
        available_memory_mb=available,
        battery_percent=battery,
        charging=_env_bool("CHARGING"),
        thermal=ThermalState.parse(_env("THERMAL")),
        foreground_only=_env_bool("FOREGROUND_ONLY") is not False,
    )
