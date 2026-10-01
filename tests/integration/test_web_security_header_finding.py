"""Real MCP schemas, changing responses and evidence-backed observation boundaries.

Migration: retired auto-execution assumption. Explicit scripted model drives
recon then web; no rule/planner restoration, no execution-guard monkeypatch.
Preserved: actual response kept, observation-only boundary, no findings
invented, evidence-backed provenance. Retired: exact 2-evidence count (now
tool evidences filtered), exact no_actionable_plan ending (now goal close).
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest
from mcp import Client

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.servers import recon, web_http
from redteam.capability.servers.config import ServerPolicy
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, ToolHealthStatus, Verdict
from redteam.core.ids import EngagementId, ToolId
from redteam.orchestration.analysis import VerificationAssessment
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h


class ChangingResponses(DummyToolGateway):
    def __init__(self, status, header):
        super().__init__()
        self.status = status
        self.header = header

    async def invoke(self, invocation):
        result = await super().invoke(invocation)
        is_web = invocation.tool_id == "web.http_request"
        status = self.status if is_web else 200
        output = {
            "target_ref": invocation.arguments["target_ref"],
            "status_code": status,
            "body_bytes": 0,
            "body_sha256": "a" * 64,
            "redirect": 300 <= status < 400,
            "security_headers_present": {},
            "body_omitted": True,
        }
        if is_web:
            output["session_ref"] = invocation.arguments.get("session_ref")
        if self.header is not None:
            output["security_headers_present"] = {"strict-transport-security": self.header}
        return result.model_copy(update={"output": output})


class OverconfidentVerifier:
    async def verify(self, request):
        return VerificationAssessment(
            verdict=Verdict.VIOLATION if request.proposal.capability == "web" else Verdict.PASS,
            reason="should not promote an observation",
            reviewer_id="test",
        )


class _HeaderLLM(h.ScriptedLLM):
    def __init__(self) -> None:
        super().__init__()
        self._graph: Any = None
        self._evidence: Any = None
        self._next_calls = 0

    async def _host_id(self) -> str:
        nodes = await self._graph.list_nodes()
        for n in nodes:
            if n.type is GraphNodeType.HOST and str(n.id) == "host:mock-bank":
                return str(n.id)
        host = next(n for n in nodes if n.type is GraphNodeType.HOST)
        return str(host.id)

    @staticmethod
    def _payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(messages[0]["content"])

    def _draft(self, host_id: str) -> dict[str, Any]:
        def _crit() -> dict[str, Any]:
            return {"kind": "evidence", "description": "result", "min_evidence": 1}

        def _mk(sid: str, agent: str, op: str) -> dict[str, Any]:
            return {
                "step_id": sid,
                "objective": "observe " + sid,
                "agent_id": agent,
                "target_node_ids": [host_id],
                "principal_refs": [],
                "allowed_operations": [op],
                "depends_on": [] if sid == "step-1" else ["step-1"],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [_crit()],
                "required": True,
                "status": "pending",
                "outcome": None,
                "evidence_ids": [],
            }

        return {
            "path_id": "path-draft-header",
            "run_id": "run-x",
            "mission_id": None,
            "version": 1,
            "parent_version": None,
            "status": "draft",
            "reason": "scripted header draft",
            "goal": "observe headers",
            "steps": [
                _mk("step-1", "recon-agent", "recon.service_discovery"),
                _mk("step-2", "web-api-agent", "web.http_request"),
            ],
            "hypothesis_ids": [],
            "evidence_ids": [],
            "changed_step_ids": [],
            "review_records": {},
        }

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return self._draft(await self._host_id())
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "completion_review":
            return h.completion_accept()
        if route == "goal_completion_review":
            payload = self._payload(messages)
            criteria = payload["mission"]["success_criteria"]
            receipts: list[str] = []
            for step in payload["path"]["steps"]:
                receipts.extend(step.get("evidence_ids", []))
            return h.goal_accept(
                [{"criterion": c, "met": True, "evidence_ids": list(receipts)} for c in criteria]
            )
        if route == "next_action":
            self._next_calls += 1
            host_id = await self._host_id()
            if self._next_calls == 1:
                return {
                    "mode": "actions",
                    "reason": "recon",
                    "actions": [
                        {
                            "path_step_id": "step-1",
                            "agent_id": "recon-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "recon",
                            "operation": "service_discovery",
                            "tool_id": "recon.http_probe",
                            "arguments": {"target_ref": "bank-lab"},
                            "reason": "probe",
                            "expected_observation": "host answers",
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
            if self._next_calls == 2:
                payload = self._payload(messages)
                run_id = payload["active_path"]["run_id"]
                records = await self._evidence.list_for_run(run_id)
                tool_ids = [
                    str(r.evidence_id)
                    for r in records
                    if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
                ]
                assert tool_ids, "need recon evidence"
                return {**h.turn_complete_step([tool_ids[0]]), "path_step_id": "step-1"}
            if self._next_calls == 3:
                return {
                    "mode": "actions",
                    "reason": "web",
                    "actions": [
                        {
                            "path_step_id": "step-2",
                            "agent_id": "web-api-agent",
                            "target_node_id": host_id,
                            "principal_ref": None,
                            "capability": "web",
                            "operation": "http_request",
                            "tool_id": "web.http_request",
                            "arguments": {"target_ref": "bank-lab", "path": "/"},
                            "reason": "inspect",
                            "expected_observation": "http answers",
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
            payload = self._payload(messages)
            run_id = payload["active_path"]["run_id"]
            records = await self._evidence.list_for_run(run_id)
            tool_ids = [
                str(r.evidence_id)
                for r in records
                if isinstance(getattr(r, "metadata", {}).get("tool_result"), dict)
            ]
            if self._next_calls == 4:
                assert len(tool_ids) >= 2, "need web evidence"
                return {**h.turn_complete_step([tool_ids[1]]), "path_step_id": "step-2"}
            return h.turn_complete_goal()
        raise AssertionError(f"no header answer for {route!r}")

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        return ""


@pytest.mark.parametrize("status,header", [(200, False), (401, False), (403, True), (200, None)])
@pytest.mark.parametrize("fanout", [1, 2])
async def test_web_observation_keeps_actual_response_and_evidence(status, header, fanout):
    policy = ServerPolicy.model_validate(
        {
            "targets": {"bank-lab": {"origin": "http://mock-bank:8080", "allowed_paths": ["/"]}},
            "allowed_networks": ["127.0.0.0/8"],
        }
    )
    registry = MCPToolRegistry()
    bindings = {}
    for namespace, server in [
        ("recon", recon.create_server(policy)),
        ("web", web_http.create_server(policy)),
    ]:
        async with Client(server) as client:
            listing = await client.list_tools()
        for tool in listing.tools:
            if tool.name not in {"http_probe", "dns_resolve", "http_request"}:
                continue
            tool_id = f"{namespace}.{tool.name}"
            registry.register(
                ToolDescriptor(
                    tool_id=ToolId(tool_id),
                    server_id=namespace,
                    tool_name=tool.name,
                    input_schema=tool.input_schema,
                    capability_tags=[namespace],
                    health_status=ToolHealthStatus.HEALTHY,
                )
            )
            bindings[tool_id] = ["service_discovery" if namespace == "recon" else "http_request"]
    registry.register(
        ToolDescriptor(
            tool_id=ToolId("web.browser_navigate"),
            server_id="browser",
            tool_name="browser_navigate",
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string"}},
            },
            capability_tags=["web"],
            health_status=ToolHealthStatus.HEALTHY,
        )
    )
    bindings["web.browser_navigate"] = ["http_request"]
    catalog = MCPToolGateway(registry, {}, bindings)
    engagement = Engagement(
        engagement_id=EngagementId("eng_observation"),
        name="observation",
        scope=EngagementScope(allowed_hosts=["mock-bank"]),
        metadata={"target_refs": {"mock-bank": "bank-lab"}},
        execution_budget=ExecutionBudget(max_actions=10),
        created_at=datetime.now(timezone.utc),
        approval_policy=ApprovalPolicy(),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
    )
    gateway = ChangingResponses(status, header)
    llm = _HeaderLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=catalog,
        llm_client=llm,
        verifier=OverconfidentVerifier(),
        settings=OrchestrationSettings(max_steps=20, max_actions_per_turn=fanout),
    )
    # Production wires the engagement onto deps (alias resolution for mapped
    # refs); the fixture helper leaves it empty, which rejects mapped refs.
    deps = replace(deps, engagement=engagement)
    llm._graph = deps.graph_store
    llm._evidence = deps.evidence_store
    final = await run_engagement(engagement, deps=deps)
    got = [str(i.tool_id) for i in gateway.invocations]
    assert got[:2] == ["recon.http_probe", "web.http_request"]
    assert all(i.arguments["target_ref"] == "bank-lab" for i in gateway.invocations)
    assert all("message" not in i.arguments for i in gateway.invocations)
    # Retired: exact UNKNOWN + no_actionable_plan assumed auto stop.
    # Preserved: observation never promoted to violation despite overconfident verifier.
    assert final.last_verdict in (Verdict.UNKNOWN, Verdict.PASS)
    service = AttackGraphService(deps.graph_store)
    assert await service.list_findings() == []
    node = await service.get_node(endpoint_node_id("GET", "/", host="mock-bank"))
    assert node is not None
    assert attrs_of(node.properties)["status_code"] == status
    assert facts_of(node.properties)["assessment_kind"] == "observation"
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    tool_evidence = [
        e for e in evidence if isinstance(getattr(e, "metadata", {}).get("tool_result"), dict)
    ]
    assert len(tool_evidence) >= 2
    evidence_ids = {str(e.evidence_id) for e in tool_evidence}
    assert set(facts_of(node.properties).get("evidence_ids", [])) <= evidence_ids
    assert all(
        edge.properties["provenance"][0]["evidence_id"] in evidence_ids
        for edge in await service.list_edges()
        if edge.type.value in {"hosts", "exposes"}
    )
    observed = deps.event_bus.history(event_type="ObservationRecorded")
    assert len(observed) == 2
    assert {event.payload["action_id"] for event in observed} == {
        str(invocation.action_id) for invocation in gateway.invocations
    }
    assert observed[0].payload["evidence_id"] in evidence_ids
