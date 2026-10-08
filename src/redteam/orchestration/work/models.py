"""Agent-Team internal work contracts; domain state remains in canonical stores."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from redteam.core.ids import AgentId, AgentInstanceId, GraphNodeId, HypothesisId, RunId
from redteam.orchestration.candidates.models import PriorityScore


class WorkStatus(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    STALE = "stale"


TERMINAL_STATUSES = frozenset(
    {
        WorkStatus.SUCCEEDED,
        WorkStatus.FAILED,
        WorkStatus.BLOCKED,
        WorkStatus.CANCELLED,
        WorkStatus.STALE,
    }
)
ACTIVE_STATUSES = frozenset({WorkStatus.PENDING, WorkStatus.LEASED, WorkStatus.RUNNING})


def work_owner_for(run_id: RunId | str, instance_id: AgentInstanceId | None) -> str:
    """One lease owner per instance, preserving the old run owner for legacy work."""
    return f"{run_id}:instance:{instance_id}" if instance_id is not None else str(run_id)


class WorkBudget(BaseModel):
    max_actions: int = Field(default=1, ge=1)
    max_seconds: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, ge=1)


class WorkItem(BaseModel):
    work_id: str
    #: None exists only for deserializing pre-run-isolation ledger rows.
    run_id: RunId | None = None
    candidate_key: str
    #: Legacy rows used bounded summaries and cannot prove original-state equality.
    execution_identity_version: int = Field(default=0, ge=0)
    agent_id: AgentId
    instance_id: AgentInstanceId | None = None
    graph_revision: int = Field(default=0, ge=0)
    path_id: str | None = None
    path_version: int | None = None
    path_step_id: str | None = None
    mission_task_id: str | None = None
    model_tool_call_id: str = ""
    task_purpose_hash: str = ""
    execution_origin_version: int = 0
    delegation_id: str | None = None
    job_id: str | None = None
    target_node_ids: list[GraphNodeId]
    hypothesis_id: HypothesisId | None = None
    prerequisite_ids: list[str] = Field(default_factory=list)
    conflict_keys: list[str] = Field(default_factory=list)
    priority: PriorityScore
    budget: WorkBudget = Field(default_factory=WorkBudget)
    repeatable: bool = False
    status: WorkStatus = WorkStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    outcome_reason: str | None = None

    @field_validator("target_node_ids")
    @classmethod
    def _target_ids_are_stable(cls, value: list[GraphNodeId]) -> list[GraphNodeId]:
        return sorted(set(value), key=str)

    @model_validator(mode="after")
    def _empty_refs_need_native_lineage(self) -> WorkItem:
        # Empty refs alone prove no targeted absence: they only record
        # native lineage (execution_origin_version == 1). Whether the
        # execution genuinely needed no target is decided by the
        # provider-declared descriptor at compile/admission time and by
        # the explicit stored proposal at completion time -- never by a
        # duplicated requires_target flag here.
        if not self.target_node_ids and self.execution_origin_version != 1:
            raise ValueError("work requires at least one target reference")
        return self

    @field_validator("conflict_keys", "prerequisite_ids")
    @classmethod
    def _keys_are_stable(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})


class TaskLease(BaseModel):
    work_id: str
    owner: str
    issued_at: datetime
    expires_at: datetime
    heartbeat_at: datetime


def is_unexecuted_stale(item: WorkItem) -> bool:
    if item.status is not WorkStatus.STALE:
        return False
    if item.job_id:
        return False
    return item.attempt == 0 or item.outcome_reason == "invalidated_before_execution"
