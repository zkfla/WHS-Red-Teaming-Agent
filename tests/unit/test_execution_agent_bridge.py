from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.orchestration.execution_bridge import (
    agent_tool_visible,
    bind_execution_arguments,
    execution_agent_options,
)
from redteam.orchestration.seed import seed_from_engagement


def engagement(
    *, execution_agent: dict | None = None, apk_ref: str | None = None
) -> Engagement:
    metadata: dict[str, Any] = {"asset_ref": "workspace-test"}
    if apk_ref is not None:
        metadata["apk_ref"] = apk_ref
    if execution_agent is not None:
        metadata["execution_agent"] = execution_agent
    return Engagement(
        engagement_id=EngagementId("eng-test"),
        name="execution bridge",
        scope=EngagementScope(allowed_assets=["workspace-test"]),
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata=metadata,
    )


def execution_descriptor(
    name: str = "run_command", operation: str = "command_run"
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(f"analysis.{name}"),
        server_id="execution",
        tool_name=name,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        operations=[operation],
        target_types=["asset"],
        capability_tags=["analysis"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )


def analysis_descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("analysis.decode_apk"),
        server_id="apktool",
        tool_name="decode_apk",
        input_schema={"type": "object"},
        operations=["static_analysis"],
        target_types=["asset"],
        capability_tags=["analysis"],
        health_status=ToolHealthStatus.HEALTHY,
    )




@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "dir/../outside", "dir\\file", "dir//file"],
)
async def test_invalid_execution_workspace_path_is_rejected_before_graph_seed(
    path: str,
) -> None:
    configured = engagement(
        execution_agent={
            "enabled": True,
            "assets": {"workspace-test": {"recipe": "line_count", "path": path}},
        }
    )

    with pytest.raises(ValueError, match="relative POSIX path"):
        await seed_from_engagement(
            AttackGraphService(InMemoryAttackGraphStore()), configured
        )


async def test_enabled_bridge_uses_seeded_facts_and_overwrites_model_command() -> None:
    configured = engagement(
        execution_agent={
            "enabled": True,
            "assets": {
                "workspace-test": {"recipe": "line_count", "path": "input.txt"}
            },
        }
    )
    service = AttackGraphService(InMemoryAttackGraphStore())
    [node] = await seed_from_engagement(service, configured, run_id=RunId("run-test"))

    assert facts_of(node.properties) | {
        "execution_recipe": "line_count",
        "execution_workspace_path": "input.txt",
    } == facts_of(node.properties)
    options = await execution_agent_options(service, configured, RunId("run-test"))
    assert options == [
        {
            "target_node_id": "asset:workspace-test",
            "capability": "analysis",
            "operation": "command_run",
            "tool_id": "analysis.run_command",
            "arguments": {
                "asset_ref": "workspace-test",
                "executable": "wc",
                "args": ["-l", "input.txt"],
                "timeout_seconds": 10.0,
                "engagement_id": "eng-test",
                "run_id": "run-test",
            },
        }
    ]
    bound = bind_execution_arguments(
        engagement=configured,
        run_id=RunId("run-test"),
        node=node,
        tool=execution_descriptor(),
        capability="analysis",
        operation="command_run",
        arguments={"executable": "python", "args": ["-c", "malicious"]},
    )
    assert bound == options[0]["arguments"]


def test_bridge_exposes_only_synchronous_fixed_execution_operation() -> None:
    assert not agent_tool_visible(execution_descriptor(), execution_ready=False)
    assert agent_tool_visible(execution_descriptor(), execution_ready=True)
    assert not agent_tool_visible(
        execution_descriptor("job_start", "command_job_start"),
        execution_ready=True,
    )
    assert agent_tool_visible(analysis_descriptor(), execution_ready=False)
