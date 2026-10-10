"""Run lifecycle limits selected by a trusted specialist profile."""

from pydantic import BaseModel, ConfigDict, Field


class SpecialistRunPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_instances: int = Field(ge=1, strict=True)
    max_activations: int = Field(ge=1, strict=True)
    max_tasks_per_strategy: int = Field(ge=1, strict=True)
    max_strategy_revisions: int = Field(ge=1, strict=True)
