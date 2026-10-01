"""Semantic completion + review receipts through the real verification path.

The meaning-judgment seam, the single-action verification step, and the
code-only completion gate are exercised together: a scripted model (no
network) drives the real structured meaning verifier, every case runs the
real verify step, and completion is checked with the real completion
validator. The scripted model and the scripted evidence store below are
local to this file; no existing fakes or production code are touched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.attack_graph.identity import node_id_for_target, session_principal_node_id
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.graph import Hypothesis
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType, Verdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    HypothesisId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.agents.base import HypothesisDraft, PlanStep
from redteam.orchestration.analysis import (
    StructuredLLMAnalysisVerifier,
    VerificationAssessment,
    append_review_receipt,
)
from redteam.orchestration.completion import validate_completion
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.expectations import Expectation
from redteam.orchestration.nodes.verify import verify_node
from redteam.orchestration.paths.models import (
    CompletionCriterion,
    PathPlan,
    PathStep,
)
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.integration import _engine_llm_harness as h

NOW = datetime.now(timezone.utc)
RUN = RunId("run_receipts")
ENGAGEMENT = EngagementId("eng_receipts")
AGENT = AgentId("recon-agent")
HOST = "target.local"
TARGET = HostTarget(host=HOST)
TARGET_NODE = str(node_id_for_target(TARGET))
PATH_ID = "path_1"
PATH_VERSION = 1
STEP_ID = "step_1"
HYP_ID = "hyp_1"


class _ScriptedLLM:
    """Minimal scripted model: returns one canned meaning judgment."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = dict(response)
        self.calls = 0

    async def complete_structured(self, *, messages, response_schema, system="", num_retries=0):
        self.calls += 1
        return dict(self._response)


class _LocalEvidenceStore:
    """Fresh in-memory evidence store local to this file."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def append(self, evidence: Any) -> None:
        self.records.append(evidence)

    async def get(self, evidence_id: Any) -> Any | None:
        return next((r for r in self.records if r.evidence_id == evidence_id), None)

    async def list_for_run(self, run_id: Any) -> list[Any]:
        return [r for r in self.records if r.run_id == run_id]

    async def list_for_action(self, action_id: Any) -> list[Any]:
        return [r for r in self.records if r.action_id == action_id]


class _NoopControlPlane:
    pass


class _NoopGateway:
    pass


def _success_llm() -> dict[str, Any]:
    return {
        "meaning": "output shows the tested claim held",
        "outcome": "success",
        "reason": "status 200 observed for the tested claim",
        "confidence": 0.9,
        "claim_supported": True,
    }


def _refuted_llm() -> dict[str, Any]:
    return {
        "meaning": "output cleanly misses the tested claim",
        "outcome": "refuted",
        "reason": "status is not 200",
        "confidence": 0.9,
        "claim_supported": False,
    }


def _harness(llm_response: dict[str, Any]):
    store = _LocalEvidenceStore()
    llm = _ScriptedLLM(llm_response)
    graph_store = InMemoryAttackGraphStore()
    service = AttackGraphService(graph_store)
    bus = InMemoryEventBus()
    verifier = StructuredLLMAnalysisVerifier(llm, store, service)
    deps = RuntimeDeps(
        control_plane=_NoopControlPlane(),  # type: ignore[arg-type]
        tool_gateway=_NoopGateway(),  # type: ignore[arg-type]
        event_bus=bus,
        graph_store=graph_store,
        evidence_store=store,  # type: ignore[arg-type]
        llm_client=llm,  # type: ignore[arg-type]
        verifier=verifier,
    )
    return store, llm, service, deps


def _proposal(
    *, action_id: str = "action_1", hyp_id: str = HYP_ID, args: dict | None = None
) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId(action_id),
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        agent_id=AGENT,
        capability="recon",
        operation="service_discovery",
        target=TARGET,
        arguments=dict(args or {}),
        reason="probe",
        hypothesis_id=HypothesisId(hyp_id),
        expected_observation="echo comes back",
        created_at=NOW,
    )


def _result(
    *, action_id: str = "action_1", invocation_id: str = "inv_1", output: Any = None
) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId(invocation_id),
        action_id=ActionId(action_id),
        tool_id=ToolId("recon.ping"),
        success=True,
        output={"status": 200} if output is None else output,
        error=None,
        started_at=NOW,
        finished_at=NOW,
    )


def _plan_step(
    *,
    violation: bool = True,
    assessment: str = "security_check",
    principal_ref: str | None = None,
    expectation: Expectation | None = None,
) -> PlanStep:
    exp = expectation or Expectation(
        kind="output_field_equals",
        field="status",
        value=200,
        violation_if_met=violation,
        description="target must not return 200",
    )
    return PlanStep(
        step_id="s1",
        candidate_key="ck1",
        path_id=PATH_ID,
        path_version=PATH_VERSION,
        path_step_id=STEP_ID,
        principal_ref=principal_ref,
        capability="recon",
        operation="service_discovery",
        tool_id=ToolId("recon.ping"),
        assessment_kind=assessment,  # type: ignore[arg-type]
        target=TARGET,
        reason="probe",
        hypothesis=HypothesisDraft(
            description="target returns 200", violation_if_confirmed=violation
        ),
        expectation=exp,
    )


def _path_step(*, principals: list[str] | None = None, kind: str = "poc") -> PathStep:
    return PathStep(
        step_id=STEP_ID,
        objective="confirm",
        target_node_ids=[TARGET_NODE],
        principal_refs=list(principals or []),
        allowed_operations=["recon.service_discovery"],
        completion_criteria=[
            CompletionCriterion(kind=kind, description="poc", min_evidence=1, hypothesis_id=HYP_ID)
        ],
    )


def _path(step: PathStep) -> PathPlan:
    return PathPlan(
        path_id=PATH_ID,
        run_id=str(RUN),
        version=PATH_VERSION,
        status="active",
        reason="test",
        goal="test",
        steps=[step],
    )


def _completion_proposal(evidence_id: Any, *, outcome: str = "success") -> dict:
    return {
        "outcome": outcome,
        "reason": "poc achieved",
        "evidence_ids": [str(evidence_id)],
        "criterion_results": [],
    }


async def _run_verify(service, deps, proposal, result, step):
    await service.record_hypothesis(
        Hypothesis(hypothesis_id=str(proposal.hypothesis_id), description="target returns 200")
    )
    state = initial_graph_state(RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT))
    state["action_proposal"] = proposal
    state["tool_result"] = result
    state["plan_step"] = step
    return await verify_node(state, deps=deps)


async def test_matching_receipt_allows_poc_completion() -> None:
    store, _llm, service, deps = _harness(_success_llm())
    proposal, result = _proposal(), _result()
    await _run_verify(service, deps, proposal, result, _plan_step(violation=True))
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step())
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues == []
    findings = await service.list_findings(verified=True)
    assert len(findings) == 1


async def test_mismatched_and_superseded_receipts_block_poc() -> None:
    store, _llm, service, deps = _harness(_success_llm())
    proposal = _proposal()
    bad_result = _result(action_id="action_2")
    out = await _run_verify(service, deps, proposal, bad_result, _plan_step(violation=True))
    assert out["runtime"].last_verdict is Verdict.UNKNOWN
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step())
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []

    store2, _llm2, service2, deps2 = _harness(_success_llm())
    proposal2, result2 = _proposal(), _result()
    await _run_verify(service2, deps2, proposal2, result2, _plan_step(violation=True))
    good_id = store2.records[0].evidence_id
    plan2 = _path(_path_step())
    ok = await validate_completion(
        path=plan2,
        step=plan2.steps[0],
        proposal=_completion_proposal(good_id, outcome="success"),
        evidence_store=store2,
        graph_service=service2,
        run_id=str(RUN),
    )
    assert ok == []
    stale = VerificationAssessment(
        verdict=Verdict.UNKNOWN,
        reason="later review supersedes the earlier positive",
        reviewer_id="analysis:structured-llm-v1",
        source_refs=[str(good_id), str(result2.invocation_id)],
        outcome="unknown",
        claim_supported=False,
    )
    await append_review_receipt(
        evidence_store=store2,
        proposal=proposal2,
        result=result2,
        assessment=stale,
        admission="final",
        supersedes=["analysis_verify"],
    )
    blocked = await validate_completion(
        path=plan2,
        step=plan2.steps[0],
        proposal=_completion_proposal(good_id, outcome="success"),
        evidence_store=store2,
        graph_service=service2,
        run_id=str(RUN),
    )
    assert blocked != []
    assert any("final admission" in item or "receipt" in item for item in blocked)


async def test_observation_only_unknown_cannot_complete_poc() -> None:
    store, _llm, service, deps = _harness(_success_llm())
    proposal, result = _proposal(), _result()
    out = await _run_verify(
        service, deps, proposal, result, _plan_step(violation=True, assessment="observation")
    )
    assert out["runtime"].last_verdict is Verdict.UNKNOWN
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step())
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []


async def test_benign_pass_is_not_violation_nor_poc() -> None:
    store, _llm, service, deps = _harness(_success_llm())
    proposal, result = _proposal(), _result()
    out = await _run_verify(service, deps, proposal, result, _plan_step(violation=False))
    assert out["runtime"].last_verdict is Verdict.PASS
    findings = await service.list_findings(verified=True)
    assert findings == []
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step())
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []


async def test_clean_refuted_is_not_poc_completion() -> None:
    store, _llm, service, deps = _harness(_refuted_llm())
    proposal = _proposal()
    result = _result(output={"status": 500})
    out = await _run_verify(service, deps, proposal, result, _plan_step(violation=True))
    assert out["runtime"].last_verdict is Verdict.PASS
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step())
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []
    assert any("refuted" in item or "receipt" in item or "violation" in item for item in issues)


async def test_account_scoped_step_rejects_wrong_and_anonymous_receipts() -> None:
    for principal_ref, needle in [("bob", "outside the step scope"), ("anonymous", "anonymous")]:
        store, _llm, service, deps = _harness(_success_llm())
        proposal, result = _proposal(), _result()
        await _run_verify(
            service, deps, proposal, result, _plan_step(violation=True, principal_ref=principal_ref)
        )
        tool_evidence_id = store.records[0].evidence_id
        plan = _path(_path_step(principals=["alice"]))
        issues = await validate_completion(
            path=plan,
            step=plan.steps[0],
            proposal=_completion_proposal(tool_evidence_id, outcome="success"),
            evidence_store=store,
            graph_service=service,
            run_id=str(RUN),
        )
        assert issues != []
        assert any(needle in item for item in issues)

    store, _llm, service, deps = _harness(_success_llm())
    proposal, result = _proposal(), _result()
    await _run_verify(
        service, deps, proposal, result, _plan_step(violation=True, principal_ref="alice")
    )
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step(principals=["alice"]))
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []
    assert any("raw session receipt" in item or "session binding" in item for item in issues)


async def test_declared_binding_with_unrelated_measured_sibling_is_not_account_proof() -> None:
    """Regression: a declared node binding plus an unrelated measured write on the
    same node must not count as account proof (desired behavior: reject).

    Only an exact raw mapping receipt (row level) or granular observed-facts
    grounding may prove the account. Implementation fix lands after root
    arbitration; this test pins the desired rejection.
    """
    store, _llm, service, deps = _harness(_success_llm())
    proposal, result = _proposal(), _result()
    await _run_verify(
        service, deps, proposal, result, _plan_step(violation=True, principal_ref="alice")
    )
    tool_evidence_id = store.records[0].evidence_id
    plan = _path(_path_step(principals=["alice"]))
    sanity = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert sanity != []

    await service.upsert_node(
        GraphNodeType.PRINCIPAL,
        node_id=session_principal_node_id(TARGET_NODE, "sess-alice"),
        facts={
            "target_ref": TARGET_NODE,
            "principal_ref": "alice",
            "session_refs": ["sess-alice"],
        },
        provenance=Provenance(
            step=1,
            source="tool:recon.ping",
            evidence_id="ev_unrelated_sibling",
            run_id=str(RUN),
            confidence=Confidence.MEASURED,
        ),
        step=1,
    )
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(tool_evidence_id, outcome="success"),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    assert issues != []
    assert any(
        "raw session receipt" in item or "session binding" in item or "account" in item
        for item in issues
    )


@pytest.mark.parametrize("mapping_success", [True, False])
async def test_same_run_session_alias_requires_successful_raw_mapping(
    mapping_success: bool,
) -> None:
    store, _llm, service, deps = _harness(_success_llm())
    mapping = h.make_tool_evidence(
        evidence_id="ev-session-map",
        run_id=str(RUN),
        engagement_id=str(ENGAGEMENT),
        action_id="action-map",
        invocation_id="inv-map",
        path_id=PATH_ID,
        path_version=1,
        step_id=STEP_ID,
        target_node_id=TARGET_NODE,
        capability="web",
        operation="session_list",
        tool_id="web.session_list",
        success=mapping_success,
        output={
            "sessions": [
                {
                    "target_ref": "configured-target",
                    "session_ref": "SessionA",
                    "principal_ref": "Alice",
                }
            ]
        },
    )
    mapping.metadata["proposal"]["arguments"] = {"target_ref": "configured-target"}
    await store.append(mapping)
    proposal = _proposal(args={"target_ref": "configured-target", "session_ref": "SessionA"})
    result = _result(output={"status": 200, "session_ref": "SessionA"})
    out = await _run_verify(
        service, deps, proposal, result, _plan_step(violation=True, principal_ref="Alice")
    )
    original = next(
        item
        for item in store.records
        if str(item.action_id) == "action_1" and item.metadata.get("tool_result") is not None
    )
    plan = _path(_path_step(principals=["Alice"]))
    issues = await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal=_completion_proposal(original.evidence_id),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )
    if mapping_success:
        assert out["runtime"].last_verdict is Verdict.VIOLATION
        assert issues == []
    else:
        assert out["runtime"].last_verdict is Verdict.UNKNOWN
        assert issues != []
