"""Probe graph.record candidate references fail closed before any write.

Real probe Task, real GraphRecordingHandler, and real candidate store. Only the verifier meaning
answers are scripted, and no provider request is made by these tests.
"""

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.graph_recording import GraphRecordingHandler
from redteam.orchestration.local_tools import LocalToolContext
from tests.integration.test_central_development import TARGET
from tests.integration.test_hypothesis_planner import candidate, probe, selection
from tests.integration.test_observation_candidate_chain_06d import (
    ChainLLM,
    MeaningLLM,
    _commit,
    _meaning,
    _observe_candidate,
    _receipt,
    _receipts,
)
from tests.integration.test_probe_workers import register

PROBE_HYPOTHESES = "probe_candidate_change_requires_planner"
PROBE_FINDING = "probe_finding_candidate_reference"
REVIEW_TEXT = "SECRET-STATEMENT-TOKEN reflects the candidate premise"
UNKNOWN_ID = "ghost-candidate-zz"


def _finding(**overrides):
    return {"statement": REVIEW_TEXT, "evidence_refs": [], "target_node_id": TARGET, **overrides}


def _handler(session, service, meaning):
    verifier = StructuredLLMAnalysisVerifier(meaning, session.deps.evidence_store, service)
    return GraphRecordingHandler(
        evidence_store=session.deps.evidence_store, graph_service=service, verifier=verifier,
    )


async def _snapshot(session, service, task_id):
    """Everything a refused call could touch: task, both candidates, findings, receipts, records."""
    state = session.deps.central_state
    candidates = [item.model_dump(mode="json") for item in await service.list_hypotheses()]
    return (
        (await state.get_task(task_id)).model_dump(mode="json"),
        candidates,
        await service.list_findings(),
        await _receipts(session),
        len(await session.deps.evidence_store.list_for_run(session.runtime.run_id)),
    )


async def _scoped_probe(session):
    """One probe admitted for the local candidate only; "other" exists but is not admitted."""
    service = AttackGraphService(session.deps.graph_store)
    await _commit(session, "local-open", candidate("local"))
    await _commit(session, "other-open", candidate("other"))
    task_id = await register(session, "scoped", hypotheses={"local": 1})
    worker, = await ready_workers(session.deps, session.runtime)
    return service, task_id, worker


@pytest.mark.parametrize(
    ("assignment", "rule"),
    [
        ({}, "required_admitted_candidate_id"),
        ({"hypothesis_id": None}, "required_admitted_candidate_id"),
        ({"hypothesis_id": "   "}, "required_admitted_candidate_id"),
        ({"hypothesis_id": UNKNOWN_ID}, "admitted_candidate_id_in_hypothesis_revisions"),
        ({"hypothesis_id": "other"}, "admitted_candidate_id_in_hypothesis_revisions"),
    ],
    ids=["missing", "null", "blank", "unknown", "unassigned"],
)
async def test_unadmitted_finding_candidate_fails_closed_without_writes(tmp_path, assignment, rule):
    meaning = MeaningLLM([])
    async with selection(tmp_path, llm=ChainLLM([])) as (session, llm, tools):
        service, task_id, worker = await _scoped_probe(session)
        before = await _snapshot(session, service, task_id)
        handler = _handler(session, service, meaning)
        result = await handler({
            "branches": [{"question": "Check a separate question", "evidence_refs": []}],
            "findings": [_finding(**assignment)],
        }, LocalToolContext(session.deps, worker))

        assert result.ok is False
        diagnostic = result.payload["diagnostic"]
        location = "$.findings[0].hypothesis_id"
        assert diagnostic["code"] == PROBE_FINDING
        assert diagnostic["retryable"] is True
        assert diagnostic["cause_scope"] == "model_input"
        assert diagnostic["owner"] == "graphrecording"
        assert diagnostic["location"] == location
        assert diagnostic["validation_issues"] == [{"rule": rule, "location": location}]
        assert UNKNOWN_ID not in json.dumps(result.payload) + result.error
        assert "SECRET-STATEMENT-TOKEN" not in json.dumps(result.payload) + result.error
        assert await _snapshot(session, service, task_id) == before
        assert meaning.review_calls == 0


async def test_restated_candidate_proposal_is_refused_with_planner_feedback(tmp_path):
    meaning = MeaningLLM([])
    async with selection(tmp_path, llm=ChainLLM([])) as (session, llm, tools):
        service, task_id, worker = await _scoped_probe(session)
        before = await _snapshot(session, service, task_id)
        handler = _handler(session, service, meaning)
        result = await handler({
            "hypotheses": [{"statement": "SECRET-STATEMENT-TOKEN start a new investigation"}],
            "findings": [_finding(hypothesis_id=UNKNOWN_ID)],
        }, LocalToolContext(session.deps, worker))

        assert result.ok is False
        diagnostic = result.payload["diagnostic"]
        assert diagnostic["code"] == PROBE_HYPOTHESES
        assert diagnostic["retryable"] is False
        assert diagnostic["cause_scope"] == "model_input"
        assert diagnostic["owner"] == "graphrecording"
        assert diagnostic["location"] == "$.hypotheses"
        assert diagnostic["summary"] == (
            "Probe calls cannot register candidate proposals; no items recorded"
        )
        assert "SECRET-STATEMENT-TOKEN" not in json.dumps(result.payload) + result.error
        assert UNKNOWN_ID not in json.dumps(result.payload) + result.error
        assert await _snapshot(session, service, task_id) == before
        assert meaning.review_calls == 0


async def _observed_probe(session, after_task):
    """A probe scoped to the canonical candidate that the same-run observation reviewed."""
    task_id = await register(
        session, "scoped", hypotheses={after_task.hypothesis_id: after_task.revision},
    )
    worker, = await ready_workers(session.deps, session.runtime)
    return AttackGraphService(session.deps.graph_store), task_id, worker


OPENING = {"updates": [candidate()], "tasks": [probe(candidate="local")],
           "reason": "Observe the candidate's smallest uncertainty"}


async def test_padded_admitted_candidate_reaches_existing_review(tmp_path):
    meaning = MeaningLLM([_meaning("success")])
    async with selection(tmp_path, llm=ChainLLM([OPENING])) as (session, llm, tools):
        after_task, source = await _observe_candidate(session, llm)
        service, task_id, worker = await _observed_probe(session, after_task)
        handler = _handler(session, service, meaning)
        cited = [str(source.evidence_id)]
        finding = _finding(hypothesis_id=f"  {after_task.hypothesis_id}  ", evidence_refs=cited)
        context = LocalToolContext(session.deps, worker, budget=DecisionBudget())
        result = await handler({"findings": [finding]}, context)

        assert result.ok is True, result.payload["findings"][0].get("reason")
        receipt = await _receipt(session, result)
        assert receipt.metadata["record_kind"] == "review_receipt"
        assert meaning.review_calls == 1


async def test_corrected_candidate_after_feedback_reaches_existing_review(tmp_path):
    meaning = MeaningLLM([_meaning("success")])
    async with selection(tmp_path, llm=ChainLLM([OPENING])) as (session, llm, tools):
        after_task, source = await _observe_candidate(session, llm)
        service, task_id, worker = await _observed_probe(session, after_task)
        before = await _snapshot(session, service, task_id)
        handler = _handler(session, service, meaning)
        context = LocalToolContext(session.deps, worker, budget=DecisionBudget())
        cited = [str(source.evidence_id)]

        first = await handler({"findings": [_finding(evidence_refs=cited)]}, context)
        assert first.ok is False
        assert first.payload["diagnostic"]["code"] == PROBE_FINDING
        assert first.payload["diagnostic"]["location"] == "$.findings[0].hypothesis_id"
        assert await _snapshot(session, service, task_id) == before
        assert meaning.review_calls == 0

        finding = _finding(hypothesis_id=after_task.hypothesis_id, evidence_refs=cited)
        second = await handler({"findings": [finding]}, context)
        assert second.ok is True, second.error
        receipt = await _receipt(session, second)
        assert receipt.metadata["record_kind"] == "review_receipt"
        assert meaning.review_calls == 1
