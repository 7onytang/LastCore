"""Adapter from the Controller's assignment payload to local execution.

The controller (`controller-server`) returns each node an assignment shaped like::

    {
      "id": "<assignment uuid>", "task_id": "<task uuid>", "node_id": "<node uuid>",
      "position": 0, "layer_start": 0, "layer_end": 2, "status": "pending", "error": null,
      "previous_node": null,
      "next_node": {"node_id": "<uuid>", "endpoint": "http://10.0.0.12:9000", "id": "<uuid>"},
      "task_status": "queued", "request": {"model_id": "...", "num_layers": 4, ...},
      "deadline": 1700000000.0
    }

:func:`assignment_plan` turns that into the values this engine needs: the layer
segment, the downstream/upstream peer addresses (``schema://host:port`` URLs are
reduced to ``(host, port)``) and the absolute task deadline. Layer ranges keep the
controller's left-closed, right-open, zero-based convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from .errors import ShardError
from .stage import StageRuntime, load_stage


@dataclass(frozen=True)
class AssignmentPlan:
    task_id: str
    assignment_id: str
    layer_start: int
    layer_end: int
    num_layers: int
    model_id: str | None = None
    deadline: float | None = None
    next_endpoint: tuple[str, int] | None = None
    previous_endpoint: tuple[str, int] | None = None

    @property
    def is_first(self) -> bool:
        return self.layer_start == 0

    @property
    def is_last(self) -> bool:
        return self.layer_end == self.num_layers


def parse_endpoint(value: str) -> tuple[str, int]:
    """Parse the peer address forms the Controller and node-agent exchange.

    Accepts ``host:port`` and ``scheme://host:port`` 鈥?the Controller serialises
    pydantic ``HttpUrl`` with a trailing slash (``http://10.0.0.12:9000/``) 鈥?and
    rejects anything without a usable host and a numeric port.

    IPv6 literals are rejected on purpose: ``communication.ChannelListener`` binds
    ``AF_INET`` only, so accepting ``http://[::1]:9000/`` here would only move the
    failure to a bind error inside ``StageExecutor``. Rejecting at parse time keeps
    the parser honest about the transport that actually exists.
    """
    text = (value or "").strip()
    if not text:
        raise ShardError("assignment endpoint is empty")
    try:
        parsed = urlparse(text if "://" in text else f"//{text}")
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:  # malformed authority / non-numeric port
        raise ShardError(f"assignment endpoint {value!r} is not a host:port URL") from exc
    if not host or port is None:
        raise ShardError(f"assignment endpoint {value!r} is not a host:port URL")
    if ":" in host:
        raise ShardError(f"assignment endpoint {value!r} uses an IPv6 address, but the "
                         f"transport is IPv4-only (AF_INET)")
    return host, int(port)


def assignment_plan(assignment: Mapping, *, model_id: str | None = None,
                    num_layers: int | None = None) -> AssignmentPlan:
    try:
        task_id = str(assignment["task_id"])
        assignment_id = str(assignment["id"])
        layer_start = int(assignment["layer_start"])
        layer_end = int(assignment["layer_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ShardError(f"assignment is missing required fields: {exc}") from exc
    if layer_start < 0 or layer_end <= layer_start:
        raise ShardError(f"invalid layer segment [{layer_start}, {layer_end})")

    request = assignment.get("request") or {}
    request_model = request.get("model_id") if isinstance(request, Mapping) else None
    resolved_model = model_id or request_model
    resolved_layers = num_layers or (request.get("num_layers") if isinstance(request, Mapping) else None)
    if resolved_layers is not None and layer_end > int(resolved_layers):
        raise ShardError(f"segment ends at {layer_end} but the task has {resolved_layers} layers")

    def peer(name: str, field: str) -> tuple[str, int] | None:
        value = assignment.get(name)
        if not value:
            return None
        if not isinstance(value, Mapping) or field not in value:
            raise ShardError(f"{name} must be an object with an {field}")
        return parse_endpoint(str(value[field]))

    return AssignmentPlan(
        task_id=task_id, assignment_id=assignment_id, layer_start=layer_start, layer_end=layer_end,
        num_layers=int(resolved_layers) if resolved_layers is not None else layer_end,
        model_id=resolved_model,
        deadline=float(assignment["deadline"]) if assignment.get("deadline") is not None else None,
        next_endpoint=peer("next_node", "endpoint"),
        previous_endpoint=peer("previous_node", "endpoint"),
    )


def load_assigned_stage(assignment: Mapping, config, weights_path, *,
                        model_id: str | None = None) -> tuple[StageRuntime, AssignmentPlan]:
    plan = assignment_plan(assignment, model_id=model_id, num_layers=config.n_layers)
    if plan.model_id and model_id and plan.model_id != model_id:
        raise ShardError(f"assignment targets model {plan.model_id!r}, local model is {model_id!r}")
    if plan.model_id and not model_id and plan.model_id != config.model_id:
        raise ShardError(f"assignment targets model {plan.model_id!r}, local config is "
                         f"{config.model_id!r}")
    runtime = load_stage(config, weights_path, plan.layer_start, plan.layer_end,
                         task_id=plan.task_id, assignment_id=plan.assignment_id)
    return runtime, plan
