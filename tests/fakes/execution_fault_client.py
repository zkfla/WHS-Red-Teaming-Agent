"""Fault-injection client used by the opt-in execution Docker test."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.execution_manifest import EXECUTION_TOOLS
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId


async def run(marker: Path) -> None:
    run_id = RunId("run-test")
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv-fault-job-start"),
        action_id=ActionId("action-fault-job-start"),
        run_id=run_id,
        tool_id=ToolId("analysis.job_start"),
        operation=EXECUTION_TOOLS["job_start"].operation,
        arguments={
            "asset_ref": "workspace-test",
            "executable": "tail",
            "args": ["-f", "input.txt"],
            "timeout_seconds": 20,
        },
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"),
            run_id=run_id,
        ),
        created_at=datetime.now(timezone.utc),
    )
    async with open_mcp_tool_gateway(
        ["execution"],
        health_refresh_enabled=False,
    ) as gateway:
        result = await gateway.invoke(invocation)
        if not result.success:
            raise RuntimeError(f"fault client job start failed: {result.error}")
        marker.write_text(result.output["job_id"], encoding="utf-8")
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("marker", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.marker))


if __name__ == "__main__":
    main()
