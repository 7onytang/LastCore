"""Deterministic simulated backend used to exercise the adapter lifecycle.

This is **not** inference.  It exists so the executor's load / run / release,
cancellation and deadline paths can be tested on a plain dev machine with no
Android device, no numpy and no model file.  Real compute is expected to arrive
through the ONNX Runtime Mobile adapter once the engine export path is approved.
"""

from __future__ import annotations

from typing import Sequence

from ..backend import BackendCapabilities, BackendUnavailable, SegmentSpec


class ReferenceBackend:
    capabilities = BackendCapabilities(
        name="reference-simulated",
        license="Apache-2.0",
        model_forms=("none",),
        dtypes=("float32", "float16", "int8"),
        quantization=("none", "int8"),
        max_sequence=4096,
        arm64=True,
        supports_cancellation=True,
        apk_size_mb=0.0,
        maintenance="internal test double",
    )

    def __init__(self) -> None:
        self._segment: SegmentSpec | None = None
        self._released = True
        self.loads = 0
        self.runs = 0
        self.releases = 0

    @property
    def loaded(self) -> bool:
        return self._segment is not None and not self._released

    def load(self, segment: SegmentSpec) -> None:
        if segment.weights_path is not None:
            raise BackendUnavailable("reference backend needs no weights file")
        self._segment = segment
        self._released = False
        self.loads += 1

    def run(self, tokens: Sequence[int]) -> list[float]:
        if self._segment is None or self._released:
            raise BackendUnavailable("run() before load() or after release()")
        self.runs += 1
        segment = self._segment
        base = segment.layer_start * 131 + segment.layer_end * 17
        return [((token * 31 + index * 7 + base) % 1009) / 1009.0 for index, token in enumerate(tokens)]

    def release(self) -> None:
        self._segment = None
        self._released = True
        self.releases += 1
