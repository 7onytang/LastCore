from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


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


class Heartbeat(StrictModel):
    available_memory_mb: int = Field(ge=0, le=10_000_000)
    accepting_tasks: bool = True


class TaskRequest(StrictModel):
    model_id: str = Field(min_length=1, max_length=256)
    num_layers: int = Field(gt=0, le=100_000)
    layer_memory_mb: int = Field(gt=0, le=10_000_000)
    max_nodes: int = Field(default=8, gt=0, le=128)
    timeout_seconds: int = Field(default=600, ge=10, le=86400)
    input_vector: list[LinearValue] | None = Field(default=None, min_length=1, max_length=256)
    weight_seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    target_node_id: UUID | None = None

    @model_validator(mode="after")
    def linear_contract(self):
        if self.model_id == "lastcore/linear-demo-v1":
            if self.input_vector is None or self.weight_seed is None or self.target_node_id is None:
                raise ValueError("linear-demo-v1 requires input_vector, weight_seed and target_node_id")
            if self.num_layers > 4096 or self.max_nodes != 1:
                raise ValueError("targeted linear-demo-v1 supports at most 4096 rows on exactly one node")
        elif any(value is not None for value in (self.input_vector, self.weight_seed, self.target_node_id)):
            raise ValueError("linear input and target fields are only supported by linear-demo-v1")
        return self


class AssignmentReport(StrictModel):
    status: Literal["running", "completed", "failed"]
    error: str | None = Field(default=None, max_length=2048)
    result: list[LinearResult] | None = Field(default=None, min_length=1, max_length=4096)
