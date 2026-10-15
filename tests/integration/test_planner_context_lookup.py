"""Explicit scripted Planner reads, shared budgets and original CAS; no live calls."""

import json
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Hypothesis, HypothesisUpdate
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import EvidenceSourceType, TaskStatus
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.hypothesis_planner import PLANNING_INVALIDATED
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET
from tests.integration.test_hypothesis_planner import PlannerDouble
from tests.integration.test_hypothesis_planner import held as legacy_held
from tests.integration.test_probe_workers import PROFILE, selection
from tests.integration.test_v3_atomization import (
    SplitDouble,
    _atom_two_tasks,
    _condition_id,
    _plan_two_questions,
)
from tests.unit.test_central_state import task


def lookup(**query):
    return {"reason": "Read the relevant original", "questions": [], "termination": "continue",
            "context_request": {"version": 1, "context": query}}


def single_lookup(**query):
    return {"reason": "Read the relevant original", "context_request": {
        "version": 1, "context": query}}


def held():
    return {**legacy_held(), "questions": []}


async def seed_history(state, count=100):
    def seed(tx):
        mission_id, _ = tx.binding()
        for index in range(count):
            item = task(f"history-{index:04d}", mission_id=mission_id).model_copy(update={
                "status": TaskStatus.COMPLETED, "required": False,
            })
            tx.db.execute("INSERT INTO state_tasks VALUES(?,?,?)", (
                tx.run_id, str(item.task_id), item.model_dump_json(),
            ))
        source = task("z-source", mission_id=mission_id).model_copy(update={
            "status": TaskStatus.COMPLETED, "required": False,
            "objective": "Earlier principal check", "related_attack_graph_node_ids": [TARGET],
            "result": InvestigationResult.model_validate({
                "contract_version": 2, "result_id": "old-result", "outcome": "inconclusive",
                "summary": "Only the initial principal was observed", "observations": [],
                "limitations": ["Another premise remains untested"],
            }),
        })
        tx.db.execute("INSERT INTO state_tasks VALUES(?,?,?)", (
            tx.run_id, str(source.task_id), source.model_dump_json(),
        ))
    await state.transaction(seed)


def followup(payload):
    original = payload["context_lookup_result"]["result"]["records"][0]["data"]
    assert original["result"]["result_id"] == "old-result"
    assert original["result"]["limitations"] == ["Another premise remains untested"]
    q = _plan_two_questions()["questions"][0]
    q.update(source_task_id="z-source", source_result_id="old-result")
    return {"questions": [q], "reviewed_task_ids": ["z-source"],
            "reason": "Test a different premise using the existing original",
            "termination": "continue"}


def atom(payload):
    source, = payload["investigations"]
    assert source["task_id"] == "z-source" and source["result"]["result_id"] == "old-result"
    assert "model_usage" not in payload and "review_receipts" not in payload
    return {"tasks": [{"task_key": "followup", "question_id": "q1", "title": "New premise",
        "objective": "Inspect a new premise", "role_id": "probe-worker",
        "target_node_ids": [TARGET], "principal_refs": [], "evidence_ids": [],
        "probe_limits": {"max_actions": 1, "max_model_requests": 4},
        "completion_criterion_ids": [_condition_id("Inspect first")]}]}


async def test_search_read_followup_one_pass_and_no_lookup_commit(tmp_path):
    calls = [lookup(kind="task", mode="list", text="Earlier principal", count=1)]
    async with selection(tmp_path, llm=SplitDouble(calls, [atom])) as (session, llm, tools):
        state = session.deps.central_state
        await seed_history(state)

        async def read_original(payload):
            assert "z-source" not in {item["task_id"] for item in payload["investigations"]}
            page = payload["context_lookup_result"]["result"]
            assert page["count"] == 1 and page["records"][0]["identity"] == "z-source"
            assert await state.transaction(lambda tx: tx.hypothesis_planner_cursor(),
                                           write=False) is None
            assert len(await state.list_tasks()) == 101
            return lookup(kind="task", mode="read", refs=[{"kind": "task", "identity": "z-source"}])

        llm.plans.extend([read_original, followup])
        identities = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(identities) == 1
        saved = await state.get_task(identities[0])
        assert saved.source_task_id == "z-source" and saved.source_result_id == "old-result"
        ledger = await state.request_ledger().snapshot(state.run_id)
        assert ledger["spent"] == 4  # search, read, plan, atomizer; one normal budget
        assert not tools.inner.invocations
        assert len(llm.plan_inputs) == 3 and len(llm.atom_inputs) == 1


@pytest.mark.parametrize("change_related", [False, True])
async def test_lookup_source_drift_uses_related_cas_not_whole_inventory(tmp_path, change_related):
    llm = SplitDouble([lookup(kind="task", mode="read",
                             refs=[{"kind": "task", "identity": "z-source"}])], [atom])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        state = session.deps.central_state
        await seed_history(state)

        async def race(payload):
            def mutate(tx):
                identity = "z-source" if change_related else "history-0099"
                item = tx.get_task(identity).model_copy(update={"objective": "Concurrent change"})
                tx.db.execute("UPDATE state_tasks SET payload=? WHERE run_id=? AND task_id=?", (
                    item.model_dump_json(), tx.run_id, identity,
                ))
            await state.transaction(mutate)
            return followup(payload)

        llm.plans.append(race)
        result = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert bool(result) is not change_related
        cursor = await state.transaction(lambda tx: tx.hypothesis_planner_cursor(), write=False)
        assert (cursor is None) is change_related
        if change_related:
            assert await state.transaction(
                lambda tx: tx.latest_event_sequence((PLANNING_INVALIDATED,)), write=False) > 0


async def test_lookup_and_commit_mix_repairs_without_partial_state(tmp_path):
    bad = {**lookup(kind="task", mode="list"), "questions": _plan_two_questions()["questions"]}
    llm = SplitDouble([bad, held()], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 2 and not llm.atom_inputs
        assert await session.deps.central_state.list_tasks() == []
        assert "planner_lookup_cannot_commit" in json.dumps(llm.requests)


async def test_repeated_missing_lookup_exhausts_normal_budget_without_cursor(tmp_path):
    request = lookup(kind="task", mode="read", refs=[{"kind": "task", "identity": "missing"}])
    llm = SplitDouble([request] * 40, [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(session.deps).advance(session.runtime)
        assert failed.value.kind == "budget"
        assert failed.value.diagnostic.code == "planner_lookup_budget_exhausted"
        assert len(llm.plan_inputs) == session.deps.settings.decision_normal_limit
        assert len(llm.plan_inputs) <= 32
        assert all(item["context_lookup_result"]["result"]["status"] == "error"
                   for item in llm.plan_inputs[1:])
        assert await session.deps.central_state.transaction(
            lambda tx: tx.hypothesis_planner_cursor(), write=False) is None
        assert await session.deps.central_state.list_tasks() == []


async def test_hidden_reviewed_ids_repair_but_valid_source_is_not_projection_gated(tmp_path):
    q = _plan_two_questions()["questions"][0]
    q.update(source_task_id="z-source", source_result_id="old-result")
    llm = SplitDouble([
        {"questions": [q], "reviewed_task_ids": ["z-source"], "reason": "Claim hidden review",
         "termination": "continue"},
        {"questions": [q], "reason": "Use a valid supplied source reference",
         "termination": "continue"},
    ], [atom])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        await seed_history(session.deps.central_state)
        assert len(await SpecialistPlanner(session.deps).advance(session.runtime)) == 1
        assert len(llm.plan_inputs) == 2
        assert "planner_reviewed_reference_not_presented" in json.dumps(llm.requests)


async def test_single_stage_compatibility_consumes_optional_lookup(tmp_path):
    llm = PlannerDouble([single_lookup(kind="task", mode="list"), legacy_held()])
    async with selection(tmp_path, llm=llm, profile=PROFILE.with_name(
            "native-probes-legacy.yaml")) as (session, llm, _):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.planner_inputs) == 2
        assert llm.planner_inputs[1]["context_lookup_result"]["result"]["count"] == 0


async def test_omitted_terminal_candidate_and_public_receipt_are_readable(tmp_path):
    llm = SplitDouble([lookup(kind="candidate", mode="list", statuses=["rejected"],
                             text="Archived marker", count=1)], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        from redteam.attack_graph.hypotheses import hypothesis_purpose

        state = session.deps.central_state

        def seed(tx):
            for index in range(100):
                identity = f"archive-{index:04d}" if index < 99 else "z-archive"
                candidate = Hypothesis(
                    contract_version=2, hypothesis_id=identity,
                    description="Archived marker" if index == 99 else "Historical claim",
                    target_node_ids=[TARGET], reason="Synthetic reviewed premise",
                    status="rejected" if index == 99 else "confirmed",
                )
                tx.graph.save_hypothesis(HypothesisUpdate.model_validate({
                    "hypothesis": candidate.model_dump(mode="json"),
                    "review_evidence_id": "synthetic-archived-review"}),
                                         now=datetime.now(timezone.utc))
            return candidate.model_copy(update={"revision": 1})
        candidate = await state.transaction(seed)
        meta = {"record_kind": "review_receipt", "public": True, "admission": "final",
                "hypothesis_id": "z-archive", "candidate_revision": 1,
                "candidate_purpose_hash": content_hash(hypothesis_purpose(candidate)),
                "hypothesis_outcome": "refuted", "verdict": "pass", "reason": "Scoped negative",
                "private_reasoning": "Must not appear in Planner input"}
        await session.deps.evidence_store.append(EvidenceRecord(
            evidence_id="old-public-review", run_id=session.runtime.run_id,
            engagement_id=session.runtime.engagement_id, source_type=EvidenceSourceType.SYSTEM,
            source_id="fixture-independent-review", content_type="application/json",
            digest=content_hash(meta), created_at=datetime.now(timezone.utc), metadata=meta,
        ))

        def read_candidate(payload):
            assert "z-archive" not in {item["hypothesis_id"] for item in payload["candidates"]}
            page = payload["context_lookup_result"]["result"]
            assert page["count"] == 1 and page["records"][0]["identity"] == "z-archive"
            return lookup(kind="candidate", mode="read",
                          refs=[{"kind": "candidate", "identity": "z-archive"}])

        def search_receipts(payload):
            detail = payload["context_lookup_result"]["result"]["records"][0]["data"]
            assert detail["status"] == "rejected" and detail["revision"] == 1
            return {"reason": "Find the independent original", "questions": [],
                    "termination": "continue", "context_request": {"evidence": {
                "mode": "list", "hypothesis_id": "z-archive", "record_kind": "review_receipt"}}}

        def read_receipt(payload):
            page = payload["context_lookup_result"]["result"]
            assert page["records"][0]["evidence_id"] == "old-public-review"
            return {"reason": "Read that public receipt", "questions": [],
                    "termination": "continue", "context_request": {"evidence": {
                "mode": "read", "evidence_id": "old-public-review"}}}

        def finish(payload):
            record = payload["context_lookup_result"]["result"]
            assert record["status"] == "ok" and '"candidate_revision": 1' in record["text"]
            assert record["validity"]["status"] == "unbound"
            assert "private_reasoning" not in json.dumps(payload)
            return {**held(), "reviewed_hypothesis_ids": ["z-archive"]}

        llm.plans.extend([read_candidate, search_receipts, read_receipt, finish])
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 5 and not llm.atom_inputs


async def test_stale_query_is_an_error_not_an_empty_success(tmp_path):
    llm = SplitDouble([lookup(kind="task", mode="list", snapshot_id="0" * 64)], [])

    def finish(payload):
        result = payload["context_lookup_result"]["result"]
        assert result["status"] == "error" and result["code"] == "context_snapshot_changed"
        return held()

    llm.plans.append(finish)
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 2


async def test_large_parallel_frontier_is_summary_not_automatic_omission_gate(tmp_path):
    def decompose(payload):
        assert payload["investigations"] == []
        view = payload["related_work"]
        assert view["count"] == 200 and view["omitted_count"] >= 196
        assert view["summary"]["states"]["task"]["blocked"] == 200
        assert len(json.dumps(payload)) < 50000
        return _atom_two_tasks()

    llm = SplitDouble([_plan_two_questions()], [decompose])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        state = session.deps.central_state
        await seed_history(state, count=200)

        def pending(tx):
            for original in tx.list_tasks():
                if str(original.task_id).startswith("history-"):
                    item = original.model_copy(update={
                        "status": TaskStatus.BLOCKED, "related_attack_graph_node_ids": [TARGET],
                        "blocking_reason": "Unresolved premise",
                    })
                    tx.db.execute("UPDATE state_tasks SET payload=? WHERE run_id=? AND task_id=?", (
                        item.model_dump_json(), tx.run_id, str(item.task_id),
                    ))
        await state.transaction(pending)
        assert len(await SpecialistPlanner(session.deps).advance(session.runtime)) == 2
        assert len(llm.plan_inputs) == len(llm.atom_inputs) == 1


async def test_historical_receipt_index_stays_bounded_but_searchable(tmp_path):
    def finish(payload):
        page = payload["context_lookup_result"]["result"]
        assert page["count"] == 1000 and len(page["records"]) == 2
        assert page["next_offset"] == 2 and page["omitted_count"] == 998
        assert payload["review_receipts"] == []  # No exact-bound candidate in the frontier.
        assert len(payload["evidence_index"]) <= 10
        assert payload["evidence_summary"]["total"] == 1000
        assert len(json.dumps(payload)) < 50000
        return held()

    request = {"reason": "Search historical public reviews", "questions": [],
               "termination": "continue", "context_request": {"evidence": {
        "mode": "list", "record_kind": "review_receipt", "count": 2}}}
    llm = SplitDouble([request, finish], [])
    async with selection(tmp_path, llm=llm) as (session, llm, _):
        for index in range(1000):
            meta = {"record_kind": "review_receipt", "public": True, "admission": "final",
                    "hypothesis_id": f"archived-{index}", "candidate_revision": 1,
                    "reason": "Synthetic archived review " + "x" * 100}
            await session.deps.evidence_store.append(EvidenceRecord(
                evidence_id=f"archived-review-{index}", run_id=session.runtime.run_id,
                engagement_id=session.runtime.engagement_id, source_type=EvidenceSourceType.SYSTEM,
                source_id="fixture-independent-review", content_type="application/json",
                digest=content_hash(meta), created_at=datetime.now(timezone.utc), metadata=meta,
            ))
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.plan_inputs) == 2
