"""Provider-independent execution references and read-only recovery answers.

A reference correlates an approved invocation with provider-owned state.
It never authorizes execution. Only its provider interprets the opaque
handle. Completion describes execution, not an observation or achieved goal.
State describes the referenced invocation's operation. A durable job-start
acknowledgment may be complete while the separately registered job is still
running; its original acknowledgment is not the job's eventual raw result.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.core.contracts.tool import ToolError
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)


class ExecutionState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engagement_id: EngagementId
    run_id: RunId
    instance_id: AgentInstanceId | None = None
    action_id: ActionId
    invocation_id: InvocationId
    tool_id: ToolId
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_handle: dict[str, Any]

    @model_validator(mode="after")
    def _bounded_json_handle(self) -> ExecutionReference:
        try:
            serialized = json.dumps(self.provider_handle, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("execution handle must be JSON data") from exc
        if not self.provider_handle or len(serialized) > 16384:
            raise ValueError("execution handle must be nonempty and bounded")
        return self


class ExecutionRecoveryAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ExecutionState
    verified: bool = False
    uncertain: bool = True
    result_available: bool = False
    error: ToolError | None = None
