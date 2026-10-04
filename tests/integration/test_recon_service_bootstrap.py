"""H1 end to end: SERVICE with no ENDPOINT -> Recon recovers -> Web runs.

The starting graph is the state the plan calls stalled: the host probe has
already been attempted (so the playbook's own step is retired and will never
run again), a service is known, and no endpoint was ever produced. Before
this change nothing downstream could proceed, because
WEB_API_AGENT.inspect-http-service cannot fill a path on a ServiceTarget.

This test drives that state through the real loop: candidate -> Control
Plane -> tool call -> Result Mapper -> ENDPOINT -> Web/API reactivation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import host_node_id, service_node_id
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
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application
from tests.fakes.evidence_store import FakeEvidenceStore

pytestmark = pytest.mark.asyncio


class _BootstrapLLM:
    """Explicit plan for stalled-service recovery: recon probe then web."""

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
            s = str(getattr(n, "id", ""))
            if s == "host:a.local":
                return s
        for n in nodes:
            t = getattr(n, "type", None)
            tv = getattr(t, "value", t)
            if tv == "host":
                return str(n.id)
        return "host:a.local"

    def _draft(self, host_id: str) -> dict[str, Any]:
        # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
        crit = {"kind": "evidence", "description": "tool evidence recorded", "min_evidence": 1}
        return {
            "reason": "scripted recovery plan",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "probe stalled host",
                    "agent_id": "recon-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": [],
                    "allowed_operations": ["recon.service_discovery"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [crit],
                    "required": True,
                },
                {
                    "step_id": "step-2",
                    "objective": "inspect recovered surface",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [host_id],
                    "principal_refs": [],
                    "allowed_operations": ["web.http_request"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [crit],
                    "required": True,
                },
            ],
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
            # Step review keeps the old accepted/reason shape (no claims).
            return {"accepted": True, "reason": "scripted accept"}
        if route == "task_completion_review":
            # Task review: accepted/reason/claims with actual evidence ids.
            assert messages is not None
            payload = _json.loads(messages[0]["content"])
            run_id = (payload.get("path") or {}).get("run_id", "")
            run_id = run_id or payload.get("active_path", {}).get("run_id", "")
            recs = await self._evidence.list_for_run(run_id) if run_id else []
            tool_ids = [
                str(r.evidence_id)
                for r in recs
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            receipts = list(tool_ids)
            for st in (payload.get("path") or {}).get("steps", []):
                receipts.extend(st.get("evidence_ids", []))
            receipts = list(dict.fromkeys(receipts))
            return {
                "accepted": True,
                "reason": "scripted accept",
                "claims": [
                    {
                        "description": "recon tool result observed",
                        "kind": "observation",
                        "evidence_ids": receipts,
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            assert messages is not None
            payload = _json.loads(messages[0]["content"])
            criteria = payload.get("mission", {}).get("success_criteria", []) or [
                "map the in-scope host"
            ]
            receipts = []
            for st in payload.get("path", {}).get("steps", []):
                receipts.extend(st.get("evidence_ids", []))
            if not receipts:
                active_id = payload.get("active_path", {}).get("run_id", "")
                path_id = (payload.get("path") or {}).get("run_id", "")
                recs = await self._evidence.list_for_run(active_id or path_id)
                receipts = [
                    str(r.evidence_id)
                    for r in recs
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
                                "description": "recon tool result observed",
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
                    "reason": "scripted recon probe",
                    "actions": [
                        {
                            "path_step_id": "step-1",
                            "agent_id": "recon-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "recon",
                            "operation": "service_discovery",
                            "tool_id": "recon.http_probe",
                            "arguments": {"target_ref": "a.local", "path": "/"},
                            "reason": "probe stalled host",
                            "expected_observation": "probe returns",
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
                return {
                    "mode": "actions",
                    "reason": "scripted web followup",
                    "actions": [
                        {
                            "path_step_id": "step-2",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "web",
                            "operation": "http_request",
                            "tool_id": "web.http_request",
                            "arguments": {"target_ref": "a.local", "path": "/", "method": "GET"},
                            "reason": "inspect recovered endpoint",
                            "expected_observation": "endpoint answers",
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
            return {
                "mode": "hold",
                "reason": "scripted recovery done",
                "actions": [],
                "path": None,
                "path_step_id": None,
                "completion": {},
                "evidence_request": {},
                "hypothesis_update": {},
            }
        raise AssertionError(f"no scripted answer for {route}")


_HTTP_PROBE_OUTPUT = {
    "target_ref": "a.local",
    "status_code": 200,
    "body_bytes": 10,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
}


def _engagement(*hosts: str) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_bootstrap"),
        name="bootstrap",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _gateway() -> DummyToolGateway:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="test",
        tool_name="http_probe",
        operations=['service_discovery', 'endpoint_discovery'],
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    web_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="test",
        tool_name="http_request",
        operations=['http_request'],
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "path": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "session_ref": {"type": "string"},
            },
            "required": ["target_ref", "path"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    return DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, web_tool])


async def _seed_stalled_service(graph: AttackGraphService) -> None:
    """HOST + HTTP SERVICE, no ENDPOINT, host probe already retired."""

    await graph.upsert_node(
        GraphNodeType.HOST,
        node_id=host_node_id("a.local"),
        attrs={"in_scope": True},
        facts={
            "target_ref": "a.local",
            "attempted_operations": ["recon.service_discovery"],
        },
    )
    await graph.upsert_node(
        GraphNodeType.SERVICE,
        node_id=service_node_id("http", host="a.local"),
        attrs={"responded": True},
        facts={
            "host": "a.local",
            "application_protocol": "http",
            "target_ref": "a.local",
        },
    )


async def test_a_stalled_service_only_graph_is_recovered_and_web_runs() -> None:
    gateway = _gateway()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FakeEvidenceStore()
    llm = _BootstrapLLM(graph_store=graph_store, evidence_store=evidence_store)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    graph = AttackGraphService(deps.graph_store)
    await _seed_stalled_service(graph)

    final = await run_engagement(_engagement("a.local"), deps=deps)

    # 1. The existing catalog-bound probe was the thing that ran. Its path is
    #    the tool's own default, not an invented one.
    probes = [item for item in gateway.invocations if str(item.tool_id) == "recon.http_probe"]
    assert probes, "the bootstrap never reached a real probe"
    assert probes[0].arguments == {"target_ref": "a.local", "path": "/"}

    # 2. The existing Result Mapper turned that into an ENDPOINT.
    endpoints = [node for node in await graph.list_nodes() if node.type is GraphNodeType.ENDPOINT]
    assert endpoints, "the probe did not produce an endpoint"

    # 3. Web/API then used that endpoint, which is the whole point of H1.
    web_calls = [item for item in gateway.invocations if str(item.tool_id) == "web.http_request"]
    assert web_calls, "Web/API Agent never ran against the recovered endpoint"
    assert final.actions_by_agent.get("recon-agent", 0) >= 1
    assert final.actions_by_agent.get("web-api-agent", 0) >= 1


async def test_the_bootstrap_does_not_repeat_once_the_endpoint_exists() -> None:
    # Same recovery, then confirm the recovered state is stable: the endpoint
    # existing is what stops the bootstrap, so it cannot loop.
    gateway = _gateway()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FakeEvidenceStore()
    llm = _BootstrapLLM(graph_store=graph_store, evidence_store=evidence_store)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        settings=OrchestrationSettings(max_steps=20),
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
    )
    graph = AttackGraphService(deps.graph_store)
    await _seed_stalled_service(graph)

    await run_engagement(_engagement("a.local"), deps=deps)

    bootstraps = [
        item
        for item in gateway.invocations
        if str(item.tool_id) == "recon.http_probe" and item.arguments.get("path") == "/"
    ]
    assert len(bootstraps) == 1
