from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from .config import AgentConfig
from .executor import ExecutionCancelled, Executor, ExecutionContext

LOG = logging.getLogger("lastcore.node_agent.stage")

DEFAULT_STAGE_TIMEOUT = 30.0
MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_WEIGHTS_NAME = "tiny-model.npz"


def _normalized_assignment(raw: dict) -> dict:
    """Controller HttpUrl values carry a trailing slash; inference-engine's
    parse_endpoint expects a bare host:port, so strip it on the copy we hand over."""
    normalized = dict(raw)
    for peer in ("next_node", "previous_node"):
        value = raw.get(peer)
        if isinstance(value, dict) and isinstance(value.get("endpoint"), str):
            copy = dict(value)
            copy["endpoint"] = value["endpoint"].rstrip("/")
            normalized[peer] = copy
    return normalized


def _parse_local_endpoint(endpoint: str) -> "tuple[str, int]":
    from inference_engine import parse_endpoint

    return parse_endpoint(endpoint.rstrip("/"))


class StageExecutor(Executor):
    """Runs one Controller assignment on inference-engine's StageServer.

    The server binds exactly the address this node registered as its endpoint,
    so the address the controller holds for peers is the address that listens.
    The assignment's ``deadline`` is absolute epoch seconds — the same base the
    engine and communication layer use — and is handed to the StageServer so the
    engine stops serving once it expires; the agent's deadline monitor stays as a
    second layer. ``complete_after`` served requests end the execution normally
    (the agent then reports completed); ``None`` serves until the agent cancels.
    """

    name = "stage"

    def __init__(self, *, model_config, weights_path, endpoint, complete_after=1,
                 timeout=DEFAULT_STAGE_TIMEOUT, serve_poll=0.25, sleep=time.sleep):
        self.model_config = model_config
        self.weights_path = weights_path
        self.endpoint = endpoint
        self.complete_after = complete_after
        self.timeout = timeout
        self.serve_poll = serve_poll
        self.sleep = sleep

    def run(self, context: ExecutionContext) -> None:
        from inference_engine import StageServer, load_assigned_stage

        if context.raw is None:
            from inference_engine.errors import ShardError
            raise ShardError("execution context carries no raw assignment payload")
        runtime, plan = load_assigned_stage(_normalized_assignment(context.raw),
                                            self.model_config, self.weights_path)
        address = _parse_local_endpoint(self.endpoint)
        server = StageServer(runtime, address=address, downstream=plan.next_endpoint,
                             timeout=self.timeout, deadline=plan.deadline, cancel=context.cancel)
        LOG.info("stage %s serving on %s:%s layers [%s, %s) role=%s downstream=%s deadline=%s",
                 context.assignment_id, address[0], address[1], runtime.layer_start,
                 runtime.layer_end, runtime.role, plan.next_endpoint, plan.deadline)
        try:
            self._serve_loop(context, server, plan.deadline)
        finally:
            server.close()
            LOG.info("stage %s closed listener %s:%s", context.assignment_id,
                     address[0], address[1])

    def _serve_loop(self, context: ExecutionContext, server, deadline: float | None) -> None:
        from communication.errors import (ChannelClosed, ChannelTimeout, PeerError,
                                          ProtocolError, TransportError)
        from inference_engine.errors import ShardError, TaskCancelled

        failures = 0
        served = 0
        while True:
            if context.cancel.is_set():
                raise ExecutionCancelled(context.cancel_reason)
            remaining = None
            if self.complete_after is not None:
                remaining = max(self.complete_after - served, 0)
                if remaining == 0:
                    return
            try:
                results = server.serve_forever(max_requests=remaining,
                                               should_stop=context.cancel.is_set,
                                               poll_interval=self.serve_poll)
            except TaskCancelled:
                raise ExecutionCancelled(context.cancel_reason or "cancelled") from None
            except (ChannelClosed, ChannelTimeout, PeerError, ProtocolError, TransportError,
                    ShardError, OSError) as exc:
                if context.cancel.is_set():
                    raise ExecutionCancelled(context.cancel_reason or "cancelled") from None
                if deadline is not None and time.time() >= deadline:
                    context.request_cancel("deadline")
                    raise ExecutionCancelled("deadline") from None
                failures += 1
                LOG.warning("stage request failed (%s/%s): %s", failures,
                            MAX_CONSECUTIVE_FAILURES, exc)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    raise
                self.sleep(self.serve_poll)
                continue
            served += len(results)
            failures = 0
            for result in results:
                context.result_summary = {key:result[key] for key in ("output_shape","output_preview","local_seconds","parameter_bytes") if key in result}
                LOG.info("stage served request: %s", {key:value for key,value in result.items() if key != "output_preview"})


def _ensure_weights(model_config, weights_path: Path) -> Path:
    if weights_path.exists():
        return weights_path
    from inference_engine import generate_weights, save_weights

    weights_path.parent.mkdir(parents=True, exist_ok=True)
    save_weights(weights_path, generate_weights(model_config, seed=0))
    return weights_path


def build_stage_executor(config: AgentConfig) -> Callable[[], Executor]:
    try:
        from inference_engine import ModelConfig
        from inference_engine.config import tiny_config
    except ImportError as exc:
        raise RuntimeError(
            "executor 'stage' requires the inference-engine and communication modules; "
            "add the repository's inference-engine/ and communication/ directories to "
            "PYTHONPATH") from exc

    if config.model_config_path:
        model_config = ModelConfig.load(config.model_config_path)
    else:
        model_config = tiny_config()
    if config.weights_path:
        weights_path = Path(config.weights_path).expanduser()
    else:
        weights_path = _ensure_weights(model_config,
                                       Path(config.state_dir) / DEFAULT_WEIGHTS_NAME)
    complete_after = None if config.complete_after == 0 else config.complete_after
    return lambda: StageExecutor(model_config=model_config, weights_path=weights_path,
                                 endpoint=config.endpoint, complete_after=complete_after)
