from __future__ import annotations

import pytest

from lastcore_android_node.device import DeviceProfile, ThermalState, detect


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("LASTCORE_ANDROID_"):
            monkeypatch.delenv(key, raising=False)


def test_detect_arm64_and_thermal_override(monkeypatch):
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_TOTAL_MEMORY_MB", "6144")
    monkeypatch.setenv("LASTCORE_ANDROID_AVAILABLE_MEMORY_MB", "2048")
    monkeypatch.setenv("LASTCORE_ANDROID_BATTERY_PERCENT", "140")
    monkeypatch.setenv("LASTCORE_ANDROID_THERMAL", "SERIOUS")
    monkeypatch.setenv("LASTCORE_ANDROID_CPU_COUNT", "8")

    profile = detect()

    assert profile.is_android is True
    assert profile.is_arm64 is True
    assert profile.cpu_count == 8
    assert profile.total_memory_mb == 6144
    assert profile.available_memory_mb == 2048
    assert profile.battery_percent == 100
    assert profile.thermal is ThermalState.SERIOUS
    assert profile.foreground_only is True


def test_usable_memory_prefers_measured_value():
    measured = DeviceProfile("Android", "arm64", True, True, 8, 8192, available_memory_mb=3000)
    unknown = DeviceProfile("Android", "arm64", True, True, 8, 8192)
    assert measured.usable_memory_mb == 3000
    assert unknown.usable_memory_mb == 8192


def test_thermal_parse_is_lenient():
    assert ThermalState.parse("FAIR") is ThermalState.FAIR
    assert ThermalState.parse("nonsense") is ThermalState.UNKNOWN
    assert ThermalState.parse(None) is ThermalState.UNKNOWN


def _fake_sysfs(tmp_path, *, capacity="72", status="Discharging", temps=("38000",)):
    power = tmp_path / "sys/class/power_supply/battery"
    power.mkdir(parents=True)
    (power / "capacity").write_text(capacity)
    (power / "status").write_text(status)
    thermal = tmp_path / "sys/class/thermal"
    for index, temp in enumerate(temps):
        zone = thermal / f"thermal_zone{index}"
        zone.mkdir(parents=True)
        (zone / "temp").write_text(temp)
        (zone / "type").write_text("cpu")
    return tmp_path


def test_sysfs_battery_and_thermal_are_read(monkeypatch, tmp_path):
    _fake_sysfs(tmp_path, capacity="72", status="Charging", temps=("38000", "44500"))
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_SYSFS_ROOT", str(tmp_path))

    profile = detect()

    assert profile.battery_percent == 72
    assert profile.charging is True
    # hottest zone wins; 44.5 C maps to FAIR under the default 45/50/60 thresholds
    assert profile.thermal_celsius == pytest.approx(44.5)
    assert profile.thermal is ThermalState.NOMINAL


def test_sysfs_thermal_serious_threshold(monkeypatch, tmp_path):
    _fake_sysfs(tmp_path, temps=("51200",))
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_SYSFS_ROOT", str(tmp_path))

    assert detect().thermal is ThermalState.SERIOUS


def test_sysfs_thermal_thresholds_are_configurable(monkeypatch, tmp_path):
    _fake_sysfs(tmp_path, temps=("51200",))
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_SYSFS_ROOT", str(tmp_path))
    monkeypatch.setenv("LASTCORE_ANDROID_THERMAL_SERIOUS_C", "55")

    assert detect().thermal is ThermalState.FAIR


def test_missing_sysfs_stays_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_SYSFS_ROOT", str(tmp_path))

    profile = detect()

    assert profile.battery_percent is None
    assert profile.charging is None
    assert profile.thermal is ThermalState.UNKNOWN
    assert profile.thermal_celsius is None


def test_env_override_wins_over_sysfs(monkeypatch, tmp_path):
    _fake_sysfs(tmp_path, capacity="10", temps=("61000",))
    monkeypatch.setenv("LASTCORE_ANDROID_PLATFORM", "Linux")
    monkeypatch.setenv("LASTCORE_ANDROID_MACHINE", "aarch64")
    monkeypatch.setenv("LASTCORE_ANDROID_IS_ANDROID", "true")
    monkeypatch.setenv("LASTCORE_ANDROID_SYSFS_ROOT", str(tmp_path))
    monkeypatch.setenv("LASTCORE_ANDROID_BATTERY_PERCENT", "90")
    monkeypatch.setenv("LASTCORE_ANDROID_THERMAL", "nominal")

    profile = detect()

    assert profile.battery_percent == 90
    assert profile.thermal is ThermalState.NOMINAL
