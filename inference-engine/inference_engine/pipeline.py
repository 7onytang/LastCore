"""Two-or-more-node pipeline wiring.

Topology (one task at a time, matching the controller's single-task-per-node rule)::

    client --> stage 0 --> stage 1 --> ... --> stage N-1
                    |          |                   |
                    +---------- result ------------+

Every stage listens on the address it registered with the controller. A caller
sends token ids to the first stage; each stage executes only its own segment and
forwards the hidden state to the ``next_node`` endpoint the controller returned;
the last stage's logits travel back along the same chain, so no stage needs to know
more than its immediate downstream peer.
"""

from __future__ import annotations

import time
from typing import Callable

import numpy as np

from communication import (Channel, ChannelListener, ChannelTimeout, PeerError,
                           TaskCancelled)
from communication.errors import TransportError

from .errors import ShardError
from .stage import StageRuntime


class StageServer:
    def __init__(self, runtime: StageRuntime, *, address: tuple[str, int] | None = None,
                 downstream: tuple[str, int] | str | None = None, timeout: float | None = 30.0,
                 deadline: float | None = None, cancel=None, host: str = "127.0.0.1",
                 port: int = 0):
        if downstream is None and not runtime.is_last:
            raise ShardError(f"stage [{runtime.layer_start}, {runtime.layer_end}) is not the last "
                             "segment and needs a downstream peer")
        self.runtime = runtime
        self.downstream = downstream
        self.timeout = timeout
        self.deadline = deadline
        self.cancel = cancel
        self.listener = ChannelListener(host if address is None else address[0],
                                        port if address is None else address[1])
        self.processed = 0

    @property
    def address(self) -> tuple[str, int]:
        return self.listener.address

    def close(self) -> None:
        self.listener.close()

    def _hello(self) -> dict:
        return {
            "model_id": self.runtime.config.model_id,
            "num_layers": self.runtime.config.n_layers,
            "layer_start": self.runtime.layer_start,
            "layer_end": self.runtime.layer_end,
            "role": self.runtime.role,
            "task_id": self.runtime.task_id,
            "assignment_id": self.runtime.assignment_id,
        }

    def _validate_peer_hello(self, hello, *, expected_layer_start: int | None = None,
                             expected_layer_end: int | None = None,
                             peer_name: str) -> None:
        """Validate a node-to-node HELLO against this stage's contract."""
        required = ("model_id", "num_layers", "layer_start", "layer_end", "role",
                    "task_id", "assignment_id")
        missing = [field for field in required if field not in hello]
        if missing:
            raise ShardError(f"{peer_name} HELLO is missing required fields: "
                             f"{', '.join(missing)}")
        if hello["model_id"] != self.runtime.config.model_id:
            raise ShardError(f"{peer_name} runs model {hello['model_id']!r}, local stage runs "
                             f"{self.runtime.config.model_id!r}")
        if hello["num_layers"] != self.runtime.config.n_layers:
            raise ShardError(f"{peer_name} has {hello['num_layers']} layers, local model has "
                             f"{self.runtime.config.n_layers}")
        if expected_layer_start is not None and hello["layer_start"] != expected_layer_start:
            raise ShardError(f"{peer_name} segment starts at layer {hello['layer_start']}, "
                             f"expected {expected_layer_start}")
        if expected_layer_end is not None and hello["layer_end"] != expected_layer_end:
            raise ShardError(f"{peer_name} segment ends at layer {hello['layer_end']}, "
                             f"expected {expected_layer_end}")
        if (hello["layer_start"] < 0 or hello["layer_end"] <= hello["layer_start"] or
                hello["layer_end"] > hello["num_layers"]):
            raise ShardError(f"{peer_name} has invalid layer segment "
                             f"[{hello['layer_start']}, {hello['layer_end']})")
        local_task = self.runtime.task_id
        if local_task and hello["task_id"] != local_task:
            raise ShardError(f"{peer_name} task does not match this stage's task")

    def _validate_upstream(self, hello) -> None:
        # PipelineClient intentionally has a smaller HELLO surface. Node peers must use
        # the complete stage contract validated above.
        if hello.get("role") == "client":
            if "layer_end" not in hello:
                raise ShardError("client HELLO is missing required field: layer_end")
            if hello["layer_end"] != self.runtime.layer_start:
                raise ShardError(f"upstream segment ends at layer {hello['layer_end']}, local "
                                 f"segment starts at {self.runtime.layer_start}")
            peer_model = hello.get("model_id")
            if peer_model is not None and peer_model != self.runtime.config.model_id:
                raise ShardError(f"upstream runs model {peer_model!r}, local stage runs "
                                 f"{self.runtime.config.model_id!r}")
            local_task = self.runtime.task_id
            if local_task and hello.get("task_id") not in (None, local_task):
                raise ShardError("upstream task does not match this stage's task")
            return
        self._validate_peer_hello(hello, expected_layer_end=self.runtime.layer_start,
                                  peer_name="upstream")

    def _forward(self, hidden: np.ndarray) -> np.ndarray:
        link = Channel.connect(self.downstream, timeout=self.timeout, deadline=self.deadline,
                               cancel=self.cancel)
        try:
            link.send_hello(**self._hello())
            self._validate_peer_hello(link.recv_hello(),
                                      expected_layer_start=self.runtime.layer_end,
                                      peer_name="downstream")
            link.send_tensor("hidden", hidden, task_id=self.runtime.task_id,
                             assignment_id=self.runtime.assignment_id, seq=0)
            result, _ = link.recv_result()
            link.send_bye()
            return result
        finally:
            link.close()

    def handle(self, conn: Channel) -> dict:
        hello = conn.recv_hello()
        self._validate_upstream(hello)
        conn.send_hello(**self._hello())
        batch, _ = conn.recv_tensor()
        started = time.monotonic()
        output = self.runtime.execute(batch)
        local_seconds = time.monotonic() - started
        result = self._forward(output) if self.downstream is not None else output
        conn.send_result("logits", result, task_id=self.runtime.task_id,
                         assignment_id=self.runtime.assignment_id)
        conn.send_bye()
        self.processed += 1
        return {"layers": [self.runtime.layer_start, self.runtime.layer_end],
                "local_seconds": local_seconds,
                "output_shape": list(np.asarray(result).shape),
                "parameter_bytes": self.runtime.parameter_bytes}

    def serve_once(self) -> dict:
        conn = self.listener.accept(timeout=self.timeout, deadline=self.deadline, cancel=self.cancel,
                                    channel_timeout=self.timeout)
        return self._serve(conn)

    def _serve(self, conn: Channel) -> dict:
        try:
            return self.handle(conn)
        except TaskCancelled:
            try:
                conn.send_cancel("upstream cancelled the task")
            except TransportError:
                pass
            raise
        except PeerError as exc:
            try:
                conn.send_error(str(exc), code=getattr(exc, "code", "remote_error"))
            except TransportError:
                pass
            raise
        except Exception as exc:  # noqa: BLE001 - the peer must learn why the stage failed
            try:
                conn.send_error(str(exc), code=type(exc).__name__)
            except TransportError:
                pass
            raise
        finally:
            conn.close()

    def serve_forever(self, max_requests: int | None = None,
                      should_stop: Callable[[], bool] | None = None,
                      poll_interval: float = 0.25) -> list[dict]:
        """Serve requests until the count is reached, the caller stops, or a cancel arrives.

        Idle waiting is a short poll, so a stop request (``should_stop`` or a cancel event) takes
        effect within ``poll_interval``; ``timeout`` only bounds a request once a peer connected.
        An expired task ``deadline`` (epoch seconds, as the Controller and node-agent report it)
        raises ``ChannelTimeout`` instead of looping: the node must not keep polling work whose
        task has already been failed by the Controller.
        """
        results: list[dict] = []
        while max_requests is None or len(results) < max_requests:
            if should_stop is not None and should_stop():
                break
            if self.cancel is not None and self.cancel.is_set():
                break
            try:
                conn = self.listener.accept(timeout=poll_interval, cancel=self.cancel,
                                            channel_timeout=self.timeout, deadline=self.deadline)
            except ChannelTimeout:
                if self.deadline is not None and time.time() >= self.deadline:
                    raise
                continue
            except TaskCancelled:
                break
            results.append(self._serve(conn))
        return results

    def __enter__(self) -> "StageServer":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


class PipelineClient:
    def __init__(self, first_stage: tuple[str, int] | str, *, timeout: float | None = 30.0,
                 deadline: float | None = None, cancel=None, model_id: str | None = None,
                 task_id: str | None = None, assignment_id: str | None = None):
        self.first_stage = first_stage
        self.timeout = timeout
        self.deadline = deadline
        self.cancel = cancel
        self.model_id = model_id
        self.task_id = task_id
        self.assignment_id = assignment_id

    def run(self, tokens, *, task_id: str | None = None,
            assignment_id: str | None = None) -> np.ndarray:
        task_id = self.task_id if task_id is None else task_id
        assignment_id = self.assignment_id if assignment_id is None else assignment_id
        link = Channel.connect(self.first_stage, timeout=self.timeout, deadline=self.deadline,
                               cancel=self.cancel)
        try:
            link.send_hello(role="client", model_id=self.model_id, task_id=task_id,
                            assignment_id=assignment_id, layer_end=0)
            peer = link.recv_hello()
            if self.model_id and peer.get("model_id") not in (None, self.model_id):
                raise ShardError(f"first stage runs model {peer.get('model_id')!r}, expected "
                                 f"{self.model_id!r}")
            link.send_tensor("tokens", np.asarray(tokens, dtype=np.int64), task_id=task_id,
                             assignment_id=assignment_id, seq=0)
            result, _ = link.recv_result()
            link.send_bye()
            return result
        finally:
            link.close()
