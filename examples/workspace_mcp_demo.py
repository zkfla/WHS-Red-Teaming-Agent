"""Harmless temporary workspace through Catalog -> Control -> stdio MCP.

No environment configuration, credentials, external targets, LLM or write
approval bypass. Mutations/approval are exercised in integration tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.workspace_manifest import WORKSPACE_READ_TOOLS
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import AssetTarget
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, RunId


async def demo(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "synthetic.md").write_text("synthetic evidence marker\n", encoding="utf-8")
    asset = "workspace-demo"
    engagement_id = EngagementId("eng-workspace-demo")
    run_id = RunId("run-workspace-demo")
    policy = root / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "asset_ref": asset,
                "engagement_id": engagement_id,
                "run_id": run_id,
                "root": str(workspace),
                "writable": False,
            }
        ),
        encoding="utf-8",
    )
    prefix = "REDTEAM_MCP_WORKSPACE_DEMO"
    settings = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": sys.executable,
        "ARGS_JSON": json.dumps(
            [
                "-m",
                "redteam.capability.servers.workspace",
                "--policy",
                str(policy),
                "--mode",
                "read",
            ]
        ),
        "PROFILE": "workspace_read",
        "WORKSPACE_ASSET_REF": asset,
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": json.dumps(list(WORKSPACE_READ_TOOLS)),
        "TOOL_OPERATIONS_JSON": json.dumps(
            {name: [spec.operation] for name, spec in WORKSPACE_READ_TOOLS.items()}
        ),
    }
    for suffix, value in settings.items():
        os.environ[f"{prefix}_{suffix}"] = value
    engagement = Engagement(
        engagement_id=engagement_id,
        name="temporary synthetic workspace",
        scope=EngagementScope(allowed_assets=[asset]),
        execution_budget=ExecutionBudget(max_actions=3),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=3),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    async with open_mcp_tool_gateway(["workspace-demo"], health_refresh_enabled=False) as gateway:
        control = DefaultControlPlane(
            {engagement_id: engagement},
            risk_resolver=RegistryRiskResolver(gateway),
        )
        arguments = {
            "list_directory": {},
            "read_file": {"path": "synthetic.md"},
            "search_files": {"pattern": "marker"},
        }
        for name, spec in WORKSPACE_READ_TOOLS.items():
            candidates = gateway.list_tools_for_action("analysis", spec.operation)
            assert len(candidates) == 1
            proposal = ActionProposal(
                action_id=ActionId(f"action-{name}"),
                run_id=run_id,
                engagement_id=engagement_id,
                agent_id=AgentId("synthetic-workspace-demo"),
                capability="analysis",
                operation=spec.operation,
                target=AssetTarget(asset_id=AssetId(asset)),
                arguments={"asset_ref": asset, **arguments[name]},
                reason="read approved synthetic fixture",
                created_at=datetime.now(timezone.utc),
            )
            decision = await control.evaluate(proposal, ControlContext(engagement_id=engagement_id))
            assert decision.verdict == PolicyVerdict.ALLOW
            result = await gateway.invoke(
                build_tool_invocation(
                    proposal,
                    decision,
                    tool_id=candidates[0].tool_id,
                )
            )
            if not result.success:
                raise RuntimeError(f"workspace demo rejected: {result.error.code}")
            print(
                json.dumps(
                    {
                        "tool_id": str(result.tool_id),
                        "operation": spec.operation,
                        "control": decision.verdict.value,
                        "success": result.success,
                    }
                )
            )


async def main() -> None:
    with TemporaryDirectory(prefix="redteam-workspace-demo-") as directory:
        await demo(Path(directory))


if __name__ == "__main__":
    asyncio.run(main())
