"""Authentication follow-ups are bounded, policy-gated and observation-only.

New-flow notes: every request below comes from an explicit scripted model
intent through the production InvestigationEngine path. The old file assumed
a deterministic candidate frontier (exact invocation counts of 2/3/7 driven
by auto-generated recon + web probes); those counts are retired. Counts here
equal the scripted intents: 1 anonymous probe + 1 session check, nothing more.

Retired deterministic items (with replacement):
- exact auto counts (3 for one session, 7 for five, 2 for deferred/missing) ->
  this file's scripted 2-call chain (bounded: each session checked at most
  once, anonymous evidence preserved) keeps the contract without the planner.
- deferred/tool-missing branches via auto-candidates ->
  covered as policy/availability outcomes in test_session_followup_deny_blocks
  and the DENY control-plane cases in test_agent_loop.py.
- denied-session violation promotion (deny + 200 -> access_control_violation):
  test_denied_session_200_is_a_confirmed_access_control_finding pins the verified
  high-severity Finding, the confirmed hypothesis and their linkage on the
  scripted session path. The observation-only chain above stays finding-free.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.authentication import (
    AuthenticationCheck,
    assess_authentication,
    comparison_outcome,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import AUTHENTICATION_CHECKED
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h

HOST_NODE = "host:app.local"
WEB_AGENT = "web-api-agent"
WEB_TOOL = "web.http_request"
WEB_OP_KEY = "web.http_request"


class SessionGateway(DummyToolGateway):
    def __init__(self, status: int | None, session_status: int | None) -> None:
        tools = [
            ToolDescriptor(
                tool_id=ToolId(tool),
                server_id="test",
                tool_name=tool.split(".")[1],
                capability_tags=[tag],
                operations=["endpoint_discovery" if tag == "recon" else "http_request"],
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
        self.status = status
        self.session_status = session_status

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
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
            status = self.status if self.status is not None else 401
        target_ref = invocation.arguments.get("target_ref")
        session_ref = invocation.arguments.get("session_ref")
        return result.model_copy(
            update={
                "output": {
                    "target_ref": target_ref,
                    "session_ref": session_ref,
                    "status_code": status,
                    "body_bytes": 0,
                    "body_sha256": ("b" if session_ref else "a") * 64,
                    "redirect": 300 <= status < 400,
                    "security_headers_present": {},
                    "body_omitted": True,
                }
            }
        )


class SessionGate(FakeControlPlane):
    def __init__(self, verdict: PolicyVerdict) -> None:
        super().__init__()
        self.session_verdict = verdict

    async def evaluate(self, proposal: Any, context: Any) -> Any:
        self.default_verdict = (
            self.session_verdict if "session_ref" in proposal.arguments else PolicyVerdict.ALLOW
        )
        return await super().evaluate(proposal, context)


class _AuthLLM:
    """Two scripted web intents: anonymous probe, then one gated session check."""

    def __init__(
        self,
        evidence: Any,
        *,
        session_ref: str = "alice",
        expected_access: str = "unspecified",
    ) -> None:
        self._evidence = evidence
        self._session_ref = session_ref
        self._expected_access = expected_access
        self.calls: list[dict[str, Any]] = []
        self._actions = 0

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    @staticmethod
    def _hold() -> dict[str, Any]:
        return {
            "mode": "hold",
            "reason": "scripted hold after session check",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    def _draft(self) -> dict[str, Any]:
        # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
        return {
            "reason": "scripted web draft",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "probe / anonymously for the baseline",
                    "agent_id": WEB_AGENT,
                    "target_node_ids": [HOST_NODE],
                    "principal_refs": [],
                    "allowed_operations": [WEB_OP_KEY],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [
                        {"kind": "evidence", "description": "baseline recorded", "min_evidence": 1}
                    ],
                    "required": True,
                },
                {
                    "step_id": "step-2",
                    "objective": "re-probe / with the registered session",
                    "agent_id": WEB_AGENT,
                    "target_node_ids": [HOST_NODE],
                    "principal_refs": [self._session_ref],
                    "allowed_operations": [WEB_OP_KEY],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [
                        {
                            "kind": "evidence",
                            "description": "comparison recorded",
                            "min_evidence": 1,
                        }
                    ],
                    "required": True,
                },
            ],
        }

    def _anonymous(self) -> dict[str, Any]:
        return {
            "mode": "actions",
            "reason": "scripted anonymous probe",
            "actions": [
                {
                    "path_step_id": "step-1",
                    "agent_id": WEB_AGENT,
                    "target_node_id": HOST_NODE,
                    "principal_ref": None,
                    "capability": "web",
                    "operation": "http_request",
                    "tool_id": WEB_TOOL,
                    "arguments": {"target_ref": "app", "path": "/", "method": "GET"},
                    "reason": "record the anonymous baseline",
                    "expected_observation": "anonymous 401 baseline",
                    "hypothesis": None,
                    "existing_hypothesis_id": None,
                    "expectation": None,
                    "authentication_check": None,
                    "availability_impact": False,
                }
            ],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    def _session(self, baseline_id: str) -> dict[str, Any]:
        session_ref = self._session_ref
        return {
            "mode": "actions",
            "reason": "scripted session check",
            "actions": [
                {
                    "path_step_id": "step-2",
                    "agent_id": WEB_AGENT,
                    "target_node_id": HOST_NODE,
                    "principal_ref": session_ref,
                    "capability": "web",
                    "operation": "http_request",
                    "tool_id": WEB_TOOL,
                    "arguments": {
                        "target_ref": "app",
                        "path": "/",
                        "method": "GET",
                        "session_ref": session_ref,
                    },
                    "reason": "compare session against the anonymous baseline",
                    "expected_observation": "session response for comparison",
                    "hypothesis": (
                        {
                            "description": (
                                f"Registered session {session_ref} may access / after anonymous 401"
                            ),
                            "confidence": 0.5,
                            "violation_if_confirmed": True,
                            "metadata": {"kind": "authentication"},
                        }
                        if self._expected_access == "deny"
                        else None
                    ),
                    "existing_hypothesis_id": None,
                    "expectation": (
                        {
                            "kind": "output_contains",
                            "field": "",
                            "value": session_ref,
                            "description": (
                                "denied session response returned for comparison"
                            ),
                            "violation_if_met": True,
                        }
                        if self._expected_access == "deny"
                        else None
                    ),
                    "authentication_check": {
                        "baseline_evidence_id": baseline_id,
                        "session_ref": session_ref,
                        "expected_access": self._expected_access,
                    },
                    "availability_impact": False,
                }
            ],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route})
        if route == "path_draft":
            return self._draft()
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            # Step review keeps the old accepted/reason shape (no claims).
            return h.completion_accept()
        if route == "task_completion_review":
            # Task review: accepted/reason/claims with actual evidence ids.
            payload = self._payload(messages)
            run_id = (payload.get("path") or {}).get("run_id", "")
            records = await self._evidence.list_for_run(run_id) if run_id else []
            task_receipts = [
                str(r.evidence_id)
                for r in records
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if not task_receipts:
                for step in payload.get("path", {}).get("steps", []):
                    task_receipts.extend(step.get("evidence_ids", []))
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "web tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(task_receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts: list[str] = []
            for step in payload["path"]["steps"]:
                receipts.extend(step.get("evidence_ids", []))
            if not receipts:
                run_id = (payload.get("path") or {}).get("run_id", "")
                run_id = run_id or payload.get("active_path", {}).get("run_id", "")
                if run_id:
                    records = await self._evidence.list_for_run(run_id)
                    receipts = [
                        str(r.evidence_id)
                        for r in records
                        if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                    ]
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": c,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "web tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for c in criteria
                ],
            }
        if route == "next_action":
            self._actions += 1
            if self._actions == 1:
                return self._anonymous()
            if self._actions == 2:
                payload = self._payload(messages)
                run_id = payload["active_path"]["run_id"]
                records = await self._evidence.list_for_run(run_id)
                tool_evidence = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                ]
                assert tool_evidence, "session check needs the anonymous baseline evidence"
                return self._session(tool_evidence[0])
            return self._hold()
        raise AssertionError(f"no scripted answer for route {route!r}")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


def _auth_engagement(
    *, session_ref: str = "alice", expected_access: str = "unspecified"
) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_auth"),
        name="auth",
        scope=EngagementScope(allowed_hosts=["app.local"]),
        metadata={
            "target_refs": {"app.local": "app"},
            "auth_sessions": {
                "app.local": [
                    {"session_ref": session_ref, "paths": ["/"], "expected_access": expected_access}
                ]
            },
        },
        execution_budget=ExecutionBudget(max_actions=5),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        created_at=datetime.now(timezone.utc),
    )


def _auth_deps(
    engagement: Engagement,
    gateway: SessionGateway,
    control: SessionGate,
    *,
    session_ref: str = "alice",
    expected_access: str = "unspecified",
) -> RuntimeDeps:
    evidence = FakeEvidenceStore()
    llm = _AuthLLM(evidence, session_ref=session_ref, expected_access=expected_access)
    return RuntimeDeps(
        control_plane=control,
        tool_gateway=gateway,
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=evidence,
        llm_client=llm,
        engagement=engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        work_store=InMemoryWorkStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        operator_intent_store=None,
        settings=OrchestrationSettings(max_steps=8, checkpointing=False),
    )


@pytest.mark.parametrize(
    "status,expected",
    [(200, "session_access_observed"), (401, "session_not_accepted"), (403, "access_denied")],
)
def test_comparison_outcome_matrix(status: int, expected: str) -> None:
    assert comparison_outcome(status, "unspecified") == expected
    assert comparison_outcome(None, "unspecified") == "request_failed_or_uninterpretable"
    assert comparison_outcome(200, "deny") == "access_control_violation"


def _unit_evidence(
    store: FakeEvidenceStore,
    *,
    evidence_id: str,
    run_id: str,
    engagement_id: str,
    http_status: int | None,
    session_ref: str | None,
) -> EvidenceRecord:
    now = datetime.now(timezone.utc)
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        source_type=EvidenceSourceType.TOOL,
        source_id="web.http_request",
        action_id=ActionId("action_unit"),
        invocation_id=InvocationId("inv_unit"),
        content_type="application/json",
        digest="00" * 32,
        summary="unit baseline",
        created_at=now,
        metadata={
            "success": True,
            "target_node_id": HOST_NODE,
            "http_observation": {
                "kind": "http_response",
                "method": "GET",
                "path": "/",
                "target_ref": "app",
                "session_ref": session_ref,
                "status_code": http_status,
            },
        },
    )


@pytest.mark.parametrize("mismatch", ["missing", "path", "target_ref", "session_ref", "method"])
async def test_mismatched_baseline_or_request_cannot_confirm(mismatch: str) -> None:
    store = FakeEvidenceStore()
    now = datetime.now(timezone.utc)
    baseline = _unit_evidence(
        store, evidence_id="ev-base", run_id="run_unit", engagement_id="eng_unit",
        http_status=401, session_ref=None,
    )
    await store.append(baseline)
    current = _unit_evidence(
        store, evidence_id="ev-now", run_id="run_unit", engagement_id="eng_unit",
        http_status=200, session_ref="alice",
    )
    arguments: dict[str, Any] = {
        "target_ref": "app",
        "path": "/",
        "method": "GET",
        "session_ref": "alice",
    }
    baseline_ref = "ev-base"
    if mismatch == "missing":
        baseline_ref = "ev-gone"
    elif mismatch == "method":
        arguments = {**arguments, "method": "POST"}
    elif mismatch == "path":
        arguments = {**arguments, "path": "/different"}
    elif mismatch == "target_ref":
        arguments = {**arguments, "target_ref": "/different"}
    elif mismatch == "session_ref":
        arguments = {**arguments, "session_ref": "/different"}
    proposal = ActionProposal(
        action_id=ActionId("action_unit"),
        run_id=RunId("run_unit"),
        engagement_id=EngagementId("eng_unit"),
        agent_id=AgentId("web-api-agent"),
        capability="web",
        operation="http_request",
        target=HostTarget(host="app.local"),
        arguments=arguments,
        reason="unit",
        created_at=now,
    )
    result = ToolResult(
        invocation_id=InvocationId("inv_unit"),
        action_id=ActionId("action_unit"),
        tool_id=ToolId("web.http_request"),
        success=True,
        output={"status_code": 200},
        started_at=now,
        finished_at=now,
    )
    assessment = await assess_authentication(
        AuthenticationCheck(baseline_evidence_id=EvidenceId(baseline_ref), session_ref="alice"),
        proposal,
        result,
        current,
        store,
    )
    assert assessment.hypothesis_status == "inconclusive"
    assert assessment.outcome == "baseline_evidence_unavailable_or_mismatched"
    assert assessment.evidence_ids == ["ev-now"]


async def test_anonymous_401_then_single_gated_session_check() -> None:
    engagement = _auth_engagement()
    gateway = SessionGateway(401, 200)
    deps = _auth_deps(engagement, gateway, SessionGate(PolicyVerdict.ALLOW))
    final = await run_engagement(engagement, deps)
    assert len(gateway.invocations) == 2
    assert "session_ref" not in gateway.invocations[0].arguments
    assert gateway.invocations[1].arguments["session_ref"] == "alice"
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    events = bus.history(event_type=AUTHENTICATION_CHECKED)
    assert len(events) == 1
    assert events[0].payload["outcome"] == "session_access_observed"
    assert events[0].payload["assessment_kind"] == "observation"
    refs = events[0].payload["evidence_ids"]
    assert len(refs) == 2
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    assert set(refs) <= {str(e.evidence_id) for e in evidence}
    from redteam.attack_graph.service import AttackGraphService
    assert await AttackGraphService(deps.graph_store).list_findings() == []


async def test_session_followup_deny_blocks() -> None:
    engagement = _auth_engagement()
    gateway = SessionGateway(401, 200)
    control = SessionGate(PolicyVerdict.DENY)
    deps = _auth_deps(engagement, gateway, control)
    await run_engagement(engagement, deps)
    assert len(gateway.invocations) == 1
    assert "session_ref" not in gateway.invocations[0].arguments
    assert [p for p in control.evaluated if "session_ref" in p.arguments]
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    assert not bus.history(event_type=AUTHENTICATION_CHECKED)


async def test_denied_session_200_is_a_confirmed_access_control_finding() -> None:
    engagement = _auth_engagement(session_ref="bob", expected_access="deny")
    gateway = SessionGateway(401, 200)
    deps = _auth_deps(
        engagement, gateway, SessionGate(PolicyVerdict.ALLOW),
        session_ref="bob",
        expected_access="deny",
    )
    final = await run_engagement(engagement, deps)
    bus = deps.event_bus
    assert isinstance(bus, InMemoryEventBus)
    checked = bus.history(event_type=AUTHENTICATION_CHECKED)
    assert len(checked) == 1
    assert checked[0].payload["outcome"] == "access_control_violation"
    assert checked[0].payload["assessment_kind"] == "security_check"
    from redteam.attack_graph.service import AttackGraphService
    graph = AttackGraphService(deps.graph_store)
    findings = await graph.list_findings(verified=True)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.verified is True
    assert finding.severity == "high"
    assert len(finding.evidence_ids) == 2
    hypotheses = await graph.list_hypotheses(status="confirmed")
    assert len(hypotheses) == 1
    assert finding.metadata["hypothesis_id"] == hypotheses[0].hypothesis_id
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    refs = checked[0].payload["evidence_ids"]
    assert set(refs) <= {str(e.evidence_id) for e in evidence}
