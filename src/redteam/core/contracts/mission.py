"""Mission, Task Graph and operator-intent domain contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from redteam.core.enums import MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import (
    AgentId,
    EngagementId,
    GraphNodeId,
    HypothesisId,
    MissionId,
    OperatorIntentId,
    TaskId,
)

_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "cookie", "api_key")


def _reject_secret_material(value: Any, *, path: str = "metadata") -> Any:
    """Reject secret-shaped fields while allowing opaque ``*_ref`` values."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SECRET_KEY_PARTS) and not normalized.endswith(
                ("_ref", "_reference", "_id")
            ):
                raise ValueError(f"raw secret field is not permitted at {path}.{key}")
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")
    return value


class Provenance(BaseModel):
    source: str = Field(min_length=1)
    references: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source")
    @classmethod
    def _source_not_blank(cls, value: str) -> str:
        return value.strip()

    @field_validator("references")
    @classmethod
    def _stable_references(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})

    @field_validator("metadata")
    @classmethod
    def _metadata_has_no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_secret_material(value, path="provenance.metadata")


class MissionPlan(BaseModel):
    mission_id: MissionId
    engagement_id: EngagementId
    objective: str = Field(min_length=1)
    priorities: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    initial_workstreams: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    operator_constraints: dict[str, Any] = Field(default_factory=dict)
    operator_preferences: dict[str, Any] = Field(default_factory=dict)
    status: MissionStatus = MissionStatus.DRAFT
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime
    provenance: Provenance

    @field_validator("objective")
    @classmethod
    def _objective_not_blank(cls, value: str) -> str:
        return value.strip()

    @field_validator("priorities", "success_criteria", "initial_workstreams", "assumptions")
    @classmethod
    def _stable_text_lists(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("operator_constraints", "operator_preferences")
    @classmethod
    def _operator_data_has_no_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _reject_secret_material(value, path="operator_data")

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> MissionPlan:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class MissionTask(BaseModel):
    task_id: TaskId
    mission_id: MissionId
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    assigned_agent: AgentId | None = None
    required_capability: str | None = None
    parent_task_id: TaskId | None = None
    dependency_task_ids: list[TaskId] = Field(default_factory=list)
    blocking_reason: str | None = None
    related_attack_graph_node_ids: list[GraphNodeId] = Field(default_factory=list)
    related_hypothesis_ids: list[HypothesisId] = Field(default_factory=list)
    provenance: Provenance
    created_at: datetime
    updated_at: datetime

    @field_validator("title", "objective")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "dependency_task_ids", "related_attack_graph_node_ids", "related_hypothesis_ids"
    )
    @classmethod
    def _stable_ids(cls, value: list[Any]) -> list[Any]:
        return sorted(set(value), key=str)

    @model_validator(mode="after")
    def _task_invariants(self) -> MissionTask:
        if self.task_id in self.dependency_task_ids:
            raise ValueError("task cannot depend on itself")
        if self.status is TaskStatus.BLOCKED and not (self.blocking_reason or "").strip():
            raise ValueError("blocked task requires blocking_reason")
        if self.status is not TaskStatus.BLOCKED and self.blocking_reason is not None:
            raise ValueError("blocking_reason is only valid for blocked tasks")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class OperatorIntent(BaseModel):
    intent_id: OperatorIntentId
    mission_id: MissionId
    intent_type: OperatorIntentType
    instruction: str = Field(min_length=1)
    task_ids: list[TaskId] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    created_at: datetime

    @field_validator("instruction")
    @classmethod
    def _instruction_not_blank(cls, value: str) -> str:
        return value.strip()

    @field_validator("task_ids")
    @classmethod
    def _stable_task_ids(cls, value: list[TaskId]) -> list[TaskId]:
        return sorted(set(value), key=str)

    @field_validator("parameters")
    @classmethod
    def _parameters_are_non_executable_and_secret_free(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        forbidden = {"tool_id", "tool_invocation", "mcp_invocation", "command", "payload"}
        overlap = sorted({str(key).lower() for key in value} & forbidden)
        if overlap:
            raise ValueError(f"operator intent cannot contain executable fields: {overlap}")
        return _reject_secret_material(value, path="intent.parameters")
