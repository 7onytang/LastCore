"""Cross-component check against Engineer-A's real node-agent Executor contract.

Opt-in: set ``LASTCORE_NODE_AGENT_PATH`` to a checkout of the ``node-agent/``
package (its parent directory).  Use the pinned baseline
``agent/engineer-a-a/b33d0fac979a`` (``ba52895ae829dcf0c8c605de46103e3b25fad650``,
the node-agent revision LC-52/LC-54 pinned); a different revision may change the
contract.  Without the variable the module is skipped, so the Android component
still tests cleanly on a machine that has no node-agent checkout.

What this proves (off-device, no Android hardware):
  * ``AndroidExecutor`` is a real ``lastcore_node_agent.executor.Executor``
    subclass, so node-agent's ``isinstance`` gate accepts it.
  * ``load_executor("lastcore_android_node.executor:executor_factory")``
    returns a usable instance (the exact spec passed to ``node-agent
    --executor``).
  * a real ``ExecutionContext`` drives run / cancel / deadline, and the backend
    is always released (no leaked segment on the error paths).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from lastcore_android_node.backends.reference import ReferenceBackend
from lastcore_android_node.budget import ModelShape
from lastcore_android_node.device import DeviceProfile, ThermalState

NODE_AGENT_PATH = os.environ.get("LASTCORE_NODE_AGENT_PATH")

pytestmark = pytest.mark.skipif(
    not NODE_AGENT_PATH,
    reason="set LASTCORE_NODE_AGENT_PATH to a node-agent checkout "
    "(pinned baseline agent/engineer-a-a/b33d0fac979a = ba52895) to run the contract check",
)

SHAPE = ModelShape(num_layers=4, d_model=32, num_heads=4, vocab=64)
SHARD_WEIGHT_BYTES = 4 * 1024 * 1024


@pytest.fixture(scope="module")
def node_agent():
    if NODE_AGENT_PATH and NODE_AGENT_PATH not in sys.path:
        sys.path.insert(0, NODE_AGENT_PATH)
    try:
        from lastcore_node_agent import executor as node_executor
    except ImportError as exc:  # pragma: no cover - only when the path is wrong
        pytest.skip(f"node-agent not importable from {NODE_AGENT_PATH}: {exc}")
    return node_executor


def _profile() -> DeviceProfile:
    return DeviceProfile(
        platform_name="Android",
        machine="arm64",
        is_android=True,
        is_arm64=True,
        cpu_count=8,
        total_memory_mb=8192,
        available_memory_mb=4096,
        battery_percent=90,
        charging=False,
        thermal=ThermalState.NOMINAL,
    )


def _executor(backend: ReferenceBackend):
    from lastcore_android_node.executor import AndroidExecutor

    return AndroidExecutor(
        profile=_profile(), shape=SHAPE, shard_weight_bytes=SHARD_WEIGHT_BYTES,
        tokens=128, adapters=[backend],
    )


def test_executor_is_a_node_agent_executor(node_agent):
    assert isinstance(_executor(ReferenceBackend()), node_agent.Executor)


def test_load_executor_accepts_the_documented_spec(node_agent):
    loaded = node_agent.load_executor("lastcore_android_node.executor:executor_factory")
    assert isinstance(loaded, node_agent.Executor)
    assert loaded.name == "android-node"


def test_real_execution_context_runs_and_releases(node_agent):
    backend = ReferenceBackend()
    context = node_agent.ExecutionContext(
        task_id="t1",
        assignment_id="a1",
        model_id="tiny",
        position=0,
        layer_start=0,
        layer_end=2,
        previous_node=None,
        next_node=None,
        deadline=time.time() + 30,
    )
    _executor(backend).run(context)
    assert (backend.loads, backend.runs, backend.releases) == (1, 1, 1)
    assert not backend.loaded


def test_cancel_before_start_stops_without_loading(node_agent):
    backend = ReferenceBackend()
    context = node_agent.ExecutionContext(
        task_id="t1", assignment_id="a1", model_id="tiny", position=0,
        layer_start=0, layer_end=2, previous_node=None, next_node=None,
        deadline=time.time() + 30,
    )
    context.request_cancel("task-terminal")
    with pytest.raises(node_agent.ExecutionCancelled):
        _executor(backend).run(context)
    # Cancelled before any adapter call: nothing acquired, nothing to leak.
    assert (backend.loads, backend.runs, backend.releases) == (0, 0, 0)
    assert not backend.loaded


def test_deadline_before_start_stops_without_loading(node_agent):
    backend = ReferenceBackend()
    context = node_agent.ExecutionContext(
        task_id="t1", assignment_id="a1", model_id="tiny", position=0,
        layer_start=0, layer_end=2, previous_node=None, next_node=None,
        deadline=time.time() - 1,
    )
    with pytest.raises(node_agent.ExecutionCancelled):
        _executor(backend).run(context)
    assert (backend.loads, backend.runs, backend.releases) == (0, 0, 0)
    assert not backend.loaded


def test_second_context_is_isolated(node_agent):
    """One Executor instance per assignment: state must not leak between runs."""
    backend = ReferenceBackend()
    first = node_agent.ExecutionContext(
        task_id="t1", assignment_id="a1", model_id="tiny", position=0,
        layer_start=0, layer_end=2, previous_node=None, next_node=None,
        deadline=time.time() + 30,
    )
    second = node_agent.ExecutionContext(
        task_id="t2", assignment_id="a2", model_id="tiny", position=1,
        layer_start=2, layer_end=4, previous_node=None, next_node=None,
        deadline=time.time() + 30,
    )
    _executor(backend).run(first)
    _executor(backend).run(second)
    assert (backend.loads, backend.runs, backend.releases) == (2, 2, 2)


def test_threaded_cancel_is_observed_while_running(node_agent):
    """Cancel from another thread stops a long shard and releases the segment."""
    backend = ReferenceBackend()
    context = node_agent.ExecutionContext(
        task_id="t1", assignment_id="a1", model_id="tiny", position=0,
        layer_start=0, layer_end=2, previous_node=None, next_node=None,
        deadline=time.time() + 30,
    )
    from lastcore_android_node.executor import AndroidExecutor

    executor = AndroidExecutor(
        profile=_profile(), shape=SHAPE, shard_weight_bytes=SHARD_WEIGHT_BYTES,
        tokens=128, adapters=[backend], steps=1000,
        step_seconds=0.01, poll_seconds=0.05,
    )
    timer = threading.Timer(0.1, lambda: context.request_cancel("shutdown"))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(node_agent.ExecutionCancelled):
            executor.run(context)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.0, "cancel must be observed within the 1s contract"
    assert backend.releases == 1 and not backend.loaded


def test_android_node_is_not_a_copy_of_the_engine():
    """Boundary check: the Android component must not import engine/desktop modules."""
    package_root = Path(__file__).resolve().parents[1] / "lastcore_android_node"
    forbidden = ("inference_engine", "communication", "lastcore_node_agent.agent")
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, f"android-node must not depend on desktop/engine code: {offenders}"
