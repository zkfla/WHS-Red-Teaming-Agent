"""Run-scoped adapter for the proposed execution-substrate MCP."""

from __future__ import annotations

import re
from dataclasses import replace

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.adapters.mcp.scoped_analysis import ScopedAnalysisMCPAdapter
from redteam.core.contracts.tool import ToolDescriptor, ToolError

_EXECUTION_ERROR = re.compile(r"\b(execution_[a-z0-9_]+)\b")


class ExecutionMCPAdapter(ScopedAnalysisMCPAdapter):
    error_prefix = "execution"

    async def invoke(
        self,
        tool: ToolDescriptor,
        arguments: dict,
    ) -> MCPCallResult:
        result = await super().invoke(tool, arguments)
        if result.error is None:
            return result
        structured_code = (
            result.output.get("error_code") if isinstance(result.output, dict) else None
        )
        match = (
            _EXECUTION_ERROR.fullmatch(structured_code)
            if isinstance(structured_code, str)
            else _EXECUTION_ERROR.search(result.error.message)
        )
        if match is None:
            return result
        return replace(
            result,
            error=ToolError(
                code=match.group(0),
                message=result.error.message,
                retryable=False,
            ),
        )
