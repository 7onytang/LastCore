"""node-agent ``Executor`` implementation for the Android node.

Contract (see ``node-agent/PROTOCOL.md`` §1): one fresh instance per assignment,
``run(context)`` blocks until the shard finishes, cancellation is polled at
least once per second through ``context.cancel``, and ``ExecutionCancelled`` is
raised on cancel / deadline.  When ``lastcore_node_agent`` is importable its
``Executor`` / ``ExecutionCancelled`` are reused so the node can load this via
``--executor lastcore_android_node.executor:executor_factory``; otherwise
compatible fallbacks keep this package runnable standalone.

Cancellation granularity (bounded, not a blanket promise): the executor guards
``context.cancel`` / deadline *between* adapter calls, so the guaranteed poll
interval is ``max(one load(), step_seconds, one run() call)``.  ``step_seconds``
is required to be ``<= poll_seconds`` and a real adapter must return control
from each call within ``poll_seconds`` (1s); an adapter that blocks for a whole
shard — including inside ``load()`` — cannot be interrupted.  This is a contract
requirement on future ONNX / ExecuTorch adapters and is recorded in
``FEASIBILITY.md`` §5; the ≤1s figure is verified only for the simulated backend
and short steps, not for a real backend or a long load.

Admission needs *real* model metadata.  The executor refuses (``MissingModelMetadata``)
when the model shape, the shard's weight bytes, or the context length is unknown
instead of falling back to a toy shape — an accepted assignment has to be sized
against the real shard, and the model metadata source (LC-9 manifest) is not
wired yet.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from .backend import (
    BackendAdapter,
    BackendUnavailable,
    SegmentSpec,
    default_adapters,
    select_backend,
)
from .budget import Decision, ModelShape, build_budget, evaluate
from .device import DeviceProfile, detect

try:  # pragma: no cover - depends on the sibling node-agent package
    from lastcore_node_agent.executor import (  # type: ignore
        ExecutionCancelled,
        Executor as _BaseExecutor,
    )
except Exception:  # pragma: no cover - standalone fallback

    class ExecutionCancelled(Exception):  # type: ignore[no-redef]
        def __init__(self, reason: str = "cancelled"):
            super().__init__(reason)
            self.reason = reason

    class _BaseExecutor:  # type: ignore[no-redef]
        name = "executor"


class AndroidNodeError(RuntimeError):
    """Unrecoverable local node error; node-agent reports the shard failed."""


class SizeRejected(AndroidNodeError):
    """The device refused the assignment by the admission policy."""

    def __init__(self, decision: Decision, reason: str):
        super().__init__(f"{decision.value}: {reason}")
        self.decision = decision
        self.reason = reason


class MissingModelMetadata(AndroidNodeError):
    """The assignment cannot be sized: real metadata is missing.

    Raised instead of admitting against a placeholder shape.  The values must
    come from the model manifest / assignment (LC-9 plus the LC-5 contract);
    until that is wired, an unconfigured node refuses rather than guessing.
    """


DEFAULT_DTYPE_BYTES = {"float32": 4, "float16": 2, "int8": 1}


class AndroidExecutor(_BaseExecutor):
    name = "android-node"

    def __init__(
        self,
        *,
        profile: DeviceProfile | None = None,
        adapters: Sequence[BackendAdapter] | None = None,
        shape: ModelShape | None = None,
        weights_path: str | None = None,
        shard_weight_bytes: int | None = None,
        dtype: str = "float16",
        batch: int = 1,
        tokens: int | None = None,
        runtime_overhead_mb: int = 128,
        steps: int = 1,
        step_seconds: float = 0.0,
        poll_seconds: float = 0.5,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        detect_profile: Callable[[], DeviceProfile] = detect,
    ) -> None:
        if poll_seconds <= 0 or poll_seconds > 1:
            raise ValueError("poll_seconds must be in (0, 1] to satisfy the 1s cancel contract")
        if step_seconds < 0:
            raise ValueError("step_seconds must be non-negative")
        if step_seconds > poll_seconds:
            raise ValueError(
                f"step_seconds={step_seconds} exceeds poll_seconds={poll_seconds}: "
                "the sleep is not interruptible, so it would break the cancel-poll contract"
            )
        if dtype not in DEFAULT_DTYPE_BYTES:
            raise ValueError(f"unsupported dtype {dtype!r}")
        self._profile = profile
        self._adapters = list(adapters) if adapters is not None else None
        self._shape = shape
        self._weights_path = weights_path
        self._shard_weight_bytes = shard_weight_bytes
        self._dtype = dtype
        self._batch = batch
        self._tokens = tokens
        self._runtime_overhead_mb = runtime_overhead_mb
        self._steps = max(1, steps)
        self._step_seconds = float(step_seconds)
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._detect = detect_profile

    def _required_metadata(self) -> tuple[ModelShape, int, int]:
        """Return ``(shape, shard_weight_bytes, tokens)`` or refuse admission."""
        missing: list[str] = []
        if self._shape is None:
            missing.append("model shape (layers/d_model/heads/vocab)")
        if self._shard_weight_bytes is None or self._shard_weight_bytes <= 0:
            missing.append("shard_weight_bytes")
        if self._tokens is None or self._tokens <= 0:
            missing.append("tokens (context length)")
        if missing:
            raise MissingModelMetadata(
                "refusing admission without real model metadata: " + ", ".join(missing)
            )
        assert self._shape is not None and self._tokens is not None
        assert self._shard_weight_bytes is not None
        return self._shape, self._shard_weight_bytes, self._tokens

    def _segment(self, context, shape: ModelShape) -> SegmentSpec:
        return SegmentSpec(
            model_id=str(getattr(context, "model_id", "") or ""),
            layer_start=int(context.layer_start),
            layer_end=int(context.layer_end),
            shape=shape,
            weights_path=self._weights_path,
        )

    def run(self, context) -> None:
        profile = self._profile or self._detect()
        shape, shard_weight_bytes, tokens = self._required_metadata()
        segment = self._segment(context, shape)

        budget = build_budget(
            segment.shard_layers,
            shard_weight_bytes=shard_weight_bytes,
            shape=shape,
            batch=self._batch,
            tokens=tokens,
            dtype_bytes=DEFAULT_DTYPE_BYTES[self._dtype],
            runtime_overhead_mb=self._runtime_overhead_mb,
        )
        decision, reason = evaluate(profile, budget)
        if decision is not Decision.ACCEPT:
            raise SizeRejected(decision, reason)

        adapters = self._adapters if self._adapters is not None else default_adapters()
        try:
            adapter = select_backend(profile, adapters, dtype=self._dtype)
        except BackendUnavailable as exc:
            raise AndroidNodeError(str(exc)) from exc

        self._guard(context)
        try:
            # `load` is inside the try: a partially loaded segment must still be
            # released.  Adapters must make `release()` idempotent and safe
            # after a failed load (asserted in tests).
            adapter.load(segment)
            for step in range(self._steps):
                self._guard(context)
                adapter.run(list(range(tokens)))
                self._guard(context)
                if self._step_seconds and step + 1 < self._steps:
                    self._sleeper(self._step_seconds)
        finally:
            adapter.release()

    def _guard(self, context) -> None:
        cancel = getattr(context, "cancel", None)
        if cancel is not None and cancel.is_set():
            raise ExecutionCancelled(getattr(context, "cancel_reason", "") or "cancelled")
        deadline = getattr(context, "deadline", None)
        if deadline is not None and deadline > 0 and self._clock() >= deadline:
            raise ExecutionCancelled("deadline")


def executor_factory(**kwargs) -> AndroidExecutor:
    """Factory consumed by ``node-agent --executor lastcore_android_node.executor:executor_factory``."""
    return AndroidExecutor(**kwargs)
