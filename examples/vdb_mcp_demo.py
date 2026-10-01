"""Synthetic VDB -> real stdio catalog -> Control -> Gateway; no network/LLM."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from build_vdb_synthetic_fixture import build

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.vdb_manifest import VDB_TOOLS
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
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, RunId, ToolId


async def demo(directory: Path) -> None:
    root = directory / "db"
    build(root)
    asset = "vdb-demo"
    engagement_id = EngagementId("eng-vdb-demo")
    run_id = RunId("run-vdb-demo")
    policy = directory / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "root": str(root),
                "asset_ref": asset,
                "engagement_id": str(engagement_id),
                "run_id": str(run_id),
                "synthetic": True,
            }
        ),
        encoding="utf-8",
    )
    settings = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": sys.executable,
        "ARGS_JSON": json.dumps(
            ["-m", "redteam.capability.servers.vdb_readonly", "--policy", str(policy)]
        ),
        "PROFILE": "vdb_readonly",
        "VDB_ASSET_REF": asset,
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": json.dumps(list(VDB_TOOLS)),
        "TOOL_OPERATIONS_JSON": json.dumps({n: [s.operation] for n, s in VDB_TOOLS.items()}),
    }
    for suffix, value in settings.items():
        os.environ[f"REDTEAM_MCP_VDB_DEMO_{suffix}"] = value
    engagement = Engagement(
        engagement_id=engagement_id,
        name="synthetic VDB only",
        scope=EngagementScope(allowed_assets=[asset]),
        execution_budget=ExecutionBudget(max_actions=3),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=3),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    args = {
        "vdb_status": {},
        "search_by_cve": {"cve_id": "CVE-2026-900001"},
        "search_by_purl_like": {"purl": "pkg:npm/redteam-synthetic-demo@1.5.0"},
    }
    async with open_mcp_tool_gateway(["vdb-demo"], health_refresh_enabled=False) as gateway:
        control = DefaultControlPlane(
            {engagement_id: engagement}, risk_resolver=RegistryRiskResolver(gateway)
        )
        for name, spec in VDB_TOOLS.items():
            proposal = ActionProposal(
                action_id=ActionId(f"action-{name}"),
                run_id=run_id,
                engagement_id=engagement_id,
                agent_id=AgentId("vdb-demo-agent"),
                capability="analysis",
                operation=spec.operation,
                target=AssetTarget(asset_id=AssetId(asset)),
                arguments={"asset_ref": asset, **args[name]},
                reason="synthetic knowledge lookup only",
                created_at=datetime.now(timezone.utc),
            )
            decision = await control.evaluate(proposal, ControlContext(engagement_id=engagement_id))
            assert decision.verdict == PolicyVerdict.ALLOW
            result = await gateway.invoke(
                build_tool_invocation(proposal, decision, tool_id=ToolId(f"analysis.{name}"))
            )
            if not result.success:
                raise RuntimeError(f"vdb demo rejected: {result.error.code}")
            print(
                json.dumps(
                    {
                        "tool_id": str(result.tool_id),
                        "control": decision.verdict.value,
                        "success": result.success,
                        "synthetic": True,
                        "match_count": len(result.output.get("matches", [])),
                        "target_vulnerability_confirmed": False,
                    }
                )
            )


async def main() -> None:
    with TemporaryDirectory(prefix="redteam-vdb-demo-") as directory:
        await demo(Path(directory).resolve())


if __name__ == "__main__":
    asyncio.run(main())
