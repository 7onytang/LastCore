from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

LinearValue = Annotated[float, Field(allow_inf_nan=False, ge=-1_000_000, le=1_000_000)]
LinearResult = Annotated[float, Field(allow_inf_nan=False, ge=-1, le=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NodeRegistration(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)
    endpoint: HttpUrl
    available_memory_mb: int = Field(gt=0, le=10_000_000)
    compute_weight: float = Field(gt=0, le=1_000_000)
    accepting_tasks: bool = True
    supported_workloads: list[Literal["pi", "log_stats"]] = Field(default_factory=list, max_length=2)


class Heartbeat(StrictModel):
    available_memory_mb: int = Field(ge=0, le=10_000_000)
    accepting_tasks: bool = True
    supported_workloads: list[Literal["pi", "log_stats"]] = Field(default_factory=list, max_length=2)


class TaskRequest(StrictModel):
    model_id: str = Field(min_length=1, max_length=256)
    workload: Literal["linear", "pi", "log_stats"] = "linear"
    num_layers: int = Field(gt=0, le=100_000)
    layer_memory_mb: int = Field(gt=0, le=10_000_000)
    max_nodes: int = Field(default=8, gt=0, le=128)
    timeout_seconds: int = Field(default=600, ge=10, le=86400)
    input_vector: list[LinearValue] | None = Field(default=None, min_length=1, max_length=256)
    weight_seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    target_node_id: UUID | None = None
    total_samples: int | None = Field(default=None, strict=True, ge=1_000, le=100_000_000)
    log_lines: list[str] | None = Field(default=None, min_length=1, max_length=10_000)

    @field_validator("log_lines")
    @classmethod
    def validate_log_payload(cls, lines):
        if lines is not None and (any(len(line) > 4096 for line in lines)
                                  or sum(map(len, lines)) > 1_000_000):
            raise ValueError("log_lines are limited to 4096 characters each and 1,000,000 total characters")
        return lines

    @model_validator(mode="before")
    @classmethod
    def typed_integer_contract(cls, data):
        if isinstance(data, dict) and data.get("workload") in ("pi", "log_stats"):
            for name in ("num_layers", "layer_memory_mb", "max_nodes", "timeout_seconds", "weight_seed", "total_samples"):
                if name in data and data[name] is not None and type(data[name]) is not int:
                    raise ValueError("typed workload integer fields must not be bool, float or string")
        return data

    def protocol_dump_json(self):
        # Frozen legacy request dictionaries also bind published edge cache hashes.
        exclude = {"workload", "total_samples", "log_lines"} if self.workload == "linear" else set()
        return self.model_dump_json(exclude=exclude)

    @model_validator(mode="after")
    def validate_demo_size(self):
        known = {"lastcore/linear-demo-v1": "linear", "lastcore/pi-monte-carlo-v1": "pi",
                 "lastcore/log-statistics-v1": "log_stats"}
        if self.workload in ("pi", "log_stats") and known.get(self.model_id) != self.workload:
            raise ValueError("model_id must match the requested workload")
        if self.model_id in known and known[self.model_id] != self.workload:
            raise ValueError("model_id and workload disagree")
        if self.workload == "pi":
            if self.total_samples is None or self.weight_seed is None:
                raise ValueError("pi requires total_samples and weight_seed")
            if self.num_layers > self.total_samples:
                raise ValueError("pi partitions cannot exceed the number of samples")
            if any(value is not None for value in (self.input_vector, self.log_lines, self.target_node_id)):
                raise ValueError("pi does not accept linear, log or target fields")
        elif self.workload == "log_stats":
            if self.log_lines is None or self.num_layers != len(self.log_lines):
                raise ValueError("log_stats requires log_lines and matching num_layers")
            if any(value is not None for value in (self.input_vector, self.weight_seed, self.total_samples, self.target_node_id)):
                raise ValueError("log_stats does not accept linear, seed, sample or target fields")
        else:
            if self.total_samples is not None or self.log_lines is not None:
                raise ValueError("pi/log fields require their explicit workload")
            if self.model_id == "lastcore/linear-demo-v1":
                if self.input_vector is None or self.weight_seed is None or self.target_node_id is None:
                    raise ValueError("linear-demo-v1 requires input_vector, weight_seed and target_node_id")
                if self.num_layers > 4096 or self.max_nodes != 1:
                    raise ValueError("targeted linear-demo-v1 supports at most 4096 rows on exactly one node")
            elif any(value is not None for value in (self.input_vector, self.weight_seed, self.target_node_id)):
                raise ValueError("linear fields require linear-demo-v1")
        return self


class AssignmentReport(StrictModel):
    status: Literal["running", "completed", "failed"]
    error: str | None = Field(default=None, max_length=2048)
    result: list[LinearResult] | None = Field(default=None, min_length=1, max_length=4096)
    workload_result: dict[str, Annotated[int, Field(strict=True, ge=0, le=100_000_000)]] | None = Field(default=None, max_length=4)
