"""Scripted harness for the engine LLM-flow integration tests.

Helper module only (no test_ functions, so pytest never collects it).
Every stand-in model answers through the real structured-decision client,
so the engine under test always follows its production LLM path. Nothing
here touches the network, the clock, or the existing tests/fakes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from redteam.attack_graph.service import AttackGraphService
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
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.contracts.mission import Provenance as MissionProvenance
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import EvidenceSourceType, RiskLevel, TaskStatus, ToolHealthStatus, Verdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    HypothesisId,
    InvocationId,
    MissionId,
    RunId,
    TaskId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.analysis import (
    VerificationAssessment,
    VerificationOutcome,
    append_review_receipt,
)
from redteam.orchestration.observations import default_result_mapper_registry
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore

HOST = "host-01"
RECON_AGENT = "recon-agent"
INVENTORY_TOOL = "recon.inventory"
CANONICAL_OP = "recon.service_discovery"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScriptedLLM:
    """Answer each structured call from a per-route script, in order."""

    def __init__(self) -> None:
        self._routes: dict[str, list[Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def add(self, route: str, response: Any) -> None:
        self._routes.setdefault(route, []).append(response)

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

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        num_retries: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        queue = self._routes.get(route, [])
        if not queue:
            raise AssertionError(f"no scripted response left for route {route!r}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def complete(
        self, *, messages: list[dict[str, Any]], system: str | None = None, **_: Any
    ) -> str:
        return ""


class AlwaysInvalidLLM:
    """Always answer off-schema so the decision budget is spent, never filled."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return {"bogus": True}

    async def complete(self, **_: Any) -> str:
        return ""


class FakeEvidenceStore:
    """In-memory evidence port double owned by this harness."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    async def append(self, evidence: EvidenceRecord) -> None:
        self._records[str(evidence.evidence_id)] = evidence

    async def get(self, evidence_id: Any) -> EvidenceRecord | None:
        return self._records.get(str(evidence_id))

    async def list_for_run(self, run_id: Any) -> list[EvidenceRecord]:
        return [r for r in self._records.values() if str(r.run_id) == str(run_id)]

    async def list_for_action(self, action_id: Any) -> list[EvidenceRecord]:
        return [r for r in self._records.values() if str(r.action_id) == str(action_id)]


class FakeCatalog:
    """Read-only tool catalog double owned by this harness."""

    def __init__(self, tools: list[ToolDescriptor]) -> None:
        self._tools = {str(t.tool_id): t for t in tools}

    def get(self, tool_id: Any) -> ToolDescriptor | None:
        return self._tools.get(str(tool_id))

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [t for t in self._tools.values() if capability_tag in t.capability_tags]

    def list_tools_for_action(self, capability: str, operation: str) -> list[ToolDescriptor]:
        return [
            t
            for t in self._tools.values()
            if capability in t.capability_tags and operation in t.operations
        ]


def inventory_descriptor() -> ToolDescriptor:
    """The playbook never emits this tool; the catalog exposes it."""
    return ToolDescriptor(
        tool_id=ToolId(INVENTORY_TOOL),
        server_id="test-harness",
        tool_name="inventory",
        description="harness-only recon inventory tool, absent from every playbook",
        input_schema={
            "type": "object",
            "properties": {"host": {"type": "string"}, "target_ref": {"type": "string"}},
            "additionalProperties": True,
        },
        operations=["service_discovery"],
        capability_tags=["recon"],
        target_types=[],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.UNKNOWN,
    )


def make_engagement(host: str = HOST, *, engagement_id: str = "eng-t1") -> Engagement:
    return Engagement(
        engagement_id=EngagementId(engagement_id),
        name="engine llm harness",
        scope=EngagementScope(allowed_hosts=[host]),
        rules_of_engagement=[],
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30, max_concurrent_actions=2),
        approval_policy=ApprovalPolicy(),
        created_at=utcnow(),
        metadata={"target_refs": {host: host}},
    )


async def seed_host(run_id: str, host: str = HOST) -> SimpleNamespace:
    """Seed one in-scope host through the production seeding path."""
    store = InMemoryAttackGraphStore()
    service = AttackGraphService(store)
    engagement = make_engagement(host)
    nodes = await seed_from_engagement(service, engagement, run_id=RunId(run_id), step=0)
    return SimpleNamespace(
        service=service,
        store=store,
        engagement=engagement,
        node_id=str(nodes[0].id),
        host=host,
    )


def make_world(
    llm: Any, *, graph_store: Any | None = None, decision_max_attempts: int = 3
) -> SimpleNamespace:
    evidence = FakeEvidenceStore()
    store = graph_store if graph_store is not None else InMemoryAttackGraphStore()
    catalog = FakeCatalog([inventory_descriptor()])
    tasks = InMemoryTaskGraphStore()
    work = InMemoryWorkStore()
    paths = InMemoryPathStore()
    settings = OrchestrationSettings(decision_max_attempts=decision_max_attempts)
    deps = SimpleNamespace(
        event_bus=InMemoryEventBus(),
        llm_client=llm,
        settings=settings,
        evidence_store=evidence,
        graph_store=store,
        path_store=paths,
        tool_catalog=catalog,
        agents=default_registry(),
        work_store=work,
        task_graph_store=tasks,
        engagement=None,
        operator_intent_store=None,
        verifier=None,
        result_mappers=default_result_mapper_registry(),
    )
    return SimpleNamespace(
        deps=deps,
        evidence=evidence,
        graph_store=store,
        catalog=catalog,
        tasks=tasks,
        work=work,
        paths=paths,
        settings=settings,
    )


def make_runtime(
    run_id: str,
    engagement_id: str,
    *,
    goal: str = "map the in-scope host",
    mission_id: str | None = None,
    task_id: str | None = None,
) -> RuntimeState:
    return RuntimeState(
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        mission_id=MissionId(mission_id) if mission_id else None,
        active_task_id=TaskId(task_id) if task_id else None,
        current_goal=goal,
        step_count=0,
        remaining_budget=10,
    )


def draft_path(
    node_id: str, *, kind: str = "evidence", hypothesis_id: str | None = None
) -> dict[str, Any]:
    criterion: dict[str, Any] = {
        "kind": kind,
        "description": "harness completion condition",
        "min_evidence": 1,
    }
    if hypothesis_id:
        criterion["hypothesis_id"] = hypothesis_id
    return {
        "path_id": "path-draft-1",
        "run_id": "run-x",
        "mission_id": None,
        "version": 1,
        "parent_version": None,
        "status": "draft",
        "reason": "scripted draft",
        "goal": "map the in-scope host",
        "steps": [
            {
                "step_id": "step-1",
                "objective": "confirm the host answers inventory",
                "agent_id": RECON_AGENT,
                "target_node_ids": [node_id],
                "principal_refs": [],
                "allowed_operations": [CANONICAL_OP],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [criterion],
                "required": True,
                "status": "pending",
                "outcome": None,
                "evidence_ids": [],
            }
        ],
        "hypothesis_ids": [],
        "evidence_ids": [],
        "changed_step_ids": [],
        "review_records": {},
    }


def review_accept(reason: str = "scripted accept") -> dict[str, Any]:
    return {"decision": "accept", "reason": reason, "path": None}


def turn_actions(node_id: str, *, tool_id: str = INVENTORY_TOOL) -> dict[str, Any]:
    return {
        "mode": "actions",
        "reason": "scripted action",
        "actions": [
            {
                "path_step_id": "step-1",
                "agent_id": RECON_AGENT,
                "target_node_id": node_id,
                "principal_ref": None,
                "capability": "recon",
                "operation": "service_discovery",
                "tool_id": tool_id,
                "arguments": {},
                "reason": "inventory the host",
                "expected_observation": "the inventory tool returns a result",
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


def turn_complete_step(
    evidence_ids: list[str], *, outcome: str = "success", reason: str = "scripted completion"
) -> dict[str, Any]:
    return {
        "mode": "complete_step",
        "reason": reason,
        "actions": [],
        "path": None,
        "path_step_id": "step-1",
        "completion": {
            "evidence_ids": list(evidence_ids),
            "outcome": outcome,
            "reason": reason,
            "criterion_results": [],
        },
        "evidence_request": {},
        "hypothesis_update": {},
    }


def turn_complete_goal(reason: str = "scripted goal completion") -> dict[str, Any]:
    return {
        "mode": "complete_goal",
        "reason": reason,
        "actions": [],
        "path": None,
        "path_step_id": None,
        "completion": {},
        "evidence_request": {},
        "hypothesis_update": {},
    }


def completion_accept(reason: str = "scripted semantic accept") -> dict[str, Any]:
    return {"accepted": True, "reason": reason}


def goal_accept(
    criteria: list[dict[str, Any]], reason: str = "scripted goal accept"
) -> dict[str, Any]:
    return {"accepted": True, "reason": reason, "criteria": criteria}


def meaning(
    outcome: str, claim_supported: bool, reason: str = "scripted meaning"
) -> dict[str, Any]:
    return {
        "meaning": "scripted",
        "outcome": outcome,
        "reason": reason,
        "confidence": 0.8,
        "claim_supported": claim_supported,
    }


def make_tool_evidence(
    *,
    evidence_id: str,
    run_id: str,
    engagement_id: str,
    action_id: str,
    invocation_id: str,
    path_id: str,
    path_version: int,
    step_id: str,
    target_node_id: str,
    hypothesis_id: str | None = None,
    success: bool = True,
    output: Any | None = None,
    capability: str = "recon",
    operation: str = "service_discovery",
    tool_id: str = INVENTORY_TOOL,
) -> EvidenceRecord:
    body = output if output is not None else {"inventory": "ok"}
    raw = {
        "action_id": action_id,
        "invocation_id": invocation_id,
        "tool_id": tool_id,
        "success": success,
        "output": body,
        "metadata": {},
    }
    metadata: dict[str, Any] = {
        "path_id": path_id,
        "path_version": path_version,
        "path_step_id": step_id,
        "target_node_id": target_node_id,
        "capability": capability,
        "operation": operation,
        "canonical_operation": f"{capability}.{operation}",
        "operation_key": f"{capability}.{operation}",
        "action_id": action_id,
        "invocation_id": invocation_id,
        "success": success,
        "proposal": {
            "action_id": action_id,
            "capability": capability,
            "operation": operation,
            "arguments": {},
        },
        "tool_result": raw,
    }
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        source_type=EvidenceSourceType.TOOL,
        source_id=tool_id,
        action_id=ActionId(action_id),
        invocation_id=InvocationId(invocation_id),
        hypothesis_id=HypothesisId(hypothesis_id) if hypothesis_id is not None else None,
        content_type="application/json",
        digest=hashlib.sha256(evidence_id.encode()).hexdigest(),
        summary=f"harness tool evidence {evidence_id}",
        created_at=utcnow(),
        metadata=metadata,
    )


def make_proposal_result(
    *,
    run_id: str,
    engagement_id: str,
    action_id: str,
    invocation_id: str,
    host: str = HOST,
    hypothesis_id: str | None = None,
    success: bool = True,
    output: Any | None = None,
    tool_id: str = INVENTORY_TOOL,
) -> tuple[ActionProposal, ToolResult]:
    proposal = ActionProposal(
        action_id=ActionId(action_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        agent_id=AgentId(RECON_AGENT),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=host),
        arguments={},
        reason="harness proposal",
        hypothesis_id=HypothesisId(hypothesis_id) if hypothesis_id is not None else None,
        created_at=utcnow(),
    )
    result = ToolResult(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId(action_id),
        tool_id=ToolId(tool_id),
        success=success,
        output=output if output is not None else {"inventory": "ok"},
        started_at=utcnow(),
        finished_at=utcnow(),
        metadata={},
    )
    return proposal, result


async def admit_final(
    evidence_store: FakeEvidenceStore,
    *,
    proposal: ActionProposal,
    result: ToolResult,
    outcome: VerificationOutcome,
    verdict: Verdict,
    claim_supported: bool,
    source_refs: list[str],
    reason: str = "harness final admission",
) -> Any:
    assessment = VerificationAssessment(
        verdict=verdict,
        reason=reason,
        reviewer_id="harness",
        source_refs=list(source_refs),
        outcome=outcome,
        last_record_stage=None,
        claim_supported=claim_supported,
    )
    return await append_review_receipt(
        evidence_store=evidence_store,
        proposal=proposal,
        result=result,
        assessment=assessment,
        admission="final",
    )


async def record_open_hypothesis(
    service: AttackGraphService, hypothesis_id: str, target_node_id: str, run_id: str
) -> Hypothesis:
    from redteam.attack_graph.predicates import Confidence, Provenance

    hypothesis = Hypothesis(
        hypothesis_id=hypothesis_id,
        description="harness hypothesis",
        confidence=0.4,
        status="open",
        metadata={"target_node_id": target_node_id},
    )
    await service.record_hypothesis(
        hypothesis,
        about=GraphNodeId(target_node_id),
        step=0,
        provenance=Provenance(
            source="test:harness", run_id=run_id, step=0, confidence=Confidence.INFERRED
        ),
    )
    return hypothesis


async def record_verified_finding(
    service: AttackGraphService,
    *,
    finding_id: str,
    evidence_ids: list[str],
    target_node_id: str,
    hypothesis_id: str | None = None,
) -> Finding:
    finding = Finding(
        finding_id=finding_id,
        title="harness verified finding",
        description="harness",
        verified=True,
        evidence_ids=list(evidence_ids),
        metadata={"hypothesis_id": hypothesis_id} if hypothesis_id else {},
    )
    await service.record_finding(finding, about=GraphNodeId(target_node_id), step=0)
    return finding


def make_mission_task(
    *, mission_id: str, engagement_id: str, criteria: list[str], task_id: str = "task-t1"
) -> tuple[MissionPlan, MissionTask]:
    now = utcnow()
    mission = MissionPlan(
        mission_id=MissionId(mission_id),
        engagement_id=EngagementId(engagement_id),
        objective="map the in-scope host",
        success_criteria=list(criteria),
        created_at=now,
        updated_at=now,
        provenance=MissionProvenance(source="test:t1"),
    )
    task = MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId(mission_id),
        title="Investigate the operator goal",
        objective="map the in-scope host",
        status=TaskStatus.READY,
        required=True,
        provenance=MissionProvenance(source="test:t1"),
        created_at=now,
        updated_at=now,
    )
    return mission, task
