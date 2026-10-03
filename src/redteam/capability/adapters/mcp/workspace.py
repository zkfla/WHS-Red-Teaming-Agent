"""Run-scoped workspace adapter; trusted context is never selected by Agent."""

from __future__ import annotations

from redteam.capability.adapters.mcp.scoped_analysis import ScopedAnalysisMCPAdapter


class WorkspaceMCPAdapter(ScopedAnalysisMCPAdapter):
    error_prefix = "workspace"
