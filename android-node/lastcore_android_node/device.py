"""Permission-free device probing for the Android minimal node.

The node must decide *before* accepting an assignment whether the device can
hold the assigned segment, so probing here is deliberately cheap and
permission-free: platform, architecture, CPU count, memory, battery and thermal
state.  Nothing runs in the background and no Android runtime permission is
requested.

Every value can be overridden via ``LASTCORE_ANDROID_*`` environment variables
so the admission logic is testable off-device (no emulator, no real phone).
On a real device the probe additionally tries the kernel interfaces an app can
read without a permission (``/proc/meminfo``, ``/sys/class/power_supply``,
``/sys/class/thermal``); anything unreadable stays ``None`` / ``UNKNOWN`` rather
than being guessed.  The framework APIs (``PowerManager.getCurrentThermalStatus``,
``BatteryManager``) are authoritative and must be preferred once the Android app
shell exposes them — see ``FEASIBILITY.md`` §3.
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
    thermal_celsius: float | None = None
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


# --- kernel interfaces readable by an app without a runtime permission --------
#
# Android's framework APIs (BatteryManager, PowerManager.getCurrentThermalStatus)
# are authoritative, but reaching them needs a JNI/platform channel.  Until the
# app shell provides one, the node tries the sysfs files these APIs also read.
# Anything unreadable degrades to None / UNKNOWN: never invent a battery level.

_DEFAULT_THERMAL_THRESHOLDS = (45.0, 50.0, 60.0)
# Only these two states mean "power is going in".  "Not charging" / "Discharging"
# / "Unknown" must NOT grant the battery-threshold exemption: an unreadable or
# explicitly-not-charging device is not confirmed to be charging.
_CHARGING_STATES = frozenset({"charging", "full"})


def _sysfs_root() -> str:
    return _env("SYSFS_ROOT") or "/"


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_sysfs_battery(root: str) -> tuple[int | None, bool | None]:
    base = os.path.join(root.rstrip("/"), "sys/class/power_supply")
    if not os.path.isdir(base):
        return None, None
    best: tuple[int, bool | None] | None = None
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None, None
    for entry in entries:
        if not entry.startswith("battery") and entry != "Battery":
            continue
        raw = _read_text(os.path.join(base, entry, "capacity"))
        if raw is None:
            continue
        digits = raw.strip().rstrip("%")
        try:
            percent = max(0, min(100, int(float(digits))))
        except ValueError:
            continue
        status = (_read_text(os.path.join(base, entry, "status")) or "").lower()
        charging: bool | None = None
        if status:
            charging = status in _CHARGING_STATES
        best = (percent, charging)
        break
    return best if best is not None else (None, None)


def _thermal_from_celsius(celsius: float, thresholds: tuple[float, float, float]) -> ThermalState:
    fair, serious, critical = thresholds
    if celsius >= critical:
        return ThermalState.CRITICAL
    if celsius >= serious:
        return ThermalState.SERIOUS
    if celsius >= fair:
        return ThermalState.FAIR
    return ThermalState.NOMINAL


def _thermal_thresholds() -> tuple[float, float, float]:
    fair, serious, critical = _DEFAULT_THERMAL_THRESHOLDS
    for name, default in (("THERMAL_FAIR_C", fair), ("THERMAL_SERIOUS_C", serious),
                          ("THERMAL_CRITICAL_C", critical)):
        raw = _env(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if name == "THERMAL_FAIR_C":
            fair = value
        elif name == "THERMAL_SERIOUS_C":
            serious = value
        else:
            critical = value
    return fair, serious, critical


def _read_sysfs_thermal(root: str, thresholds: tuple[float, float, float]) -> tuple[ThermalState, float | None]:
    base = os.path.join(root.rstrip("/"), "sys/class/thermal")
    if not os.path.isdir(base):
        return ThermalState.UNKNOWN, None
    try:
        zones = sorted(name for name in os.listdir(base) if name.startswith("thermal_zone"))
    except OSError:
        return ThermalState.UNKNOWN, None
    hottest: float | None = None
    for zone in zones:
        raw = _read_text(os.path.join(base, zone, "temp"))
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        celsius = value / 1000.0 if abs(value) >= 1000 else value
        if celsius <= -100 or celsius >= 200:
            continue
        if hottest is None or celsius > hottest:
            hottest = celsius
    if hottest is None:
        return ThermalState.UNKNOWN, None
    return _thermal_from_celsius(hottest, thresholds), hottest


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

    charging = _env_bool("CHARGING")

    thermal = ThermalState.parse(_env("THERMAL"))
    thermal_celsius: float | None = None

    if is_android and (battery is None or charging is None or thermal is ThermalState.UNKNOWN):
        root = _sysfs_root()
        if battery is None or charging is None:
            sysfs_percent, sysfs_charging = _read_sysfs_battery(root)
            if battery is None:
                battery = sysfs_percent
            if charging is None:
                charging = sysfs_charging
        if thermal is ThermalState.UNKNOWN:
            thermal, thermal_celsius = _read_sysfs_thermal(root, _thermal_thresholds())

    return DeviceProfile(
        platform_name=platform_name,
        machine=machine,
        is_android=is_android,
        is_arm64=machine.strip().lower() in ARM64_MACHINES,
        cpu_count=_env_int("CPU_COUNT") or os.cpu_count() or 1,
        total_memory_mb=total,
        available_memory_mb=available,
        battery_percent=battery,
        charging=charging,
        thermal=thermal,
        thermal_celsius=thermal_celsius,
        foreground_only=_env_bool("FOREGROUND_ONLY") is not False,
    )
