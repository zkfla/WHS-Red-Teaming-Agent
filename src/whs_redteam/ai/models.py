"""Small, strict schemas exchanged with an optional planning model."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanningCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    endpoint_template: str
    method: Literal["GET"] = "GET"
    parameter: str
    parameter_kind: Literal["string", "integer"]
    checks: tuple[Literal["sqli"], ...] = ("sqli",)


class PlanningContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "planning-context-v1"
    candidates: tuple[PlanningCandidate, ...]
    allowed_actions: tuple[Literal["run_sqli_safe", "skip_candidate", "finish"], ...] = (
        "run_sqli_safe",
        "skip_candidate",
        "finish",
    )
    remaining_requests: int = Field(ge=0)
    planning_round: int = Field(ge=0)
    prior_results: tuple[dict[str, object], ...] = ()


class PlannedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
    action: Literal["run_sqli_safe", "skip_candidate"]
    reason: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "planner-decision-v1"
    selected_actions: tuple[PlannedAction, ...] = Field(max_length=20)
    finish: bool = False
    finish_reason: str | None = Field(default=None, max_length=500)
