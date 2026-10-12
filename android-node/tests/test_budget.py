from __future__ import annotations

import pytest

from lastcore_android_node.budget import (
    AdmissionError,
    Decision,
    ModelShape,
    build_budget,
    estimate_activation_bytes,
    estimate_kv_bytes,
    evaluate,
)
from lastcore_android_node.device import DeviceProfile, ThermalState

SHAPE = ModelShape(num_layers=4, d_model=32, num_heads=4, vocab=64)


def profile(**overrides) -> DeviceProfile:
    base = dict(
        platform_name="Android",
        machine="arm64",
        is_android=True,
        is_arm64=True,
        cpu_count=8,
        total_memory_mb=8192,
        available_memory_mb=4096,
        battery_percent=80,
        charging=False,
        thermal=ThermalState.NOMINAL,
    )
    base.update(overrides)
    return DeviceProfile(**base)


def test_activation_and_kv_arithmetic():
    assert estimate_activation_bytes(1, 128, 256, 2) == 1 * 128 * 256 * 2
    assert estimate_kv_bytes(2, 1, 128, 256, 2) == 2 * 2 * 1 * 128 * 256 * 2


def test_budget_totals_include_runtime_overhead():
    budget = build_budget(2, 10 * 1024 * 1024, SHAPE, tokens=16, dtype_bytes=2, runtime_overhead_mb=64)
    assert budget.weights_bytes == 10 * 1024 * 1024
    assert budget.kv_bytes == estimate_kv_bytes(2, 1, 16, 32, 2)
    assert budget.runtime_overhead_bytes == 64 * 1024 * 1024
    assert budget.total_bytes == sum(
        [budget.weights_bytes, budget.activation_bytes, budget.kv_bytes, budget.runtime_overhead_bytes]
    )


def test_budget_rejects_non_positive_segments():
    with pytest.raises(AdmissionError):
        build_budget(0, 0, SHAPE)
    with pytest.raises(AdmissionError):
        build_budget(1, 0, SHAPE, tokens=0)


def test_accept_when_within_ceiling():
    budget = build_budget(2, 100 * 1024 * 1024, SHAPE, tokens=64, runtime_overhead_mb=64)
    decision, _ = evaluate(profile(), budget)
    assert decision is Decision.ACCEPT


def test_reject_when_over_ceiling():
    budget = build_budget(2, 4096 * 1024 * 1024, SHAPE, tokens=64)
    decision, reason = evaluate(profile(), budget)
    assert decision is Decision.REJECT_MEMORY
    assert "ceiling" in reason


def test_reject_when_memory_unknown():
    budget = build_budget(1, 0, SHAPE)
    decision, _ = evaluate(profile(total_memory_mb=0, available_memory_mb=0), budget)
    assert decision is Decision.REJECT_MEMORY


def test_thermal_and_battery_gates():
    budget = build_budget(1, 0, SHAPE)
    assert evaluate(profile(thermal=ThermalState.CRITICAL), budget)[0] is Decision.REJECT_THERMAL
    assert evaluate(profile(battery_percent=5, charging=False), budget)[0] is Decision.REJECT_BATTERY
    assert evaluate(profile(battery_percent=5, charging=True), budget)[0] is Decision.ACCEPT
    assert evaluate(profile(foreground_only=False), budget)[0] is Decision.REJECT_BACKGROUND


def test_unknown_charging_state_does_not_grant_the_exemption():
    """Only a confirmed 'charging' reading may skip the low-battery gate."""
    budget = build_budget(1, 0, SHAPE)
    decision, reason = evaluate(profile(battery_percent=5, charging=None), budget)
    assert decision is Decision.REJECT_BATTERY
    assert "not charging" in reason
