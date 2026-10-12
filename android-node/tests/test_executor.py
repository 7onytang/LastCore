from __future__ import annotations

import threading

import pytest

from lastcore_android_node.backend import BackendUnavailable
from lastcore_android_node.backends.reference import ReferenceBackend
from lastcore_android_node.budget import Decision, ModelShape
from lastcore_android_node.device import DeviceProfile, ThermalState
from lastcore_android_node.executor import (
    AndroidExecutor,
    AndroidNodeError,
    ExecutionCancelled,
    MissingModelMetadata,
    SizeRejected,
)

SHAPE = ModelShape(num_layers=4, d_model=32, num_heads=4, vocab=64)
SHARD_WEIGHT_BYTES = 4 * 1024 * 1024


class FakeContext:
    """Stand-in for lastcore_node_agent.executor.ExecutionContext (same fields)."""

    def __init__(self, cancel: threading.Event | None = None, deadline: float = 1e18,
                 layer_start: int = 0, layer_end: int = 2):
        self.task_id = "task-1"
        self.assignment_id = "assign-1"
        self.model_id = "tiny"
        self.position = 0
        self.layer_start = layer_start
        self.layer_end = layer_end
        self.previous_node = None
        self.next_node = None
        self.deadline = deadline
        self.cancel = cancel or threading.Event()
        self.cancel_reason = ""


def profile(**overrides) -> DeviceProfile:
    base = dict(platform_name="Android", machine="arm64", is_android=True, is_arm64=True,
                cpu_count=8, total_memory_mb=8192, available_memory_mb=4096,
                battery_percent=90, charging=False, thermal=ThermalState.NOMINAL)
    base.update(overrides)
    return DeviceProfile(**base)


def executor(backend: ReferenceBackend, **kwargs) -> AndroidExecutor:
    kwargs.setdefault("profile", profile())
    kwargs.setdefault("shape", SHAPE)
    kwargs.setdefault("shard_weight_bytes", SHARD_WEIGHT_BYTES)
    kwargs.setdefault("tokens", 128)
    kwargs.setdefault("adapters", [backend])
    return AndroidExecutor(**kwargs)


def test_happy_path_loads_runs_and_releases():
    backend = ReferenceBackend()
    executor(backend).run(FakeContext())
    assert backend.loads == 1
    assert backend.runs == 1
    assert backend.releases == 1
    assert not backend.loaded


def test_executor_name_matches_node_agent_contract():
    assert executor(ReferenceBackend()).name == "android-node"


def test_cancelled_before_start_raises_without_loading():
    backend = ReferenceBackend()
    cancel = threading.Event()
    cancel.set()
    context = FakeContext(cancel=cancel)
    context.cancel_reason = "task-terminal"
    with pytest.raises(ExecutionCancelled) as excinfo:
        executor(backend).run(context)
    assert excinfo.value.reason == "task-terminal"
    # Nothing was acquired, so nothing needs releasing: the pre-load guard runs
    # before any adapter call.
    assert backend.loads == 0 and backend.runs == 0 and backend.releases == 0


def test_cancellation_checked_between_steps():
    backend = ReferenceBackend()

    class CancelOnSecondStep:
        def __init__(self):
            self.calls = 0

        def __call__(self, _tokens):
            self.calls += 1
            if self.calls == 2:
                cancel.set()
            return [0.0]

    cancel = threading.Event()
    backend.run = CancelOnSecondStep()  # type: ignore[assignment]
    with pytest.raises(ExecutionCancelled):
        executor(backend, steps=3).run(FakeContext(cancel=cancel))
    assert backend.releases == 1


def test_deadline_expiry_raises():
    backend = ReferenceBackend()
    context = FakeContext(deadline=10.0)
    with pytest.raises(ExecutionCancelled) as excinfo:
        executor(backend, clock=lambda: 11.0).run(context)
    assert excinfo.value.reason == "deadline"
    # The pre-load guard fires first, so nothing is released and nothing leaked.
    assert backend.releases == 0


def test_resource_rejection_is_reported_as_size_rejected():
    backend = ReferenceBackend()
    with pytest.raises(SizeRejected) as excinfo:
        executor(backend, profile=profile(battery_percent=3, charging=False)).run(FakeContext())
    assert excinfo.value.decision is Decision.REJECT_BATTERY
    assert backend.loads == 0 and backend.releases == 0


def test_missing_backend_becomes_node_error():
    with pytest.raises(AndroidNodeError):
        executor(ReferenceBackend(), adapters=[]).run(FakeContext())


def test_poll_interval_must_satisfy_one_second_contract():
    with pytest.raises(ValueError):
        executor(ReferenceBackend(), poll_seconds=1.5)
    with pytest.raises(ValueError):
        executor(ReferenceBackend(), poll_seconds=0)


def test_step_seconds_cannot_exceed_the_poll_interval():
    """A non-interruptible sleep larger than poll_seconds would break the contract."""
    with pytest.raises(ValueError):
        executor(ReferenceBackend(), poll_seconds=0.5, step_seconds=2.0)
    executor(ReferenceBackend(), poll_seconds=0.5, step_seconds=0.25)


def test_missing_metadata_refuses_admission():
    backend = ReferenceBackend()
    context = FakeContext()
    for missing in ({"shape": None}, {"shard_weight_bytes": None},
                    {"shard_weight_bytes": 0}, {"tokens": None}, {"tokens": 0}):
        kwargs = dict(profile=profile(), shape=SHAPE, shard_weight_bytes=SHARD_WEIGHT_BYTES,
                      tokens=128, adapters=[backend])
        kwargs.update(missing)
        with pytest.raises(MissingModelMetadata):
            AndroidExecutor(**kwargs).run(context)
    assert backend.loads == 0 and backend.releases == 0


def test_no_default_adapter_is_used_when_none_is_configured(monkeypatch):
    """Production path must fail closed: no simulated backend by default."""
    import lastcore_android_node.executor as executor_module

    monkeypatch.setattr(executor_module, "default_adapters", lambda **_: [])
    with pytest.raises(AndroidNodeError):
        executor(ReferenceBackend(), adapters=None).run(FakeContext())


def test_load_failure_still_releases():
    class BrokenLoad(ReferenceBackend):
        def load(self, segment):
            self.loads += 1
            raise BackendUnavailable("half-loaded then failed")

    backend = BrokenLoad()
    with pytest.raises(BackendUnavailable):
        executor(backend).run(FakeContext())
    assert backend.loads == 1
    assert backend.releases == 1, "a failed load must still be released"
    assert not backend.loaded
