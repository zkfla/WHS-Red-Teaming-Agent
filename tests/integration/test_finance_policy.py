"""The provided AppConfig requires actual Control Plane approval for active tools."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_application
from redteam.core.config import load_app_config
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.convert import NonAllowDecisionError, build_tool_invocation
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, RunId, ToolId
from redteam.llm.fake import FakeLLMClient
from tests.fakes.tool_gateway import DummyToolGateway


async def test_finance_config_blocks_unapproved_port_scan(tmp_path: Path) -> None:
    config = load_app_config(Path(__file__).parents[2] / "deploy/mcp/finance/app.yaml")
    engagement, deps = await build_application(
        config,
        evidence_dir=tmp_path,
        tool_gateway=DummyToolGateway(),
        llm=FakeLLMClient(),
    )
    proposal = ActionProposal(
        action_id=ActionId("scan"),
        run_id=RunId("run"),
        engagement_id=engagement.engagement_id,
        agent_id=AgentId("recon"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host="mock-bank"),
        arguments={"target_ref": "bank-lab"},
        reason="synthetic test",
        created_at=datetime.now(timezone.utc),
    )
    decision = await deps.control_plane.evaluate(
        proposal,
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    with pytest.raises(NonAllowDecisionError):
        build_tool_invocation(proposal, decision, tool_id=ToolId("recon.port_scan"))
