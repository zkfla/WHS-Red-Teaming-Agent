"""Authentication follow-ups are bounded, policy-gated and observation-only."""

from datetime import datetime, timezone

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import PolicyVerdict, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.events import AUTHENTICATION_CHECK_DEFERRED, AUTHENTICATION_CHECKED
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, FakeControlPlane, build_test_application


class SessionGateway(DummyToolGateway):
    def __init__(self, status, session_status):
        tools = [
            ToolDescriptor(
                tool_id=ToolId(tool),
                server_id="test",
                tool_name=tool.split(".")[1],
                capability_tags=[tag],
                health_status=ToolHealthStatus.HEALTHY,
                input_schema={
                    "type": "object",
                    "required": ["target_ref", "path"],
                    "properties": {
                        "target_ref": {"type": "string"},
                        "path": {"type": "string"},
                        "method": {"type": "string"},
                        "session_ref": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            )
            for tool, tag in [("recon.http_probe", "recon"), ("web.http_request", "web")]
        ]
        super().__init__(tools=tools)
        self.status, self.session_status = status, session_status
        self.disable_after_anonymous = False

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        if "session_ref" in invocation.arguments:
            if self.session_status is None:
                return result.model_copy(
                    update={
                        "success": False,
                        "output": None,
                        "error": ToolError(
                            code="session_unavailable", message="session unavailable"
                        ),
                    }
                )
            status = self.session_status
        else:
            status = self.status
            if invocation.tool_id == "web.http_request" and self.disable_after_anonymous:
                self._tools = [t for t in self._tools if t.tool_id != "web.http_request"]
        return result.model_copy(update={"output": {"status_code": status}})


class SessionGate(FakeControlPlane):
    def __init__(self, verdict):
        super().__init__()
        self.session_verdict = verdict

    async def evaluate(self, proposal, context):
        self.default_verdict = (
            self.session_verdict if "session_ref" in proposal.arguments else PolicyVerdict.ALLOW
        )
        return await super().evaluate(proposal, context)


async def exercise(
    *,
    anonymous=401,
    authenticated=200,
    sessions=None,
    verdict=PolicyVerdict.ALLOW,
    tool_available=True,
    max_actions=10,
):
    engagement = Engagement(
        engagement_id=EngagementId("eng_auth"),
        name="auth",
        scope=EngagementScope(allowed_hosts=["app.local"]),
        metadata={
            "target_refs": {"app.local": "app"},
            "auth_sessions": {"app.local": sessions or []},
        },
        execution_budget=ExecutionBudget(max_actions=max_actions),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        created_at=datetime.now(timezone.utc),
    )
    gateway = SessionGateway(anonymous, authenticated)
    gateway.disable_after_anonymous = not tool_available
    control = SessionGate(verdict)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        control_plane=control,
        settings=OrchestrationSettings(max_steps=30),
        agents=default_registry(),
    )
    final = await run_engagement(engagement, deps=deps)
    return final, deps, gateway, control


@pytest.mark.parametrize(
    "status,outcome",
    [
        (200, "session_access_observed"),
        (401, "session_not_accepted"),
        (403, "access_denied"),
        (302, "inconclusive_response"),
        (None, "request_failed_or_uninterpretable"),
    ],
)
async def test_401_creates_exactly_one_gated_session_check(status, outcome):
    final, deps, gateway, control = await exercise(
        authenticated=status,
        sessions=[{"session_ref": "alice", "paths": ["/"]}],
    )
    assert len(gateway.invocations) == 3
    assert len(control.evaluated) == 3
    assert gateway.invocations[-1].arguments == {
        "target_ref": "app",
        "path": "/",
        "method": "GET",
        "session_ref": "alice",
    }
    events = deps.event_bus.history(event_type=AUTHENTICATION_CHECKED)
    assert len(events) == 1
    assert events[0].payload["outcome"] == outcome
    refs = events[0].payload["evidence_ids"]
    assert len(refs) == 2
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert set(refs) <= {e.evidence_id for e in evidence}
    graph = AttackGraphService(deps.graph_store)
    assert await graph.list_findings() == []
    hypotheses = await graph.list_hypotheses()
    auth = [h for h in hypotheses if h.metadata.get("kind") == "authentication"]
    assert len(auth) == 1 and auth[0].status != "open"
    assert final.termination_reason == "no_actionable_plan"


@pytest.mark.parametrize("sessions", [[], [{"session_ref": "alice", "paths": ["/other"]}]])
async def test_missing_session_is_reported_without_making_a_request(sessions):
    _, deps, gateway, _ = await exercise(sessions=sessions)
    assert len(gateway.invocations) == 2
    deferred = deps.event_bus.history(event_type=AUTHENTICATION_CHECK_DEFERRED)
    assert deferred and deferred[0].payload["reason"] == "no_registered_session_for_path"


async def test_missing_tool_is_reported_without_executing_followup():
    _, deps, gateway, _ = await exercise(
        sessions=[{"session_ref": "alice", "paths": ["/"]}],
        tool_available=False,
    )
    assert len(gateway.invocations) == 2
    assert any(
        e.payload["reason"] == "no_compatible_authentication_tool"
        for e in deps.event_bus.history(event_type=AUTHENTICATION_CHECK_DEFERRED)
    )


@pytest.mark.parametrize(
    "verdict,reason",
    [
        (PolicyVerdict.DENY, "no_actionable_plan"),
        (PolicyVerdict.REQUIRE_APPROVAL, "approval_pending"),
    ],
)
async def test_followup_cannot_bypass_control(verdict, reason):
    final, deps, gateway, control = await exercise(
        sessions=[{"session_ref": "alice", "paths": ["/"]}],
        verdict=verdict,
    )
    assert len(gateway.invocations) == 2
    assert len(control.evaluated) == 3
    assert final.termination_reason == reason
    assert not deps.event_bus.history(event_type=AUTHENTICATION_CHECKED)


async def test_denied_session_200_is_a_confirmed_access_control_finding():
    # The operator explicitly registered bob as expected_access="deny" for
    # "/", yet the session reached it (200). That is an operator-declared
    # policy violation corroborated by two evidence records (anonymous 401
    # baseline + this session's 200), so -- unlike an ambiguous status-only
    # signal -- it IS promoted to a verified Finding.
    _, deps, _, _ = await exercise(
        sessions=[{"session_ref": "bob", "paths": ["/"], "expected_access": "deny"}]
    )
    assert (
        deps.event_bus.history(event_type=AUTHENTICATION_CHECKED)[0].payload["outcome"]
        == "access_control_violation"
    )
    findings = await AttackGraphService(deps.graph_store).list_findings(verified=True)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.verified is True
    assert finding.severity == "high"
    # Corroborated by the baseline 401 and the session's 200.
    assert len(finding.evidence_ids) == 2


async def test_each_session_is_checked_once_and_preserves_anonymous_evidence():
    final, deps, gateway, _ = await exercise(
        sessions=[{"session_ref": f"actor{i}", "paths": ["/"]} for i in range(5)]
    )
    assert len(gateway.invocations) == 7
    assert final.termination_reason == "no_actionable_plan"
    graph = AttackGraphService(deps.graph_store)
    endpoints = [n for n in await graph.list_nodes() if n.type.value == "endpoint"]
    observations = facts_of(endpoints[0].properties)["observations"]
    assert len(observations) == 6
    assert observations[0]["status_code"] == 401 and observations[0]["session_ref"] is None
    assert len({o["session_ref"] for o in observations[1:]}) == 5


@pytest.mark.parametrize("status", [200, 403, 500])
async def test_other_status_does_not_start_authentication(status):
    _, deps, gateway, _ = await exercise(
        anonymous=status,
        sessions=[{"session_ref": "alice", "paths": ["/"]}],
    )
    assert len(gateway.invocations) == 2
    assert not deps.event_bus.history(event_type=AUTHENTICATION_CHECKED)


@pytest.mark.parametrize("mismatch", ["missing", "path", "target_ref", "session_ref", "method"])
async def test_mismatched_baseline_or_request_cannot_confirm_authentication(mismatch):
    from redteam.core.contracts.tool import ToolResult
    from redteam.core.ids import EvidenceId
    from redteam.orchestration.authentication import AuthenticationCheck, assess_authentication

    final, deps, gateway, control = await exercise(
        sessions=[{"session_ref": "alice", "paths": ["/"]}]
    )
    event = deps.event_bus.history(event_type=AUTHENTICATION_CHECKED)[0]
    baseline_id, current_id = event.payload["evidence_ids"]
    evidence = await deps.evidence_store.get(EvidenceId(current_id))
    proposal = control.evaluated[-1]
    if mismatch != "missing":
        arguments = dict(proposal.arguments)
        arguments[mismatch] = "POST" if mismatch == "method" else "/different"
        proposal = proposal.model_copy(update={"arguments": arguments})
    invocation = gateway.invocations[-1]
    now = datetime.now(timezone.utc)
    result = ToolResult(
        invocation_id=invocation.invocation_id,
        action_id=proposal.action_id,
        tool_id=invocation.tool_id,
        success=True,
        output={"status_code": 200},
        started_at=now,
        finished_at=now,
    )
    assessment = await assess_authentication(
        AuthenticationCheck(
            baseline_evidence_id=EvidenceId("missing" if mismatch == "missing" else baseline_id),
            session_ref="alice",
        ),
        proposal,
        result,
        evidence,
        deps.evidence_store,
    )
    assert assessment.hypothesis_status == "inconclusive"
    assert assessment.outcome == "baseline_evidence_unavailable_or_mismatched"
    assert assessment.evidence_ids == [current_id]
