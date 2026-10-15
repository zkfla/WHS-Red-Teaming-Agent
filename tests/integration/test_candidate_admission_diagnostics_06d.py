"""06D admission diagnostics through the real planner, canonical store and receipt guard.

Only the model/provider boundary is scripted. Evidence, graph, Task and outbox rows are real.
"""

import pytest

from redteam.attack_graph.identity import hypothesis_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import Verdict
from redteam.orchestration.analysis import VerificationAssessment, append_posthoc_claim_receipt
from redteam.orchestration.central_state import RECEIPT_REASONS, check_evidence
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.coordination import CandidateAdmissionConflict, content_hash
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_development import TARGET, TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import PlannerDouble, candidate, probe, selection
from tests.integration.test_probe_workers import drive

FIRST = {"updates": [candidate()], "tasks": [probe(candidate="local")],
         "reason": "Observe the candidate's smallest uncertainty"}


async def _observe(session, llm):
    """One real observation Task that completes with a preserved result."""
    first_id, = await SpecialistPlanner(session.deps).advance(session.runtime)
    llm.native_responses_by_task[first_id] = [discover(TOOL), turn(inspect("observed"))]
    await drive(session)
    return first_id


async def _state(session):
    """Task, candidate and outbox rows that a rejected batch must leave untouched."""
    state = session.deps.central_state
    tasks = sorted(str(task.task_id) for task in await state.list_tasks())
    outbox = await state.transaction(lambda tx: tx.db.execute(
        "SELECT count(*) FROM state_outbox WHERE run_id=?", (tx.run_id,),
    ).fetchone()[0], write=False)
    service = AttackGraphService(session.deps.graph_store)
    candidates = {item.hypothesis_id: (item.revision, item.status)
                  for item in await service.list_hypotheses()}
    return {"tasks": tasks, "outbox": outbox, "candidates": candidates}


def _followup(current, source, first_id, *, question):
    """A follow-up proposal citing the exact preserved source Task result."""
    source_ref = {"task_id": first_id, "result": source.result.model_dump(mode="json")}
    update = candidate(current=current.model_dump(mode="json"),
                       evidence=source.result.evidence_ids)
    task = probe("followup", candidate=current.hypothesis_id,
                 revision=current.revision + 1, source=source_ref)
    if question is not None:
        task["source_question_id"] = question
    return {"updates": [update], "tasks": [task]}


async def _register(session, key, proposal_dict, *, source_ids=()):
    """Canonical probe admission through the real producer and CentralState transaction."""
    state = session.deps.central_state
    service = AttackGraphService(session.deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
    proposal = HypothesisProbeProposal.model_validate(proposal_dict)
    subjects = {TARGET}
    subjects.update(str(hypothesis_node_id(item.hypothesis.hypothesis_id))
                    for item in proposal.updates)
    subjects.update(str(hypothesis_node_id(ref)) for task in proposal.tasks
                    for ref in task.hypothesis_revisions)
    graph_read = {ref: content_hash(await service.get_node(ref)) for ref in subjects}
    task_read = {identity: content_hash(await state.get_task(identity))
                 for identity in source_ids}
    return await register_probe_tasks(
        session.deps, proposal_id=key, proposal=proposal, expected_revision=mission.revision,
        expected_binding_hash=binding, graph_read=graph_read, known_targets={TARGET},
        known_principals=set(), max_tasks=4, task_read=task_read,
    )


async def test_planner_source_question_mismatch_rolls_back_batch_with_typed_diagnostic(tmp_path):
    def followup(payload):
        current, = payload["candidates"]
        source, = payload["investigations"]
        return {"updates": [candidate(current=current, evidence=source["result"]["evidence_ids"])],
                "tasks": [{**probe("followup", candidate=current["hypothesis_id"],
                                  revision=current["revision"] + 1, source=source),
                           "source_question_id": "question-absent"}],
                "reason": "Follow up the same preserved original"}

    llm = PlannerDouble([FIRST, followup])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        state = session.deps.central_state
        source = await state.get_task(first_id)
        before = await _state(session)
        calls = len(llm.planner_inputs)
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        diagnostic = failed.value.diagnostic
        assert failed.value.stage == "hypothesis_planning"
        assert diagnostic.code == "follow_up_source_missing_or_mismatched"
        assert diagnostic.location == "$.source_question_id"
        assert diagnostic.owner == "orchestration.central_state"
        assert diagnostic.cause_scope == "evidence_validity"
        assert diagnostic.retryable is False
        assert diagnostic.attempts == failed.value.attempts >= 1
        assert diagnostic.trace[0]["source_task_id"] == first_id
        assert diagnostic.trace[0]["source_result_id"] == source.result.result_id
        assert diagnostic.trace[0]["source_question_id"] == "question-absent"
        assert len(llm.planner_inputs) == calls + 1
        assert await _state(session) == before


async def test_canonical_source_question_mismatch_rejects_candidate_and_task_together(tmp_path):
    llm = PlannerDouble([FIRST])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        source = await state.get_task(first_id)
        before = await _state(session)
        proposal = _followup(current, source, first_id, question="question-absent")
        with pytest.raises(CandidateAdmissionConflict) as rejected:
            await _register(session, "mismatch", proposal, source_ids=[first_id])
        assert str(rejected.value) == "follow_up_source_missing_or_mismatched"
        assert rejected.value.diagnostic is rejected.value.diagnostics[0]
        assert rejected.value.diagnostic.retryable is False
        assert await _state(session) == before
        assert (await service.get_hypothesis(current.hypothesis_id)).revision == current.revision
        # The same rejected proposal is not replayed as a successful receipt.
        with pytest.raises(CandidateAdmissionConflict):
            await _register(session, "mismatch", proposal, source_ids=[first_id])
        assert await _state(session) == before


async def test_verdict_receipt_guards_use_canonical_inputs_and_keep_positive_replay(tmp_path):
    llm = PlannerDouble([FIRST])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        first_id = await _observe(session, llm)
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)
        current, = await service.list_hypotheses()
        sources = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
        source = next(item for item in sources if item.metadata.get("mission_task_id") == first_id
                      and isinstance(item.metadata.get("tool_result"), dict))
        receipt = await append_posthoc_claim_receipt(
            evidence_store=session.deps.evidence_store, run_id=session.runtime.run_id,
            engagement_id=session.runtime.engagement_id,
            claim={"statement": current.description}, evidence_ids=[str(source.evidence_id)],
            assessment=VerificationAssessment(
                verdict=Verdict.PASS, reviewer_id="fixture-independent-review",
                reason="Synthetic interpretation of preserved original", outcome="success",
                hypothesis_outcome="success", source_refs=[str(source.evidence_id)],
                claim_supported=True,
            ),
            hypothesis_id=current.hypothesis_id, action_id=str(source.action_id),
            invocation_id=str(source.invocation_id), target_node_id=TARGET, candidate=current,
        )
        evidence = [str(source.evidence_id)]
        review = str(receipt.evidence_id)
        base = candidate(current=current.model_dump(mode="json"), status="confirmed",
                         evidence=evidence, review=review)
        before = await _state(session)

        async def verdict(update, request, checked_ids):
            mission = await state.get_mission(session.runtime.mission_id)
            binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
            checked = await check_evidence(session.deps.evidence_store,
                run_id=state.run_id, engagement_id=state.engagement_id,
                evidence_ids=checked_ids)
            # The proposal model refuses an unreviewed verdict before the canonical guard;
            # validate a reviewed copy, then restore the real (possibly absent) review field.
            reviewed = update["review_evidence_id"] or review
            validated = HypothesisProbeProposal.model_validate({
                "updates": [{**update, "review_evidence_id": reviewed}], "tasks": []}).updates
            updates = [item.model_copy(update={"review_evidence_id": update["review_evidence_id"]})
                       for item in validated]
            return await state.transaction(lambda tx: tx.register_hypothesis_probes(
                request, content_hash(request), updates=updates, tasks=[], checked=checked,
                expected_revision=mission.revision, expected_binding_hash=binding,
                graph_read={}, notifications=[],
            ))

        cases = {
            "omitted": ({**base, "review_evidence_id": None}, evidence),
            "revision": ({**base, "expected_revision": 2}, evidence + [review]),
            "purpose": ({**base, "hypothesis": {**base["hypothesis"],
                          "description": "A different purpose at the same revision"}},
                         evidence + [review]),
        }
        for reason, (update, checked_ids) in cases.items():
            with pytest.raises(CandidateAdmissionConflict) as rejected:
                await verdict(update, "receipt-" + reason, checked_ids)
            diagnostic = rejected.value.diagnostic
            assert str(rejected.value) == "candidate_verdict_receipt_unbound"
            assert diagnostic.code == "candidate_verdict_receipt_unbound"
            assert diagnostic.summary == RECEIPT_REASONS[reason]
            assert diagnostic.retryable is False
            assert diagnostic.trace == [{"candidate_id": current.hypothesis_id, "reason": reason}]
            assert await _state(session) == before

        bound = await verdict(base, "receipt-bound", evidence + [review])
        assert bound["candidate_revisions"] == {current.hypothesis_id: current.revision + 1}
        assert await verdict(base, "receipt-bound", evidence + [review]) == bound
        saved = await service.get_hypothesis(current.hypothesis_id)
        assert saved.status == "confirmed" and saved.revision == current.revision + 1


