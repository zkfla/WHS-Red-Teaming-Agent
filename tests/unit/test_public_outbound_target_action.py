"""Short C regressions: narrow outbound gate and per-target action limits."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.audit import AuditLog, InMemoryAuditStore
from redteam.control.outbound import evaluate_with_descriptor
from redteam.control.service import DefaultControlPlane
from redteam.control.target_action import check_target_action_policies
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    TargetActionPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import EndpointTarget, HostTarget, UrlTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId, ToolId

SECRET = "SECRET_VALUE_XYZ"


def _descriptor(fields: list[str]) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("web.web_search"),
        server_id="web-search",
        tool_name="web_search",
        outbound_text_fields=fields,
    )


def _proposal(args: dict, target=None) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_pub"),
        run_id=RunId("run_pub"),
        engagement_id=EngagementId("eng_pub"),
        agent_id=AgentId("agent_pub"),
        capability="web",
        operation="page_read",
        target=target or HostTarget(host="docs.example"),
        arguments=args,
        reason="public lookup",
        created_at=datetime.now(timezone.utc),
    )


def test_only_declared_fields_are_inspected() -> None:
    blocked, reasons, _ = evaluate_with_descriptor(
        _proposal({"query": "password=" + SECRET, "other": "plain"}),
        _descriptor(["query"]),
        None,
    )
    assert blocked is True
    assert SECRET not in " ".join(reasons)
    allowed, _, _ = evaluate_with_descriptor(
        _proposal({"other": "password=" + SECRET}),
        _descriptor(["query"]),
        None,
    )
    assert allowed is False
    plain, _, _ = evaluate_with_descriptor(
        _proposal({"query": "token usage notes"}),
        _descriptor(["query"]),
        None,
    )
    assert plain is False


def test_empty_and_unknown_descriptors_do_not_batch_reject() -> None:
    blocked, _, _ = evaluate_with_descriptor(
        _proposal({"query": "password=" + SECRET}),
        _descriptor([]),
        None,
    )
    assert blocked is False
    passed, _, _ = evaluate_with_descriptor(
        _proposal({"query": "password=" + SECRET}),
        None,
        None,
    )
    assert passed is False


async def test_first_storage_rejects_safely_without_persisting_secret() -> None:
    store = InMemoryAuditStore()
    engagement = Engagement(
        engagement_id=EngagementId("eng_pub"),
        name="pub",
        scope=EngagementScope(allowed_hosts=["docs.example"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(store),
        outbound_descriptor_resolver=lambda _: _descriptor(["query"]),
    )
    proposal = _proposal({"query": "token=" + SECRET})
    decision = await plane.evaluate(
        proposal,
        ControlContext(
            engagement_id=engagement.engagement_id,
            selected_tool_id=ToolId("web.web_search"),
        ),
    )
    assert decision.verdict == PolicyVerdict.DENY
    assert "outbound_text" in decision.matched_rules
    assert SECRET not in " ".join(decision.reasons)
    entries = await store.list_for_action(proposal.action_id)
    assert entries
    dumped = "".join(entry.model_dump_json() for entry in entries)
    assert SECRET not in dumped


def test_host_url_endpoint_spellings_reach_same_restriction() -> None:
    selector = EngagementScope(allowed_hosts=["docs.example"])
    policy = TargetActionPolicy(
        name="read-only",
        selector=selector,
        allowed_action_keys=["web.page_read"],
    )
    for target in (
        HostTarget(host="docs.example"),
        UrlTarget(url="https://docs.example/guide?q=1"),
        EndpointTarget(host="docs.example", port=443, path="/other"),
    ):
        ok, _, _ = check_target_action_policies(_proposal({}, target), [policy])
        assert ok is True
    other_path = _proposal({}, UrlTarget(url="https://docs.example/totally-different"))
    ok, _, _ = check_target_action_policies(other_path, [policy])
    assert ok is True
    foreign = _proposal({}, UrlTarget(url="https://other.example/guide?q=1"))
    ok, _, _ = check_target_action_policies(foreign, [policy])
    assert ok is True
    denied = _proposal({"x": 1}, HostTarget(host="docs.example")).model_copy(
        update={"capability": "web", "operation": "file_upload"}
    )
    ok, reasons, _ = check_target_action_policies(denied, [policy])
    assert ok is False
    assert SECRET not in " ".join(reasons)


def test_portless_target_does_not_bypass_port_pinned_selector() -> None:
    # Port-pinned selector still matches a port-less spelling (fail closed),
    # so the allow/deny keys still apply instead of being bypassed.
    selector = EngagementScope(allowed_hosts=["docs.example:8443"])
    policy = TargetActionPolicy(
        name="pinned",
        selector=selector,
        allowed_action_keys=["web.page_read"],
    )
    ok, _, _ = check_target_action_policies(
        _proposal({}, EndpointTarget(host="docs.example", port=8443)), [policy]
    )
    assert ok is True
    denied = _proposal({}, EndpointTarget(host="docs.example", port=8443)).model_copy(
        update={"operation": "file_upload"}
    )
    ok, _, _ = check_target_action_policies(denied, [policy])
    assert ok is False
    # No port cannot prove a different port: still matched, still denied.
    portless_denied = _proposal({}, EndpointTarget(host="docs.example")).model_copy(
        update={"operation": "file_upload"}
    )
    missing, _, _ = check_target_action_policies(portless_denied, [policy])
    assert missing is False
    # A different concrete port is genuinely a different target (no match).
    other_port = _proposal({}, UrlTarget(url="https://docs.example/")).model_copy(
        update={"operation": "file_upload"}
    )
    scheme_default, _, _ = check_target_action_policies(other_port, [policy])
    assert scheme_default is True


def test_deny_wins_and_explicit_empty_allow_denies() -> None:
    selector = EngagementScope(allowed_hosts=["docs.example"])
    both = TargetActionPolicy(
        name="both",
        selector=selector,
        allowed_action_keys=["web.page_read"],
        denied_action_keys=["web.page_read"],
    )
    ok, _, _ = check_target_action_policies(
        _proposal({}, HostTarget(host="docs.example")), [both]
    )
    assert ok is False
    empty = TargetActionPolicy(
        name="empty", selector=selector, allowed_action_keys=[]
    )
    ok, _, _ = check_target_action_policies(
        _proposal({}, HostTarget(host="docs.example")), [empty]
    )
    assert ok is False
