"""Direct tool choice survives compilation, approval, execution and evidence conversion."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control.approval import ApprovalService
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    CommandApprovalRule,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget, UrlTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolTargetBinding
from redteam.core.convert import build_tool_invocation, tool_result_to_evidence
from redteam.core.enums import ApprovalStatus, PolicyVerdict, RiskLevel, ToolHealthStatus
from redteam.core.ids import ActionId, AgentId, EngagementId, GraphNodeId, RunId, ToolId
from redteam.core.tool_binding import execution_fingerprint
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler, candidate_key
from redteam.orchestration.candidates.models import CandidateDraft


def _tool(**updates):
    values = dict(
        tool_id=ToolId("analysis.provider.run"),
        server_id="provider",
        tool_name="run",
        capability_tags=["analysis"],
        target_types=["host"],
        health_status=ToolHealthStatus.HEALTHY,
        risk_level=RiskLevel.HIGH,
        command_fields=["executable", "args"],
        input_schema={
            "type": "object",
            "properties": {
                "executable": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["executable", "args"],
            "additionalProperties": False,
        },
    )
    values.update(updates)
    return ToolDescriptor(**values)


class _Adapter:
    server_id = "provider"

    def __init__(self):
        self.calls = []

    async def invoke(self, tool, arguments):
        self.calls.append((tool.tool_id, arguments))
        now = datetime.now(timezone.utc)
        return MCPCallResult(success=True, started_at=now, finished_at=now, output="observed text")


def _setup(tool=None, rules=None):
    tool = tool or _tool()
    registry = MCPToolRegistry()
    registry.register(tool)
    adapter = _Adapter()
    gateway = MCPToolGateway(registry, {"provider": adapter}, {}, health_refresh_enabled=False)
    engagement = Engagement(
        engagement_id=EngagementId("eng-selected"),
        name="selected tool",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(
            require_approval_for=["analysis"],
            auto_approve_up_to=RiskLevel.HIGH,
            command_approval_rules=rules or [],
        ),
        created_at=datetime.now(timezone.utc),
    )
    approvals = ApprovalService()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=approvals,
        outbound_descriptor_resolver=gateway.get,
    )
    return gateway, adapter, engagement, approvals, plane


def _proposal(arguments=None):
    return ActionProposal(
        action_id=ActionId("action-selected"),
        run_id=RunId("run-selected"),
        engagement_id=EngagementId("eng-selected"),
        agent_id=AgentId("specialist"),
        binding_mode="selected_tool",
        tool_id=ToolId("analysis.provider.run"),
        target=HostTarget(host="good.local"),
        arguments=arguments or {"executable": "grep", "args": ["needle", "input.txt"]},
        reason="inspect approved target",
        created_at=datetime.now(timezone.utc),
    )


async def test_unbound_direct_choice_compiles_executes_and_preserves_raw_evidence():
    gateway, adapter, _, _, plane = _setup()
    proposal = _proposal()
    step = PlanStep(
        step_id="direct",
        binding_mode="selected_tool",
        tool_id=proposal.tool_id,
        target=proposal.target,
        arguments=proposal.arguments,
        reason=proposal.reason,
    )
    draft = CandidateDraft(
        agent_id=proposal.agent_id, plan_step=step, target_node_ids=[GraphNodeId("host:good.local")]
    )
    candidates, rejected = CandidateCompiler(gateway).compile_drafts_with_rejections(
        [draft], allowed_capabilities={"unrelated"}
    )
    assert rejected == [] and len(candidates) == 1
    assert candidates[0].tool.operations == []
    decision = await plane.evaluate(
        proposal,
        ControlContext(engagement_id=proposal.engagement_id, selected_tool_id=proposal.tool_id),
    )
    assert decision.verdict is PolicyVerdict.ALLOW
    invocation = build_tool_invocation(proposal, decision, tool_id=candidates[0].tool.tool_id)
    assert invocation.operation == "" and invocation.binding_mode == "selected_tool"
    result = await gateway.invoke(invocation)
    assert result.success and result.output == "observed text"
    assert adapter.calls == [(proposal.tool_id, proposal.arguments)]
    evidence = tool_result_to_evidence(
        result, run_id=proposal.run_id, engagement_id=proposal.engagement_id
    )
    assert (
        evidence.source_id == proposal.tool_id
        and evidence.invocation_id == invocation.invocation_id
    )


@pytest.mark.parametrize(
    "arguments, pattern",
    [
        ({"executable": "grep", "args": ["needle"]}, "grep"),
        ({"executable": "rm", "args": ["-rf", "/workspace/item"]}, "rm -rf"),
        ({"executable": "dd", "args": ["if=input", "of=output"]}, "dd if="),
    ],
)
async def test_command_components_require_human_despite_general_rule_and_auto_ceiling(
    arguments, pattern
):
    gateway, adapter, _, approvals, plane = _setup(
        rules=[CommandApprovalRule(name="human", patterns=[pattern])]
    )
    proposal = _proposal(arguments)
    decision = await plane.evaluate(
        proposal,
        ControlContext(engagement_id=proposal.engagement_id, selected_tool_id=proposal.tool_id),
    )
    assert decision.verdict is PolicyVerdict.REQUIRE_APPROVAL and decision.human_required
    assert "command:human" in decision.matched_rules and "analysis" in decision.matched_rules
    approval = await approvals.get(decision.approval_id)
    assert approval.execution_fingerprint == decision.execution_fingerprint
    assert adapter.calls == []
    await plane.resolve_approval(decision.approval_id, ApprovalStatus.APPROVED, "operator")
    resumed = await plane.approval_outcome(decision.approval_id, current_proposal=proposal)
    assert resumed.verdict is PolicyVerdict.ALLOW
    result = await gateway.invoke(
        build_tool_invocation(proposal, resumed, tool_id=proposal.tool_id)
    )
    assert result.success and len(adapter.calls) == 1


@pytest.mark.parametrize("change", ["tool", "target", "arguments"])
async def test_approval_restart_and_cached_resume_reject_changed_call(change):
    gateway, _, engagement, approvals, plane = _setup(
        rules=[CommandApprovalRule(patterns=["grep"])]
    )
    proposal = _proposal()
    decision = await plane.evaluate(
        proposal,
        ControlContext(engagement_id=proposal.engagement_id, selected_tool_id=proposal.tool_id),
    )
    await plane.resolve_approval(decision.approval_id, ApprovalStatus.APPROVED, "operator")
    restored = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=approvals,
        outbound_descriptor_resolver=gateway.get,
    )
    checkpoint = ActionProposal.model_validate_json(proposal.model_dump_json())
    await restored.register_pending_proposal(decision.approval_id, checkpoint)
    updates = {
        "tool": {"tool_id": ToolId("analysis.other.run")},
        "target": {"target": HostTarget(host="outside.local")},
        "arguments": {"arguments": {"executable": "grep", "args": ["different"]}},
    }[change]
    changed = checkpoint.model_copy(update=updates)
    # A restart must refuse linking different content to the durable approval.
    bad_restart = DefaultControlPlane({engagement.engagement_id: engagement}, approvals=approvals)
    with pytest.raises(ValueError, match="fingerprint"):
        await bad_restart.register_pending_proposal(decision.approval_id, changed)
    allowed = await restored.approval_outcome(decision.approval_id, current_proposal=checkpoint)
    assert allowed.verdict is PolicyVerdict.ALLOW
    assert (
        await restored.approval_outcome(decision.approval_id, current_proposal=changed)
    ).verdict is PolicyVerdict.DENY
    with pytest.raises(ValueError, match="fingerprint"):
        build_tool_invocation(changed, allowed, tool_id=changed.tool_id)
    with pytest.raises(ValueError, match="tool_id"):
        build_tool_invocation(checkpoint, allowed, tool_id=ToolId("analysis.other.run"))


async def test_direct_resume_cannot_reuse_legacy_approval_without_fingerprint():
    _, _, engagement, approvals, _ = _setup()
    proposal = _proposal()
    legacy_approval = await approvals.request(proposal.action_id)
    restored = DefaultControlPlane({engagement.engagement_id: engagement}, approvals=approvals)
    with pytest.raises(ValueError, match="no execution fingerprint"):
        await restored.register_pending_proposal(legacy_approval.approval_id, proposal)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), object()])
def test_execution_fingerprint_never_stringifies_non_json_arguments(invalid):
    with pytest.raises((TypeError, ValueError)):
        execution_fingerprint(
            ToolId("analysis.provider.run"), HostTarget(host="good.local"), {"value": invalid}
        )


def test_direct_compilation_consumes_provider_url_semantics_without_operation():
    tool = _tool(
        target_types=["url"],
        command_fields=[],
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        target_binding=ToolTargetBinding(requires_url_target=True, requires_exact_url_match=True),
    )
    gateway, _, _, _, _ = _setup(tool)

    def draft(target, arguments):
        from redteam.attack_graph.identity import node_id_for_target

        return CandidateDraft(
            agent_id=AgentId("specialist"),
            plan_step=PlanStep(
                step_id="url",
                binding_mode="selected_tool",
                tool_id=tool.tool_id,
                target=target,
                arguments=arguments,
                reason="read",
            ),
            target_node_ids=[node_id_for_target(target)],
        )

    compiler = CandidateCompiler(gateway)
    good = draft(UrlTarget(url="https://good.local/known"), {})
    compiled = compiler.compile_drafts([good])
    assert compiled[0].plan_step.arguments == {"url": "https://good.local/known"}
    _, rejected = compiler.compile_drafts_with_rejections(
        [draft(good.plan_step.target, {"url": "https://good.local/other"})]
    )
    assert rejected
    decorated = good.model_copy(
        update={
            "plan_step": good.plan_step.model_copy(
                update={"capability": "annotation", "operation": "annotation"}
            )
        }
    )
    assert candidate_key(good) == candidate_key(decorated)
