"""Run-scoped adapter for the proposed execution-substrate MCP."""

from __future__ import annotations

from redteam.capability.adapters.mcp.scoped_analysis import ScopedAnalysisMCPAdapter


class ExecutionMCPAdapter(ScopedAnalysisMCPAdapter):
    error_prefix = "execution"
