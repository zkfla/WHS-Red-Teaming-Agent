"""Bounded observations carried from one Agent turn into the next.

The Attack Graph remains the source of truth for target knowledge and the
Evidence Store remains the source of truth for tool output.  This model fills
the smaller, previously missing seam: outcomes that influence the next
decision but are not target facts, most importantly Control Plane decisions.

Only compact, redacted summaries are checkpointed.  Raw tool output, prompts,
arguments and credentials never belong here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from redteam.core.redaction import redact_sensitive_text

MAX_RUNTIME_OBSERVATIONS = 12


class RuntimeObservation(BaseModel):
    """One policy/tool/state outcome visible to a later planning turn."""

    kind: Literal["control", "tool", "state", "operator"]
    summary: str = Field(min_length=1, max_length=500)
    outcome: str
    step: int = Field(ge=0)
    action_id: str | None = None
    capability: str | None = None
    operation: str | None = None
    target: str | None = None
    reasons: list[str] = Field(default_factory=list, max_length=8)
    matched_rules: list[str] = Field(default_factory=list, max_length=12)
    approval_id: str | None = None
    requires_approval: bool = False
    alternative_allowed: bool = True

    @field_validator(
        "summary",
        "outcome",
        "action_id",
        "capability",
        "operation",
        "target",
        "approval_id",
    )
    @classmethod
    def _redact_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return redact_sensitive_text(value)[:500]

    @field_validator("reasons", "matched_rules")
    @classmethod
    def _redact_list(cls, value: list[str]) -> list[str]:
        return [redact_sensitive_text(str(item))[:200] for item in value]


def append_runtime_observation(runtime, observation: RuntimeObservation):  # noqa: ANN001, ANN201
    """Return ``runtime`` with a stable, bounded recent-observation window."""

    recent = [*runtime.recent_observations, observation]
    return runtime.model_copy(update={"recent_observations": recent[-MAX_RUNTIME_OBSERVATIONS:]})


def observations_for_prompt(observations: list[RuntimeObservation]) -> list[dict[str, object]]:
    """Render the safe subset accepted by planner/generator prompt builders."""

    return [item.model_dump(mode="json", exclude_none=True) for item in observations]
