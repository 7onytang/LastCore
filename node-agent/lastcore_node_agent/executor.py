from __future__ import annotations

import importlib
import math
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

    def run(self, context: ExecutionContext):
        raise NotImplementedError


class SliceDemoExecutor(Executor):
    name = "demo"

    def __init__(self, slices: int = 20, slice_seconds: float = 0.2,
                 sleep: Callable[[float], None] = time.sleep):
        self.slices = slices
        self.slice_seconds = slice_seconds
        self.sleep = sleep

    def run(self, context: ExecutionContext):
        for _ in range(self.slices):
            if context.cancel.is_set():
                raise ExecutionCancelled(context.cancel_reason)
            self.sleep(self.slice_seconds)
        if context.cancel.is_set():
            raise ExecutionCancelled(context.cancel_reason)
        request = context.raw.get("request", {}) if context.raw else {}
        workload = request.get("workload", "linear")
        if workload == "pi" or context.model_id == "lastcore/pi-monte-carlo-v1":
            seed = int(request["weight_seed"])
            total = int(request["total_samples"])
            chunks = int(request["num_layers"])
            first = total * context.layer_start // chunks
            last = total * context.layer_end // chunks
            inside = 0
            for index in range(first, last):
                x = _random_unit(seed, index, 0)
                y = _random_unit(seed, index, 1)
                inside += x * x + y * y <= 1.0
            return {"inside": inside, "samples": last - first}
        if workload == "log_stats" or context.model_id == "lastcore/log-statistics-v1":
            counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
            for line in request["log_lines"][context.layer_start:context.layer_end]:
                upper = line.upper()
                level = next((item for item in ("ERROR", "WARN", "INFO") if item in upper), "OTHER")
                counts[level] += 1
            return counts
        return None


def _random_unit(seed: int, index: int, stream: int) -> float:
    value = (seed + index * 0x9E3779B97F4A7C15 + stream * 0xD1B54A32D192ED03) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 31
    return value / 0x10000000000000000


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
