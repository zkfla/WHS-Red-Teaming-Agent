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
            "actual_session_ref": "SessionA",
            "principal_ref": "Alice",
        },
    )
    mapping.metadata["proposal"]["arguments"] = {"target_ref": "configured-target"}
    await store.append(mapping)
    proposal = _proposal(args={"target_ref": "configured-target", "session_ref": "SessionA"})
    result = _result(output={"status": 200, "actual_session_ref": "SessionA"})
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


@pytest.mark.parametrize(
    "tool_id", ["web.session_list", "web.bank.session_list", "web.bank.list_test_sessions"]
)
@pytest.mark.parametrize("row_target", ["bank-lab", "other-bank"])
async def test_session_inventory_never_proves_actual_use_for_completion(
    tool_id: str, row_target: str
) -> None:
    store, _llm, service, _deps = _harness(_success_llm())
    await service.upsert_node(
        GraphNodeType.HOST,
        node_id=TARGET_NODE,
        facts={"target_ref": "bank-lab"},
        provenance=Provenance(
            step=0,
            source="engagement:test",
            run_id=str(RUN),
            confidence=Confidence.SERVER_DECLARED,
        ),
        step=0,
    )
    mapping = h.make_tool_evidence(
        evidence_id="ev-session-list-bob",
        run_id=str(RUN),
        engagement_id=str(ENGAGEMENT),
        action_id="action-session-list",
        invocation_id="inv-session-list",
        path_id=PATH_ID,
        path_version=PATH_VERSION,
        step_id=STEP_ID,
        target_node_id=TARGET_NODE,
        capability="web",
        operation="response_analysis",
        tool_id=tool_id,
        output={
            "sessions": [
                {
                    "target_ref": row_target,
                    "session_ref": "bob",
                    "principal_ref": "test-bob",
                }
            ]
        },
    )
    mapping.metadata["principal_ref"] = "bob"
    await store.append(mapping)
    step = PathStep(
        step_id=STEP_ID,
        objective="verify the supplied bob session alias",
        target_node_ids=[TARGET_NODE],
        principal_refs=["bob"],
        allowed_operations=["web.response_analysis"],
        completion_criteria=[
            CompletionCriterion(
                kind="evidence",
                description="session_list contains bob",
                min_evidence=1,
                principal_refs=["bob"],
            )
        ],
    )
    plan = _path(step)

    issues = await validate_completion(
        path=plan,
        step=step,
        proposal=_completion_proposal(mapping.evidence_id),
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )

    assert issues
    assert any("session" in item or "principal" in item for item in issues)


async def test_summary_step_aggregates_three_prior_origins_without_direct_execution() -> None:
    store, _llm, service, _deps = _harness(_success_llm())
    from redteam.orchestration.paths import InMemoryPathStore

    prior_ids: list[str] = []
    for stem in ("1", "2", "3"):
        ev = h.make_tool_evidence(
            evidence_id=f"ev-summary-{stem}",
            run_id=str(RUN),
            engagement_id=str(ENGAGEMENT),
            action_id=f"action-summary-{stem}",
            invocation_id=f"inv-summary-{stem}",
            path_id=PATH_ID,
            path_version=PATH_VERSION,
            step_id=f"s{stem}",
            target_node_id=TARGET_NODE,
        )
        await store.append(ev)
        prior_ids.append(str(ev.evidence_id))

    def _prior(step_id: str, stem: str) -> PathStep:
        return PathStep(
            step_id=step_id,
            objective=f"collect part {stem}",
            target_node_ids=[TARGET_NODE],
            allowed_operations=["recon.service_discovery"],
            completion_criteria=[
                CompletionCriterion(
                    kind="evidence",
                    description=f"part {stem} observed",
                    min_evidence=1,
                )
            ],
            required=True,
            status="completed",
            evidence_ids=[f"ev-summary-{stem}"],
        )

    summary = PathStep(
        step_id="summary",
        objective="summarize three prior observations",
        target_node_ids=[TARGET_NODE],
        allowed_operations=["recon.service_discovery"],
        completion_criteria=[
            CompletionCriterion(
                kind="evidence",
                description="aggregate three origins",
                min_evidence=3,
            )
        ],
        required=True,
        status="pending",
        depends_on=["s1", "s2", "s3"],
    )
    plan = PathPlan(
        path_id=PATH_ID,
        run_id=str(RUN),
        version=PATH_VERSION,
        status="active",
        reason="test summary",
        goal="test",
        steps=[_prior("s1", "1"), _prior("s2", "2"), _prior("s3", "3"), summary],
    )
    path_store = InMemoryPathStore()
    await path_store.register(plan)

    issues = await validate_completion(
        path=plan,
        step=summary,
        proposal={
            "outcome": "success",
            "reason": "summary aggregates three stored origins",
            "evidence_ids": list(prior_ids),
            "criterion_results": [],
        },
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
        path_store=path_store,
    )
    assert issues == []
    origins = [await store.get(eid) for eid in prior_ids]
    assert all(rec is not None for rec in origins)
    assert {str(rec.metadata.get("path_step_id")) for rec in origins} == {"s1", "s2", "s3"}
    assert "summary" not in {str(rec.metadata.get("path_step_id")) for rec in origins}
    assert all(str(rec.metadata.get("path_id")) == PATH_ID for rec in origins)


# Runtime-local recording regressions use preserved originals and the real
# verifier, graph service and completion consumer; only the model is scripted.
async def _recording_world(*, output=None, response=None):
    from redteam.orchestration.graph_recording import (
        GraphRecordingHandler,
        graph_recording_descriptor,
    )
    from redteam.orchestration.local_tools import LocalToolRegistry

    store, llm, service, deps = _harness(response or _success_llm())
    await service.observe_target(TARGET)
    refs = []
    for index in (1, 2):
        proposal = _proposal(action_id=f"record-action-{index}", hyp_id=None)
        evidence = h.make_tool_evidence(
            evidence_id=f"record-source-{index}",
            run_id=str(RUN),
            engagement_id=str(ENGAGEMENT),
            action_id=str(proposal.action_id),
            invocation_id=f"record-inv-{index}",
            path_id=PATH_ID,
            path_version=PATH_VERSION,
            step_id=STEP_ID,
            target_node_id=TARGET_NODE,
            output={"status": 200, "address": "https://other.local/api"}
            if output is None
            else output,
        )
        evidence.metadata["proposal"] = proposal.model_dump(mode="json")
        await store.append(evidence)
        refs.append(str(evidence.evidence_id))
    registry = LocalToolRegistry()
    registry.register(
        graph_recording_descriptor(),
        GraphRecordingHandler(evidence_store=store, graph_service=service),
    )
    return store, llm, service, deps, registry, refs


def _recording_context(deps, *, limit=10):
    from redteam.orchestration.cognition import DecisionBudget
    from redteam.orchestration.local_tools import LocalToolContext

    return LocalToolContext(
        deps=deps,
        runtime=RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT),
        purpose="validate supplied source",
        budget=DecisionBudget.reasoning_chain(limit, 0),
    )


async def _posthoc_complete(
    store, service, hid, refs, *, kind="hypothesis_confirmed", outcome="success"
):
    plan = _path(_path_step(kind=kind))
    plan.steps[0].completion_criteria[0].hypothesis_id = hid
    return await validate_completion(
        path=plan,
        step=plan.steps[0],
        proposal={"outcome": outcome, "reason": "source reviewed", "evidence_ids": refs},
        evidence_store=store,
        graph_service=service,
        run_id=str(RUN),
    )


async def test_local_record_string_and_same_value_replay_preserve_original():
    from copy import deepcopy

    from redteam.attack_graph.predicates import facts_of
    from redteam.orchestration.analysis import is_final_admission_receipt

    store, llm, service, deps, registry, refs = await _recording_world(output="service banner")
    original = deepcopy((await store.get(refs[0])).model_dump(mode="json"))
    args = {
        "facts": [
            {
                "evidence_id": refs[0],
                "raw_path": [],
                "value": "service banner",
                "fact_name": "banner",
                "target_node_id": TARGET_NODE,
            }
        ],
        "branches": [
            {
                "question": "Inspect another endpoint",
                "rationale": "banner raises a question",
                "evidence_refs": refs,
            }
        ],
    }
    first = await registry.call("graph.record", args, _recording_context(deps))
    repeat = await registry.call("graph.record", args, _recording_context(deps))
    assert first.ok and repeat.ok
    assert first.payload["record_ids"] == repeat.payload["record_ids"]
    assert facts_of((await service.get_node(TARGET_NODE)).properties)["banner"] == "service banner"
    branch = await store.get(first.payload["branches"][0]["evidence_id"])
    assert branch.metadata["record_kind"] == "branch_note"
    assert branch.metadata["evidence_refs"] == refs
    assert str(branch.evidence_id) in first.refs
    assert not is_final_admission_receipt(branch)
    assert llm.calls == 0
    assert (await store.get(refs[0])).model_dump(mode="json") == original


async def test_scalar_measurement_cannot_ground_an_arbitrary_related_target():
    from redteam.attack_graph.predicates import facts_of
    from redteam.orchestration.analysis import claim_target_is_grounded

    store, llm, service, deps, registry, refs = await _recording_world()
    unrelated = await service.observe_target(HostTarget(host="unrelated.local"))
    result = await registry.call(
        "graph.record",
        {
            "facts": [
                {
                    "evidence_id": refs[0],
                    "raw_path": ["status"],
                    "value": 200,
                    "fact_name": "status_code",
                    "target_node_id": TARGET_NODE,
                    "edge": {
                        "edge_type": "owns",
                        "source_node_id": TARGET_NODE,
                        "target_node_id": str(unrelated.id),
                    },
                }
            ],
            "findings": [
                {
                    "statement": "unrelated host exposes data",
                    "target_node_id": str(unrelated.id),
                    "evidence_refs": refs,
                }
            ],
        },
        _recording_context(deps),
    )
    assert result.payload["accepted_facts"]
    assert facts_of((await service.get_node(TARGET_NODE)).properties)["status_code"] == 200
    assert not await claim_target_is_grounded(service, await store.get(refs[0]), str(unrelated.id))
    assert result.payload["findings"][0]["status"] == "refused"
    assert await service.list_findings() == []
    assert llm.calls == 0


@pytest.mark.parametrize("address_proof", [True, False])
async def test_discovered_entity_requires_address_not_success_scalar(address_proof):
    from redteam.attack_graph.identity import node_id_for_target
    from redteam.core.contracts.target import UrlTarget
    from redteam.orchestration.analysis import claim_target_is_grounded

    store, _, service, deps, registry, refs = await _recording_world()
    discovered = UrlTarget(url="https://other.local/api")
    result = await registry.call(
        "graph.record",
        {
            "facts": [
                {
                    "evidence_id": refs[0],
                    "raw_path": ["status"],
                    "value": 200,
                    "entity_raw_path": ["address"] if address_proof else ["status"],
                    "fact_name": "response_status",
                    "target_node_id": TARGET_NODE,
                    "new_entity": {
                        "target": discovered.model_dump(mode="json"),
                        "relation": "owns",
                    },
                }
            ]
        },
        _recording_context(deps),
    )
    node = await service.get_node(node_id_for_target(discovered))
    if address_proof:
        assert result.ok and node is not None
        assert node.properties["attrs"]["observation"] == "discovered"
        assert "in_scope" not in node.properties["attrs"]
        assert await claim_target_is_grounded(service, await store.get(refs[0]), str(node.id))
        relations = await service.list_edges()
        assert {edge.type.value for edge in relations} == {"observed", "owns"}
    else:
        assert not result.ok and node is None
        assert await service.list_edges() == []


@pytest.mark.parametrize("attack", ["run", "engagement", "invocation", "system", "value"])
async def test_recorded_fact_rejects_mismatched_origin_and_value(attack):
    from redteam.core.enums import EvidenceSourceType

    store, _, service, deps, registry, refs = await _recording_world()
    original = await store.get(refs[0])
    if attack == "run":
        original.run_id = RunId("another-run")
    elif attack == "engagement":
        original.engagement_id = EngagementId("another-engagement")
    elif attack == "invocation":
        original.metadata["tool_result"]["invocation_id"] = "forged-invocation"
    elif attack == "system":
        original.source_type = EvidenceSourceType.SYSTEM
    result = await registry.call(
        "graph.record",
        {
            "facts": [
                {
                    "evidence_id": refs[0],
                    "raw_path": ["status"],
                    "value": 201 if attack == "value" else 200,
                    "fact_name": "status_code",
                    "target_node_id": TARGET_NODE,
                }
            ]
        },
        _recording_context(deps),
    )
    assert not result.ok and result.payload["refused_facts"]
    assert "status_code" not in (await service.get_node(TARGET_NODE)).properties["facts"]


async def test_standalone_hypothesis_registers_open_without_model_review():
    from copy import deepcopy

    store, llm, service, deps, registry, refs = await _recording_world()
    originals = [deepcopy((await store.get(ref)).model_dump(mode="json")) for ref in refs]
    recorded = await registry.call(
        "graph.record",
        {
            "hypotheses": [
                {
                    "statement": "response observed",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": refs,
                }
            ]
        },
        _recording_context(deps),
    )
    entry = recorded.payload["hypotheses"][0]
    assert recorded.ok
    assert entry["status"] == "open"
    assert "receipt_id" not in entry
    assert llm.calls == 0
    assert (await service.get_hypothesis(entry["hypothesis_id"])).status == "open"
    # An open question alone satisfies no confirmation criterion.
    assert await _posthoc_complete(store, service, entry["hypothesis_id"], refs) != []
    assert [(await store.get(ref)).model_dump(mode="json") for ref in refs] == originals


async def test_explicit_security_finding_confirms_open_hypothesis_with_receipt():
    store, llm, service, deps, registry, refs = await _recording_world(
        response={**_success_llm(), "hypothesis_outcome": "success"}
    )
    recorded = await registry.call(
        "graph.record",
        {
            "hypotheses": [
                {
                    "statement": "Unauthenticated callers can read accounts",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": refs,
                }
            ]
        },
        _recording_context(deps),
    )
    hid = recorded.payload["hypotheses"][0]["hypothesis_id"]
    assert recorded.payload["hypotheses"][0]["status"] == "open"
    assert llm.calls == 0
    found = await registry.call(
        "graph.record",
        {
            "findings": [
                {
                    "statement": "Anonymous requests disclose another customer's account",
                    "target_node_id": TARGET_NODE,
                    "hypothesis_id": hid,
                    "evidence_refs": refs,
                }
            ]
        },
        _recording_context(deps),
    )
    finding = found.payload["findings"][0]
    assert found.ok and finding["status"] == "recorded"
    assert finding["hypothesis_id"] == hid
    assert llm.calls == 1
    assert (await service.get_hypothesis(hid)).status == "confirmed"
    assert await _posthoc_complete(store, service, hid, refs) == []
    assert await _posthoc_complete(store, service, hid, [finding["receipt_id"]]) == []


async def test_empty_hypothesis_ref_registers_open_without_review():
    store, llm, service, deps, registry, refs = await _recording_world()
    recorded = await registry.call(
        "graph.record",
        {
            "hypotheses": [
                {
                    "statement": "needs a follow-up question",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": [],
                }
            ]
        },
        _recording_context(deps),
    )
    entry = recorded.payload["hypotheses"][0]
    assert recorded.ok
    assert entry["status"] == "open"
    assert "receipt_id" not in entry
    assert llm.calls == 0
    assert (await service.get_hypothesis(entry["hypothesis_id"])).status == "open"


async def test_hypothesis_without_verifier_registers_open():
    from dataclasses import replace

    from redteam.orchestration.graph_recording import (
        GraphRecordingHandler,
        graph_recording_descriptor,
    )
    from redteam.orchestration.local_tools import LocalToolRegistry

    store, llm, service, deps, registry, refs = await _recording_world()
    plain = LocalToolRegistry()
    plain.register(
        graph_recording_descriptor(),
        GraphRecordingHandler(evidence_store=store, graph_service=service),
    )
    context = _recording_context(deps)
    context = replace(context, deps=replace(deps, verifier=None))
    recorded = await plain.call(
        "graph.record",
        {
            "hypotheses": [
                {
                    "statement": "response observed",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": refs,
                }
            ]
        },
        context,
    )
    entry = recorded.payload["hypotheses"][0]
    assert recorded.ok
    assert entry["status"] == "open"
    assert llm.calls == 0


async def test_confirmed_hypothesis_reregistration_keeps_confirmed_status():
    store, llm, service, deps, registry, refs = await _recording_world(
        response={**_success_llm(), "hypothesis_outcome": "success"}
    )
    first = await registry.call(
        "graph.record",
        {
            "findings": [
                {
                    "statement": "Anonymous requests disclose another customer's account",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": refs,
                }
            ]
        },
        _recording_context(deps),
    )
    hid = first.payload["findings"][0]["hypothesis_id"]
    assert (await service.get_hypothesis(hid)).status == "confirmed"
    calls = llm.calls
    repeat = await registry.call(
        "graph.record",
        {
            "hypotheses": [
                {
                    "statement": "Anonymous requests disclose another customer's account",
                    "target_node_id": TARGET_NODE,
                    "evidence_refs": refs,
                }
            ]
        },
        _recording_context(deps),
    )
    assert repeat.ok
    assert repeat.payload["hypotheses"][0]["hypothesis_id"] == hid
    assert repeat.payload["hypotheses"][0]["status"] == "confirmed"
    assert llm.calls == calls
    assert (await service.get_hypothesis(hid)).status == "confirmed"


async def test_finding_paraphrase_one_review_replay_and_latest_refutation():
    from copy import deepcopy

    response = {**_success_llm(), "hypothesis_outcome": "success"}
    store, llm, service, deps, registry, refs = await _recording_world(response=response)
    originals = [deepcopy((await store.get(ref)).model_dump(mode="json")) for ref in refs]
    args = {
        "hypotheses": [
            {
                "statement": "Unauthenticated callers can read accounts",
                "target_node_id": TARGET_NODE,
                "evidence_refs": refs,
            }
        ],
        "findings": [
            {
                "statement": "Anonymous requests disclose another customer's account",
                "target_node_id": TARGET_NODE,
                "evidence_refs": refs,
            }
        ],
    }
    first = await registry.call("graph.record", args, _recording_context(deps))
    finding = first.payload["findings"][0]
    assert first.ok and finding["status"] == "recorded"
    hid = finding["hypothesis_id"]
    assert llm.calls == 1
    assert await _posthoc_complete(store, service, hid, refs, kind="poc") == []
    repeat = await registry.call("graph.record", args, _recording_context(deps))
    assert repeat.payload["findings"][0]["hypothesis_id"] == hid
    assert len(await service.list_findings()) == 1
    assert await _posthoc_complete(store, service, hid, refs, kind="poc") == []
    llm._response = {**_refuted_llm(), "hypothesis_outcome": "refuted"}
    reversed_args = deepcopy(args)
    reversed_args["findings"][0]["evidence_refs"] = list(reversed(refs))
    latest = await registry.call("graph.record", reversed_args, _recording_context(deps))
    assert latest.payload["findings"][0]["status"] == "refused"
    assert (await service.get_hypothesis(hid)).status == "rejected"
    assert await _posthoc_complete(store, service, hid, refs, kind="poc")
    assert (
        await _posthoc_complete(
            store, service, hid, refs, kind="hypothesis_refuted", outcome="refuted"
        )
        == []
    )
    assert [(await store.get(ref)).model_dump(mode="json") for ref in refs] == originals


async def test_unresolved_reference_cannot_borrow_an_existing_finding_for_poc():
    store, llm, service, deps, registry, refs = await _recording_world(
        response={**_success_llm(), "hypothesis_outcome": "success"}
    )
    args = {
        "findings": [
            {
                "statement": "account data exposed",
                "target_node_id": TARGET_NODE,
                "evidence_refs": refs,
            }
        ]
    }
    first = await registry.call("graph.record", args, _recording_context(deps))
    entry = first.payload["findings"][0]
    llm._response = {**_success_llm(), "hypothesis_outcome": "unknown"}
    changed = {
        "findings": [
            {
                "statement": "a broader unrelated disclosure",
                "hypothesis_id": entry["hypothesis_id"],
                "target_node_id": TARGET_NODE,
                "evidence_refs": refs,
            }
        ]
    }
    refused = await registry.call("graph.record", changed, _recording_context(deps))
    assert refused.payload["findings"][0]["status"] == "refused"
    assert await _posthoc_complete(
        store,
        service,
        entry["hypothesis_id"],
        [refused.payload["findings"][0]["receipt_id"]],
        kind="poc",
    )
    assert len(await service.list_findings()) == 1


@pytest.mark.parametrize("outcome", ["blocked", "inconclusive"])
async def test_unresolved_result_is_durable_gap_and_cannot_prove_success(outcome):
    from redteam.orchestration.analysis import is_final_admission_receipt
    from redteam.orchestration.completion_proposal import (
        CompletionProposal,
        record_unresolved,
        validate_unresolved,
    )

    store, _, service, _, _, _ = await _recording_world()
    runtime = RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT)
    proposal = CompletionProposal(
        outcome=outcome,
        reason="no supporting source",
        gaps=[{"code": "insufficient_evidence", "description": "missing actual response"}],
    )
    assert (
        await validate_unresolved(
            proposal, evidence_store=store, runtime=runtime, target_node_ids=[TARGET_NODE]
        )
        == []
    )
    plan = _path(_path_step())
    receipt_id = await record_unresolved(
        evidence_store=store, runtime=runtime, proposal=proposal, path=plan, step_id=STEP_ID
    )
    gap = await store.get(receipt_id)
    assert gap.metadata["outcome"] == outcome and gap.metadata["claim_supported"] is False
    assert not is_final_admission_receipt(gap)
    assert await _posthoc_complete(store, service, HYP_ID, [receipt_id], kind="poc")
    assert await service.list_findings() == []


async def test_completion_reference_feedback_identifies_original_submission_positions():
    from redteam.orchestration.cognition import DecisionBudget
    from redteam.orchestration.engine import InvestigationEngine

    store, _, service, _, _, refs = await _recording_world()
    invalid = await store.get(refs[1])
    invalid.metadata.pop("path_id")
    private_id = "opaquePrivateMissingReference"
    issues = await _posthoc_complete(
        store,
        service,
        None,
        [refs[0], private_id, refs[1]],
        kind="evidence",
    )
    feedback = InvestigationEngine._safe_errors(issues, DecisionBudget(3))
    assert any(
        "$.completion.evidence_ids[1]" in item and "does not exist" in item for item in feedback
    )
    assert any(
        "$.completion.evidence_ids[2]" in item and "no path linkage" in item for item in feedback
    )
    assert private_id not in " ".join(feedback)
    assert refs[1] not in " ".join(feedback)


async def test_expanded_receipt_reference_failure_points_to_submitted_receipt_position():
    store, _, service, _, _, refs = await _recording_world()
    original = await store.get(refs[1])
    original.metadata.pop("path_id")
    proposal = _proposal(action_id="record-action-2", hyp_id=None)
    result = _result().model_copy(update={"invocation_id": InvocationId("record-inv-2")})
    receipt = await append_review_receipt(
        evidence_store=store,
        proposal=proposal,
        result=result,
        assessment=VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason="reviewed source",
            reviewer_id="analysis:test",
            source_refs=[refs[1]],
            outcome="unknown",
            claim_supported=False,
        ),
        admission="final",
    )
    issues = await _posthoc_complete(
        store,
        service,
        None,
        [refs[0], str(receipt.evidence_id)],
        kind="evidence",
    )
    diagnostics = [d for d in issues.diagnostics if "no_path_linkage" in d.code]
    assert len(diagnostics) == 1
    assert diagnostics[0].location == "$.completion.evidence_ids[1]"
    assert refs[1] not in diagnostics[0].feedback()


@pytest.mark.parametrize("prefix", [("claims",), ("criteria", 1, "claims")])
async def test_result_claim_reference_location_uses_claim_local_submission(prefix):
    from redteam.orchestration.result_claims import check_result_review

    store, _, service, _, _, refs = await _recording_world()
    invalid = await store.get(refs[1])
    invalid.metadata.pop("path_id")
    issues = await check_result_review(
        claims=[
            {"description": "second source", "kind": "observation", "evidence_ids": [refs[1]]},
        ],
        evidence_ids=refs, evidence_store=store, graph_service=service,
        path_store=None, run_id=str(RUN), mission_id=None,
        claims_location=prefix,
    )
    diagnostics = [d for d in issues.diagnostics if "no_path_linkage" in d.code]
    assert len(diagnostics) == 1
    expected = "$.claims[0].evidence_ids[0]" if len(prefix) == 1 else (
        "$.criteria[1].claims[0].evidence_ids[0]"
    )
    assert diagnostics[0].location == expected
