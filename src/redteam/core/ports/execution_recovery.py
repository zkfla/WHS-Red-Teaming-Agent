"""Optional pure-reference/read-only-recovery contract for actual providers.

Preparing a reference does not create a process, environment or external
effect. Query/fetch only collect an existing execution. They must never
resubmit a missing or uncertain execution. Unverified terminal states or
missing originals cannot become successful observations through this port.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redteam.core.contracts.execution import ExecutionRecoveryAnswer, ExecutionReference
from redteam.core.contracts.tool import ToolInvocation, ToolResult


@runtime_checkable
class ExecutionRecoveryProvider(Protocol):
    def prepare_reference(self, invocation: ToolInvocation) -> ExecutionReference | None:
        """Pure derivation from the same bound/validated invocation supplied
        to contextual execution. None means this operation has no durable
        provider recovery handle; ordinary invocation remains available.
        """
        ...

    async def query_execution(self, reference: ExecutionReference) -> ExecutionRecoveryAnswer:
        """Read existing provider state using the trusted stored reference."""
        ...

    async def fetch_execution(self, reference: ExecutionReference) -> ToolResult | None:
        """Return the preserved original only after correlation and integrity
        checks. Missing/unverified originals return None, never a rerun.
        """
        ...
