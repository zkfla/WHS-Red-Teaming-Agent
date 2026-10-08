"""Investigation ownership persisted across the existing per-action graph loop."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from redteam.core.ids import AgentInstanceId
from redteam.orchestration.work.models import WorkBudget


class DelegationRequest(BaseModel):
    """A root proposal; execution identity and default limits belong to the runtime."""

    path_step_id: str
    agent_id: str
    instance_id: AgentInstanceId | None = None
    objective: str | None = None
    budget: WorkBudget | None = None


class DelegationState(BaseModel):
    """Operational context, never observed knowledge or a mission-completion claim."""

    delegation_id: str
    agent_id: str
    objective: str
    path_id: str
    path_version: int
    path_step_id: str
    mission_task_id: str | None = None
    budget: WorkBudget
    decision_limit: int = Field(ge=1)
    started_at: datetime
    actions_used: int = Field(default=0, ge=0)
    decision_attempts_used: int = Field(default=0, ge=0)
    tokens_used: int | None = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["active", "finished", "held"] = "active"
    termination_reason: str | None = None
    summary: str | None = None
    unresolved: list[str] = Field(default_factory=list)
    held_fingerprint: str | None = None
