"""Bind operator-scoped Analysis tools to trusted execution identity."""

from __future__ import annotations

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.core.contracts.tool import ToolInvocation


class ScopedAnalysisMCPAdapter(AnalysisMCPAdapter):
    error_prefix = "analysis"

    def bind_context(self, invocation: ToolInvocation) -> dict:
        context = invocation.execution_context
        if invocation.run_id != context.run_id:
            raise ValueError(f"{self.error_prefix}_context_mismatch")
        arguments = dict(invocation.arguments)
        for name, value in (
            ("engagement_id", str(context.engagement_id)),
            ("run_id", str(context.run_id)),
        ):
            if name in arguments and arguments[name] != value:
                raise ValueError(f"{self.error_prefix}_context_mismatch")
            arguments[name] = value
        return arguments


class VDBMCPAdapter(ScopedAnalysisMCPAdapter):
    error_prefix = "vdb"
