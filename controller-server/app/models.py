from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NodeRegistration(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)
    endpoint: HttpUrl
    available_memory_mb: int = Field(gt=0, le=10_000_000)
    compute_weight: float = Field(gt=0, le=1_000_000)
    accepting_tasks: bool = True


class Heartbeat(StrictModel):
    available_memory_mb: int = Field(ge=0, le=10_000_000)
    accepting_tasks: bool = True


class TaskRequest(StrictModel):
    model_id: str = Field(min_length=1, max_length=256)
    num_layers: int = Field(gt=0, le=100_000)
    layer_memory_mb: int = Field(gt=0, le=10_000_000)
    max_nodes: int = Field(default=8, gt=0, le=128)
    timeout_seconds: int = Field(default=600, ge=10, le=86400)


class AssignmentReport(StrictModel):
    status: Literal["running", "completed", "failed"]
    error: str | None = Field(default=None, max_length=2048)
