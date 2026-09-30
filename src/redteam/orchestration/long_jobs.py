"""Long-running job lifecycle (submit/status/result/cancel).

Integration contract (root-owned wiring, agent-owned protocol):

- The default tool execution path is synchronous: one approved call runs
  to completion through the execute node's invoke helper and the caller
  waits for it.
- Async linkage turns on only when the root engine supplies a real
  adapter at the assembly point (RuntimeDeps.long_job_adapter,
  root-owned). With an adapter present the execute node polls status
  with lease heartbeat instead of the plain invoke helper; without one
  it stays on the plain invoke path.
- Submission belongs to the root engine, never the planner and never
  this manager on its own: the manager only accepts an approved
  ToolInvocation (built by build_tool_invocation from an ALLOW
  policy decision) and delegates it to the bound adapter, so async
  execution cannot bypass the policy gate.
- job_id persistence is root-owned (work ledger WorkItem.job_id
  plus update_job). This module creates no store; the manager only
  delegates, and submit must return a backend-side persistent id.
- No fakes: without an adapter the manager raises
  LongJobNotSupportedError (try_status returns None) and the
  root falls back to synchronous invoke. status must query the
  backend, result must fetch the finished normalized ToolResult,
  and neither may re-execute a side-effecting call. When an outcome is
  unclear, check the execution record and tool status first; never
  repeat the same call under a new name.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field
from redteam.core.contracts.tool import ToolInvocation, ToolResult


class LongJobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class LongJobNotSupportedError(RuntimeError):
    """Raised when long-job operations are used with no real adapter."""


class LongJobStatus(BaseModel):
    job_id: str
    state: LongJobState = LongJobState.UNKNOWN
    message: str | None = None


class LongJobResult(BaseModel):
    job_id: str
    result: ToolResult | None = None
    error: str | None = None

    def require_result(self) -> ToolResult:
        if self.result is None:
            detail = ""
            if self.error:
                detail = ": " + self.error
            raise ValueError("long job '" + self.job_id + "' has no result yet" + detail)
        return self.result


class LongJobAdapter(Protocol):
    """Real async backend supplied at the assembly point. No fakes.

    Every method must talk to the backend. submit takes an approved
    invocation (policy already passed) and returns a persistent backend
    job id; status reads live backend state; result fetches the finished
    normalized ToolResult without re-running side effects; cancel asks
    the backend to stop the job.
    """

    async def submit(self, invocation: ToolInvocation) -> str:
        """Start the approved invocation; return its persistent job id."""
        ...

    async def status(self, job_id: str) -> LongJobStatus:
        """Read backend status. Must query the backend, never invent it."""
        ...

    async def result(self, job_id: str) -> ToolResult:
        """Fetch the finished normalized result. Must not re-run side effects."""
        ...

    async def cancel(self, job_id: str) -> bool:
        """Cancel and return True when the backend accepted it."""
        ...


class LongJobManager:
    """Delegates to the bound adapter; never submits on its own.

    The manager builds no invocations and keeps no ledger: the root
    engine submits approved invocations and persists job ids in the
    work ledger. With no adapter every operation except try_status
    raises LongJobNotSupportedError so the root takes the plain
    synchronous invoke path.
    """

    def __init__(self, adapter: LongJobAdapter | None = None) -> None:
        self._adapter = adapter

    @property
    def is_enabled(self) -> bool:
        return self._adapter is not None

    @property
    def adapter(self) -> LongJobAdapter | None:
        return self._adapter

    def bind(self, adapter: LongJobAdapter) -> None:
        self._adapter = adapter

    def unbind(self) -> None:
        self._adapter = None

    def _require(self) -> LongJobAdapter:
        if self._adapter is None:
            raise LongJobNotSupportedError(
                "long jobs are not available: no adapter was supplied at the assembly point; "
                "use the default synchronous call path instead"
            )
        return self._adapter

    async def submit(self, invocation: ToolInvocation) -> str:
        """Delegate an approved invocation; return the persistent job id."""
        return await self._require().submit(invocation)

    async def status(self, job_id: str) -> LongJobStatus:
        return await self._require().status(job_id)

    async def result(self, job_id: str) -> LongJobResult:
        """Fetch the finished result wrapped with its job id."""
        backend_result = await self._require().result(job_id)
        return LongJobResult(job_id=job_id, result=backend_result)

    async def cancel(self, job_id: str) -> bool:
        return await self._require().cancel(job_id)

    async def try_status(self, job_id: str) -> LongJobStatus | None:
        """Backend status, or None when long jobs are disabled."""
        if self._adapter is None:
            return None
        return await self._adapter.status(job_id)


__all__ = [
    "LongJobAdapter",
    "LongJobManager",
    "LongJobNotSupportedError",
    "LongJobResult",
    "LongJobState",
    "LongJobStatus",
]
