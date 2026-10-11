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
