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

    def run(self, context: ExecutionContext):
        raise NotImplementedError


class SliceDemoExecutor(Executor):
    name = "demo"
    supported_workloads = ("pi", "log_stats")

    def __init__(self, slices: int = 20, slice_seconds: float = 0.2,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, random_unit=None):
        self.slices = slices
        self.slice_seconds = slice_seconds
        self.sleep = sleep
        self.clock = clock
        self.random_unit = random_unit or _random_unit

    def _check(self, context):
        if context.cancel.is_set():
            raise ExecutionCancelled(context.cancel_reason or "cancelled")
        if self.clock() >= context.deadline:
            raise ExecutionCancelled("deadline")

    def run(self, context: ExecutionContext):
        self._check(context)
        for _ in range(self.slices):
            self._check(context)
            self.sleep(self.slice_seconds)
        self._check(context)
        request = context.raw.get("request", {}) if context.raw else {}
        workload = request.get("workload", "linear")
        if workload == "pi":
            seed, total, chunks = request.get("weight_seed"), request.get("total_samples"), request.get("num_layers")
            if (context.model_id != "lastcore/pi-monte-carlo-v1" or type(seed) is not int or not 0 <= seed <= 2_147_483_647
                    or type(total) is not int or not 1_000 <= total <= 100_000_000
                    or type(chunks) is not int or not 0 < chunks <= min(total, 100_000)
                    or not 0 <= context.layer_start < context.layer_end <= chunks):
                raise ValueError("invalid bounded pi assignment")
            first, last = total * context.layer_start // chunks, total * context.layer_end // chunks
            inside = 0
            for index in range(first, last):
                if (index - first) % 256 == 0:
                    self._check(context)
                x, y = self.random_unit(seed, index, 0), self.random_unit(seed, index, 1)
                inside += x * x + y * y <= 1.0
            self._check(context)
            return {"inside": inside, "samples": last - first}
        if workload == "log_stats":
            lines = request.get("log_lines")
            if (context.model_id != "lastcore/log-statistics-v1" or not isinstance(lines, list)
                    or not 1 <= len(lines) <= 10_000 or request.get("num_layers") != len(lines)
                    or not 0 <= context.layer_start < context.layer_end <= len(lines)
                    or any(not isinstance(line, str) or len(line) > 4096 for line in lines)
                    or sum(map(len, lines)) > 1_000_000):
                raise ValueError("invalid bounded log assignment")
            counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "OTHER": 0}
            for index in range(context.layer_start, context.layer_end):
                self._check(context)
                upper = lines[index].upper()
                level = next((item for item in ("ERROR", "WARN", "INFO") if item in upper), "OTHER")
                counts[level] += 1
            self._check(context)
            return counts
        if context.model_id in ("lastcore/pi-monte-carlo-v1", "lastcore/log-statistics-v1"):
            raise ValueError("model_id and workload disagree")
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
