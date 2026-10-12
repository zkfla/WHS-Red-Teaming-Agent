"""Local, data-only job double; selected solely by central_development."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult
from redteam.core.ids import new_id
from redteam.orchestration.long_jobs import LongJobStatus


class DevelopmentJobs:
    def __init__(self, *, output: Any = None, error: ToolError | None = None):
        self.output = deepcopy(output) if output is not None else {}
        self.error = error.model_copy(deep=True) if error is not None else None
        self.submissions: dict[str, ToolInvocation] = {}
        self.states: dict[str, ExecutionState] = {}

    async def submit(self, invocation: ToolInvocation) -> str:
        identity = new_id("development_job")
        self.submissions[identity] = invocation.model_copy(deep=True)
        self.states[identity] = ExecutionState.RUNNING
        return identity

    async def status(self, job_id: str) -> LongJobStatus:
        return LongJobStatus(job_id=job_id, state=self.states[job_id])

    async def result(self, job_id: str) -> ToolResult:
        state = self.states[job_id]
        if state not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}:
            raise ValueError("development_job_not_complete")
        invocation, now = self.submissions[job_id], datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=state is ExecutionState.SUCCEEDED,
            output=self.output,
            error=self.error,
            metadata={"mock": True, "development_double": True},
            started_at=now,
            finished_at=now,
        )

    async def cancel(self, job_id: str) -> bool:
        if self.states[job_id] is ExecutionState.RUNNING:
            self.states[job_id] = ExecutionState.CANCELLED
            return True
        return False
