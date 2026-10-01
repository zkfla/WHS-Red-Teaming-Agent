"""Run-scoped workspace adapter; trusted context is never selected by Agent."""

from __future__ import annotations

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.core.contracts.tool import ToolInvocation


class WorkspaceMCPAdapter(AnalysisMCPAdapter):
    def bind_context(self, invocation: ToolInvocation) -> dict:
        context = invocation.execution_context
        if invocation.run_id != context.run_id:
            raise ValueError("workspace_context_mismatch")
        arguments = dict(invocation.arguments)
        for name, value in (
            ("engagement_id", str(context.engagement_id)),
            ("run_id", str(context.run_id)),
        ):
            if name in arguments and arguments[name] != value:
                raise ValueError("workspace_context_mismatch")
            arguments[name] = value
        return arguments
