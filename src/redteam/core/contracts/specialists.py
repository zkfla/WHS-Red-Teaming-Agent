"""Run lifecycle limits selected by a trusted specialist profile."""

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


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
    probe_atomization: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if self.probe_limits is None:
            result.pop("probe_limits", None)
        if self.probe_atomization is False:
            result.pop("probe_atomization", None)
        return result

    @model_validator(mode="after")
    def _require_limits_for_atomization(self):
        if self.probe_atomization and self.probe_limits is None:
            raise ValueError("probe_atomization_requires_probe_limits")
        return self
