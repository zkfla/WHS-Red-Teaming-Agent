"""Run lifecycle limits selected by a trusted specialist profile."""

from pydantic import BaseModel, ConfigDict, Field, model_serializer


class ProbeLimits(BaseModel):
    """Task-local admission bounds, charged to the existing Run ledgers as well."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_actions: int = Field(default=4, ge=1, strict=True)
    max_model_requests: int = Field(default=8, ge=1, strict=True)


class SpecialistRunPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_instances: int = Field(ge=1, strict=True)
    max_activations: int = Field(ge=1, strict=True)
    max_tasks_per_strategy: int = Field(ge=1, strict=True)
    max_strategy_revisions: int = Field(ge=1, strict=True)
    probe_limits: ProbeLimits | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if self.probe_limits is None:
            result.pop("probe_limits", None)
        return result
