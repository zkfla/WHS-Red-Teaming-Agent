"""Run lifecycle limits selected by a trusted specialist profile."""

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class ProbeLimits(BaseModel):
    """Task-local admission bounds, charged to the existing Run ledgers as well."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_actions: int = Field(default=4, ge=1, strict=True)
    max_model_requests: int = Field(default=8, ge=1, strict=True)
    # Optional so Tasks persisted before the worker-circuit contract retain
    # their original purpose hash. New profiles bind both limits explicitly.
    max_repair_requests: int | None = Field(default=None, ge=0, strict=True)
    max_consecutive_provider_failures: int | None = Field(default=None, ge=1, strict=True)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        for name in ("max_repair_requests", "max_consecutive_provider_failures"):
            if result[name] is None:
                result.pop(name)
        return result

    @model_validator(mode="after")
    def _bounded_sub_limits(self):
        if (
            self.max_repair_requests is not None
            and self.max_repair_requests > self.max_model_requests
        ) or (
            self.max_consecutive_provider_failures is not None
            and self.max_consecutive_provider_failures > self.max_model_requests
        ):
            raise ValueError("probe_request_sub_limit_exceeds_total")
        return self


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
