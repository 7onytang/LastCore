from __future__ import annotations

import pytest

from lastcore_android_node.backend import (
    BackendCapabilities,
    BackendUnavailable,
    SegmentSpec,
    default_adapters,
    select_backend,
)
from lastcore_android_node.backends.onnxruntime_mobile import OnnxRuntimeMobileBackend, backend_available
from lastcore_android_node.backends.reference import ReferenceBackend
from lastcore_android_node.budget import ModelShape
from lastcore_android_node.device import DeviceProfile

SHAPE = ModelShape(num_layers=4, d_model=32, num_heads=4, vocab=64)


def profile(**overrides) -> DeviceProfile:
    base = dict(platform_name="Android", machine="arm64", is_android=True, is_arm64=True,
                cpu_count=8, total_memory_mb=8192)
    base.update(overrides)
    return DeviceProfile(**base)


def segment(start=0, end=2, weights=None) -> SegmentSpec:
    return SegmentSpec("tiny", start, end, SHAPE, weights)


def test_segment_rejects_bad_ranges():
    with pytest.raises(ValueError):
        segment(2, 2)
    with pytest.raises(ValueError):
        segment(0, 9)


def test_reference_backend_lifecycle_and_determinism():
    backend = ReferenceBackend()
    assert not backend.loaded
    backend.load(segment())
    assert backend.loaded
    first = backend.run([1, 2, 3])
    second = backend.run([1, 2, 3])
    assert first == second
    assert len(first) == 3
    backend.release()
    assert not backend.loaded
    assert backend.loads == 1 and backend.runs == 2 and backend.releases == 1
    with pytest.raises(BackendUnavailable):
        backend.run([1])


def test_segment_differentiation_changes_output():
    first = ReferenceBackend()
    first.load(segment(0, 2))
    last = ReferenceBackend()
    last.load(segment(2, 4))
    assert first.run([5]) != last.run([5])


def test_select_backend_prefers_first_that_fits():
    reference = ReferenceBackend()
    chosen = select_backend(profile(), [reference], dtype="float16")
    assert chosen is reference


def test_select_backend_rejects_dtype_mismatch():
    class Float32Only(ReferenceBackend):
        capabilities = BackendCapabilities(
            name="float32-only", license="Apache-2.0", model_forms=("none",), dtypes=("float32",)
        )

    with pytest.raises(BackendUnavailable):
        select_backend(profile(), [Float32Only()], dtype="float16")


def test_select_backend_rejects_empty_registry():
    with pytest.raises(BackendUnavailable):
        select_backend(profile(), [])


def test_default_adapters_exclude_the_simulated_double():
    """Fail closed: the production candidate list must not contain a test double."""
    assert "reference-simulated" not in [a.capabilities.name for a in default_adapters()]
    assert "reference-simulated" in [
        a.capabilities.name for a in default_adapters(include_reference=True)
    ]


def test_production_default_fails_closed_without_a_real_runtime():
    if backend_available():
        pytest.skip("onnxruntime is installed in this environment")
    with pytest.raises(BackendUnavailable):
        select_backend(profile(), default_adapters(), dtype="float16")


def test_onnx_backend_reports_unavailable_without_runtime():
    if backend_available():
        pytest.skip("onnxruntime is installed in this environment")
    with pytest.raises(BackendUnavailable):
        OnnxRuntimeMobileBackend()
    assert OnnxRuntimeMobileBackend.capabilities.license == "MIT"
