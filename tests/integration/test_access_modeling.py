"""#2: a session that reaches an endpoint is modelled as a Principal and a
CAN_ACCESS edge (Object/Permission), so who-can-reach-what is first-class in
the graph rather than buried in an observation blob.

Native-loop version: every turn is an explicit scripted native completion
through the production MODEL -> TOOLS -> SETTLE path. The scripted model
answers complete_native only (plus the genuine verification and settlement
reviews); any legacy strategy route raises instead of returning a soft answer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import EngagementId, ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
    goal_settle_descriptor,
    goal_settle_handler,
    task_settle_descriptor,
    task_settle_handler,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from tests.fakes import (
    DummyToolGateway,
    FakeControlPlane,
    FakeEvidenceStore,
    build_test_application,
)

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
            item = str(identity)
            if item and item not in found:
                found.append(item)
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


class _AccessNativeLLM:
    """Native script: anonymous baseline, then one registered-session check."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
        self.last_messages: list[dict[str, Any]] = []
        self._task_call: str | None = None
        self._goal_call: str | None = None

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

    async def complete(
        self, *, messages: list[dict[str, Any]] | None = None, system: str | None = None, **_: Any
    ) -> str:
        raise AssertionError("native loop must not use free-text completion")

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
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "scripted meaning",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "session access tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            criteria = payload.get("mission", {}).get("success_criteria", []) or ["observe session"]
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "goal review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {
                        "criterion": item,
                        "met": True,
                        "evidence_ids": list(receipts),
                        "claims": [
                            {
                                "description": "session access tool result observed",
                                "kind": "observation",
                                "evidence_ids": list(receipts),
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for item in criteria
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
        names = _exposed_names(tools)
        web = exposed_tool_name(WEB_TOOL)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert web in names, "native model needs the registered web request tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.last_messages = list(messages)
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self.calls.append({"turn": self.turns, "tool_calls": [web]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-anon-1",
                        name=web,
                        arguments={"target_ref": "app.local", "path": "/", "method": "GET"},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            assert evidenced, "session check needs the anonymous baseline evidence"
            self.calls.append({"turn": self.turns, "tool_calls": [web]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-session-1",
                        name=web,
                        arguments={
                            "target_ref": "app.local",
                            "path": "/",
                            "method": "GET",
                            "session_ref": "alice",
                        },
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert len(evidenced) >= 2, "task settlement needs both executed call evidences"
            self._task_call = "call-task-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "session access evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-goal-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "session access evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


async def test_session_access_becomes_a_principal_and_can_access_edge() -> None:
    # No expected_access declared -> benign observed access (200), not a
    # Finding, but still a real access relationship worth recording.
    engagement = Engagement(
        engagement_id=EngagementId("eng_auth"),
        name="auth",
        scope=EngagementScope(allowed_hosts=["app.local"]),
        metadata={
            "target_refs": {"app.local": "app"},
            "auth_sessions": {"app.local": [{"session_ref": "alice", "paths": ["/"]}]},
        },
        execution_budget=ExecutionBudget(max_actions=10),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        created_at=datetime.now(timezone.utc),
    )
    gateway = SessionGateway(401, 200)
    control = SessionGate(PolicyVerdict.ALLOW)
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FakeEvidenceStore()
    llm = _AccessNativeLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        control_plane=control,
        settings=OrchestrationSettings(
            max_steps=20, checkpointing=False, decision_max_attempts=6
        ),
        agents=default_registry(),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    deps.local_tools.register(task_settle_descriptor(), task_settle_handler)
    deps.local_tools.register(goal_settle_descriptor(), goal_settle_handler)
    final = await run_engagement(engagement, deps=deps)
    assert final.termination_reason == TerminationReason.GOAL_ACHIEVED
    graph = AttackGraphService(deps.graph_store)

    # Both real tool calls ran: anonymous baseline first, then the session.
    assert [str(inv.tool_id) for inv in gateway.invocations] == [WEB_TOOL, WEB_TOOL]
    assert "session_ref" not in gateway.invocations[0].arguments
    assert gateway.invocations[1].arguments.get("session_ref") == "alice"

    principals = [n for n in await graph.list_nodes() if n.type.value == "principal"]
    assert any("alice" in str(p.id) for p in principals), "the session became a Principal"

    can_access = [e for e in await graph.list_edges() if e.type.value == "can_access"]
    assert can_access, "the reached endpoint must be linked as CAN_ACCESS"
    alice_ids = {str(p.id) for p in principals if "alice" in str(p.id)}
    linked = [e for e in can_access if str(e.source_id) in alice_ids]
    assert linked, "the CAN_ACCESS edge must start from the alice Principal"
    # Native mapper grounds the edge in the observed 200: a benign observed
    # success, not a violation. The legacy verify outcome field is retired
    # with the strategy loop; status_code + basis carry the same contract.
    props = linked[0].properties or {}
    assert props.get("status_code") == 200
    assert props.get("basis") == "observed_http_success"
    # No verified Finding for an undeclared session simply having access.
    assert await graph.list_findings() == []
    # Settlement went through the genuine task/goal reviews.
    assert {"task_completion_review", "goal_completion_review"} <= set(llm.reviews)
