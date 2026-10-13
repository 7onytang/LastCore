from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class ExecutionCancelled(Exception):
    def __init__(self, reason: str = "cancelled"):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ExecutionContext:
    task_id: str
    assignment_id: str
    model_id: str
    position: int
    layer_start: int
    layer_end: int
    previous_node: dict | None
    next_node: dict | None
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str = ""
    raw: dict | None = None
    result_summary: dict | None = None

    def request_cancel(self, reason: str) -> None:
        self.cancel_reason = reason
        self.cancel.set()


class Executor:
    name = "executor"

    def run(self, context: ExecutionContext) -> None:
        raise NotImplementedError


class SliceDemoExecutor(Executor):
    name = "demo"

    def __init__(self, slices: int = 20, slice_seconds: float = 0.2,
                 sleep: Callable[[float], None] = time.sleep):
        self.slices = slices
        self.slice_seconds = slice_seconds
        self.sleep = sleep

    def run(self, context: ExecutionContext) -> None:
        for _ in range(self.slices):
            if context.cancel.is_set():
                raise ExecutionCancelled(context.cancel_reason)
            self.sleep(self.slice_seconds)
        if context.cancel.is_set():
            raise ExecutionCancelled(context.cancel_reason)


def load_executor(spec: str) -> Executor:
    if spec == "demo":
        return SliceDemoExecutor()
    if ":" in spec:
        module_name, _, attribute = spec.partition(":")
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
        executor = factory() if isinstance(factory, type) else factory()
        if not isinstance(executor, Executor):
            raise TypeError(f"{spec} did not produce an Executor")
        return executor
    raise ValueError(f"unknown executor: {spec}")
