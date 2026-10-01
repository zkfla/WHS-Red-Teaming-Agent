"""#2: a session that reaches an endpoint is modelled as a Principal and a
CAN_ACCESS edge (Object/Permission), so who-can-reach-what is first-class in
the graph rather than buried in an observation blob.
"""

from __future__ import annotations

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
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import EngagementId
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.integration.test_authentication_followup import SessionGate, SessionGateway


class _AccessLLM:
    """Explicit plan: anonymous baseline then registered-session check."""

    def __init__(
        self,
        graph_store: InMemoryAttackGraphStore,
        evidence_store: FakeEvidenceStore,
    ) -> None:
        self._graph = graph_store
        self._evidence = evidence_store
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "path_id" in props and "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        return ""

    async def _host_id(self) -> str:
        nodes = await self._graph.list_nodes()
        for n in nodes:
            if str(getattr(n, "id", "")) == "host:app.local":
                return str(n.id)
        for n in nodes:
            t = getattr(n, "type", None)
            tv = getattr(t, "value", t)
            if tv == "host":
                return str(n.id)
        return "host:app.local"

    def _draft(self, host_id: str) -> dict[str, Any]:
        crit = {"kind": "evidence", "description": "tool evidence recorded", "min_evidence": 1}
        return {
            "path_id": "path-access-1",
            "run_id": "run-x",
            "mission_id": None,
            "version": 1,
            "parent_version": None,
            "status": "draft",
            "reason": "scripted session access plan",
            "goal": "observe registered session access",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "record anonymous baseline",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": [],
                    "allowed_operations": ["web.http_request"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [crit],
                    "required": True,
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                },
                {
                    "step_id": "step-2",
                    "objective": "check registered session",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": ["alice"],
                    "allowed_operations": ["web.http_request"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [crit],
                    "required": True,
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                },
            ],
            "hypothesis_ids": [],
            "evidence_ids": [],
            "changed_step_ids": [],
            "review_records": {},
        }

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        system: str | None = None,
        num_retries: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        import json as _json

        route = self._route_key(response_schema or {})
        self.calls.append({"route": route})
        if route == "path_draft":
            return self._draft(await self._host_id())
        if route == "path_review":
            return {"decision": "accept", "reason": "scripted accept", "path": None}
        if route == "completion_review":
            return {"accepted": True, "reason": "scripted accept"}
        if route == "goal_completion_review":
            assert messages is not None
            payload = _json.loads(messages[0]["content"])
            criteria = payload.get("mission", {}).get("success_criteria", []) or ["observe session"]
            receipts = []
            for st in payload.get("path", {}).get("steps", []):
                receipts.extend(st.get("evidence_ids", []))
            if not receipts:
                recs = await self._evidence.list_for_run(
                    payload.get("active_path", {}).get("run_id", "")
                )
                receipts = [str(r.evidence_id) for r in recs[:4]]
            return {
                "accepted": True,
                "reason": "scripted goal accept",
                "criteria": [
                    {"criterion": c, "met": True, "evidence_ids": list(receipts) or ["ev-dummy"]}
                    for c in criteria
                ],
            }
        if route == "analysis_verify":
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "scripted",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "next_action":
            assert messages is not None
            payload = _json.loads(messages[0]["content"])
            run_id = (payload.get("active_path") or {}).get("run_id", "")
            host_id = await self._host_id()
            recs = await self._evidence.list_for_run(run_id) if run_id else []
            tool_recs = [
                r for r in recs if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if len(tool_recs) == 0:
                return {
                    "mode": "actions",
                    "reason": "scripted anonymous baseline",
                    "actions": [
                        {
                            "path_step_id": "step-1",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "web",
                            "operation": "http_request",
                            "tool_id": "web.http_request",
                            "arguments": {"target_ref": "app.local", "path": "/", "method": "GET"},
                            "reason": "baseline",
                            "expected_observation": "401 baseline",
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
            if len(tool_recs) == 1:
                base = str(tool_recs[0].evidence_id)
                return {
                    "mode": "actions",
                    "reason": "scripted session check",
                    "actions": [
                        {
                            "path_step_id": "step-2",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": "alice",
                            "capability": "web",
                            "operation": "http_request",
                            "tool_id": "web.http_request",
                            "arguments": {
                                "target_ref": "app.local",
                                "path": "/",
                                "method": "GET",
                                "session_ref": "alice",
                            },
                            "reason": "session access check",
                            "expected_observation": "session reaches endpoint",
                            "hypothesis": None,
                            "existing_hypothesis_id": None,
                            "expectation": None,
                            "authentication_check": {
                                "baseline_evidence_id": base,
                                "session_ref": "alice",
                                "expected_access": "unspecified",
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
            return {
                "mode": "hold",
                "reason": "scripted access observed",
                "actions": [],
                "path": None,
                "path_step_id": None,
                "completion": {},
                "evidence_request": {},
                "hypothesis_update": {},
            }
        raise AssertionError(f"no scripted answer for {route}")


async def test_session_access_becomes_a_principal_and_can_access_edge() -> None:
    # No expected_access declared -> benign "session_access_observed" (200),
    # not a Finding, but still a real access relationship worth recording.
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
    llm = _AccessLLM(graph_store=graph_store, evidence_store=evidence_store)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        control_plane=control,
        settings=OrchestrationSettings(max_steps=30),
        agents=default_registry(),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    await run_engagement(engagement, deps=deps)
    graph = AttackGraphService(deps.graph_store)

    principals = [n for n in await graph.list_nodes() if n.type.value == "principal"]
    assert any("alice" in str(p.id) for p in principals), "the session became a Principal"

    can_access = [e for e in await graph.list_edges() if e.type.value == "can_access"]
    assert can_access, "the reached endpoint must be linked as CAN_ACCESS"
    assert can_access[0].properties.get("outcome") == "session_access_observed"
    # No verified Finding for an undeclared session simply having access.
    assert await graph.list_findings() == []
