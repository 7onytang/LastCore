"""ONNX Runtime Mobile adapter (preferred backend, see FEASIBILITY.md).

Only the availability probe and the capability description are implemented here;
the real ``load`` / ``run`` will be wired once the engine exposes a per-segment
ONNX export path and the choice is approved.  Constructing the adapter without
the runtime raises :class:`BackendUnavailable` instead of silently degrading.
"""

from __future__ import annotations

from typing import Sequence

from ..backend import BackendCapabilities, BackendUnavailable, SegmentSpec


def backend_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:  # pragma: no cover - environment dependent
        return False
    return True


class OnnxRuntimeMobileBackend:
    capabilities = BackendCapabilities(
        name="onnxruntime-mobile",
        license="MIT",
        model_forms=("onnx",),
        dtypes=("float32", "float16", "int8"),
        quantization=("none", "float16", "int8"),
        max_sequence=8192,
        arm64=True,
        supports_cancellation=True,
        apk_size_mb=8.0,
        maintenance="active (Microsoft)",
    )

    def __init__(self) -> None:
        if not backend_available():
            raise BackendUnavailable("onnxruntime is not installed on this device")
        self._session = None

    def load(self, segment: SegmentSpec) -> None:
        if not segment.weights_path:
            raise BackendUnavailable("onnxruntime backend requires a per-segment .onnx file")
        raise BackendUnavailable(
            "per-segment ONNX export is not available yet; pending engine export path and LC-5 approval"
        )

    def run(self, tokens: Sequence[int]) -> list[float]:
        raise BackendUnavailable("onnxruntime backend is not wired yet")

    def release(self) -> None:
        self._session = None
