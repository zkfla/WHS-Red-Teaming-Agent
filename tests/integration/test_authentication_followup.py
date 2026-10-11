"""Authentication follow-ups are bounded, policy-gated and observation-only.

Native-loop notes: every request below comes from explicit scripted
complete_native calls through the production InvestigationEngine path.
The scripted chain is 1 anonymous probe + 1 selected session check, then
the registered local authentication.assess over the two stored evidence
IDs, then (deny only) one explicit graph.record security claim. The model
holds afterwards; nothing runs on a rule fallback.

Retired deterministic items (with replacement):
- legacy complete_structured strategy scripting (path drafts, path reviews
  and next_action intents) -> complete_native tool calls plus the genuine
  analysis/task/goal completion reviews only.
- denied-session violation promotion via the legacy verify branch
  (deny + 200 -> access_control_violation with a security_check event):
  the local assess step stays observation-only, so AUTHENTICATION_CHECKED
  now carries assessment_kind observation even for deny + 200, and the
  verified high-severity Finding, the confirmed hypothesis and their
  linkage come from the explicit graph.record claim instead. The
  observation-only chain above stays finding-free.
- deferred/tool-missing branches via auto-candidates ->
  covered as policy/availability outcomes in test_session_followup_deny_blocks
  and the DENY control-plane cases in test_agent_loop.py.
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
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.authentication import (
    AuthenticationCheck,
    assess_authentication,
    comparison_outcome,
)
from redteam.orchestration.authentication_local_tools import (
    AUTHENTICATION_ASSESS_TOOL_ID,
    authentication_assess_descriptor,
    authentication_assess_handler,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import AUTHENTICATION_CHECKED
from redteam.orchestration.graph_recording import (
    GRAPH_RECORD_TOOL_ID,
    GraphRecordingHandler,
    graph_recording_descriptor,
)
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h

HOST_NODE = "host:app.local"
WEB_TOOL = "web.http_request"


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
                    # This gateway actually applied the requested session.
                    # Keep the request echo separate from the provider receipt.
                    "actual_session_ref": session_ref,
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


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


def _remote_executed_evidence(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("ok") is not True:
            continue
        if body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


class _AuthNativeLLM:
    """Scripted native model: anonymous probe, session check, assess, claim, hold.

    Only complete_native drives the loop. complete_structured answers the
    genuine reviews (execution meaning, posthoc claim review, task and goal
    settlement); any legacy strategy route raises so a silent JSON fallback
    can never stand in for a native turn.
    """

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
        self.reviews: list[str] = []
        self.turns = 0
        self._auth_call: str | None = None
        self._graph_call: str | None = None

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    @staticmethod
    def _wanted(tools: list[Any]) -> tuple[str, str, str]:
        names = _exposed_names(tools)
        web = exposed_tool_name(WEB_TOOL)
        assess = exposed_tool_name(AUTHENTICATION_ASSESS_TOOL_ID)
        record = exposed_tool_name(GRAPH_RECORD_TOOL_ID)
        assert web in names, "native model needs the registered web request tool"
        assert assess in names, "native model needs the registered authentication assess tool"
        assert record in names, "native model needs the registered graph recording tool"
        return web, assess, record

    def _record(self, names: list[str]) -> None:
        self.calls.append({"turn": self.turns, "tool_calls": list(names)})

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.reviews.append(route)
        if route == "analysis_verify":
            result = h.meaning("success", True)
            if "hypothesis_outcome" in response_schema.get("properties", {}):
                # Posthoc claim review (graph.record finding): the denied
                # session actually returned 200 after an anonymous 401, so
                # the referenced hypothesis is confirmed by this evidence.
                result["hypothesis_outcome"] = "success"
            return result
        if route == "task_completion_review":
            payload = self._payload(messages)
            receipts = list(payload["verified_source_ids"])
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
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
        if route == "goal_completion_review":
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts = list(payload["verified_source_ids"])
            assert receipts, "goal review needs the verified executed call evidence"
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
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        web, assess, record = self._wanted(tools)
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self._record([web])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-anon",
                        name=web,
                        arguments={"target_ref": "app", "path": "/", "method": "GET"},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            assert evidenced, "session check needs the anonymous baseline evidence"
            self._record([web])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-sess",
                        name=web,
                        arguments={
                            "target_ref": "app",
                            "path": "/",
                            "method": "GET",
                            "session_ref": self._session_ref,
                        },
                    ),
                ),
                text="",
            )
        anon = (_tool_result(messages, "call-anon") or {}).get("evidence_ids") or []
        sess = (_tool_result(messages, "call-sess") or {}).get("evidence_ids") or []
        if not anon or not sess:
            # The session call never executed (Control denied it): hold
            # without an assessment so no AUTHENTICATION_CHECKED event exists.
            return NativeCompletion(tool_calls=(), text="")
        if self._auth_call is None:
            self._auth_call = "call-auth"
            self._record([assess])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._auth_call,
                        name=assess,
                        arguments={
                            "baseline_evidence_id": anon[0],
                            "session_evidence_id": sess[0],
                            "session_ref": self._session_ref,
                            "expected_access": self._expected_access,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._auth_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._expected_access == "deny" and self._graph_call is None:
            self._graph_call = "call-graph"
            # The claim concerns the / resource reached by the session: the
            # target comes from the session evidence's own preserved
            # metadata, which now carries the measured host->endpoint
            # relation the mapper records for both evidences.
            session = await self._evidence.get(EvidenceId(sess[0]))
            metadata = getattr(session, "metadata", None) or {}
            target_id = metadata.get("target_node_id")
            assert isinstance(target_id, str) and target_id.strip(), (
                "graph claim needs the session evidence target"
            )
            self._record([record])
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._graph_call,
                        name=record,
                        arguments={
                            "findings": [
                                {
                                    "statement": (
                                        f"Registered session {self._session_ref} accessed / "
                                        "after anonymous 401 though expected deny"
                                    ),
                                    "evidence_refs": [anon[0], sess[0]],
                                    "target_node_id": target_id,
                                    "severity": "high",
                                }
                            ]
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(authentication_assess_descriptor(), authentication_assess_handler)
    registry.register(graph_recording_descriptor(), GraphRecordingHandler())
    return registry


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
    llm = _AuthNativeLLM(evidence, session_ref=session_ref, expected_access=expected_access)
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
        local_tools=_native_tool_registry(),
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
    # The local assess step stays observation-only; the security claim itself
    # travels through the explicit graph.record finding proven below.
    assert checked[0].payload["assessment_kind"] == "observation"
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
