"""Legacy investigation planning uses the shared bounded progression contract."""

import asyncio

from redteam.attack_graph.service import AttackGraphService
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.hypothesis_planner import PLANNING_INVALIDATED
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import register as register_legacy_task
from tests.integration.test_hypothesis_planner import PlannerDouble, selection
from tests.integration.test_probe_workers import PROFILE, register
from tests.integration.test_probe_workers import selection as profile_selection


async def test_legacy_pathless_continue_repairs_to_one_bounded_replan(tmp_path):
    llm = FakeLLMClient(structured_responses=[
        {"questions": [], "reason": "No questions were proposed", "termination": "continue"},
        {"questions": [], "reason": "Request one finite reconsideration",
         "termination": "replan"},
    ])
    async with profile_selection(
        tmp_path, profile=PROFILE.with_name("native-specialists.yaml"), llm=llm,
    ) as (session, llm, tools):
        planner = SpecialistPlanner(session.deps)
        tasks_before = await session.deps.central_state.list_tasks()

        assert await planner.advance(session.runtime) == []

        tasks_after = await session.deps.central_state.list_tasks()
        status = await session.deps.central_state.planner_progress_status()
        sequence = await session.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        )
        assert len(llm.requests) == 2
        assert tasks_after == tasks_before
        assert status is not None and status.kind == "replan_queued"
        assert (status.replan_count, status.replan_limit) == (1, 3)
        assert status.cause_code == "planner_replan_requested"
        assert sequence == 1


async def test_legacy_continue_requires_a_canonical_claimable_task_witness(tmp_path):
    llm = FakeLLMClient()
    async with profile_selection(
        tmp_path, profile=PROFILE.with_name("native-specialists.yaml"), llm=llm,
    ) as (session, llm, tools):
        task_id, = await register_legacy_task(session, "legacy-continue-witness")
        llm._canned_structured = {
            "questions": [],
            "reason": "Continue with the registered ready investigation",
            "termination": "continue",
            "reviewed_task_ids": [task_id],
        }

        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []

        status = await session.deps.central_state.planner_progress_status()
        assert status is not None and status.kind == "existing_work"
        assert (status.replan_count, status.replan_limit) == (0, 3)
        assert await session.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        ) == 0


async def test_referenced_ready_work_drift_invalidates_continue_snapshot(tmp_path):
    llm = PlannerDouble([])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        task_id = await register(session, "continue-witness")
        llm.decisions.append(lambda _payload: {
            "reviewed_task_ids": [task_id], "reason": "Continue with this ready task",
        })
        llm.request_release.clear()
        planning = asyncio.create_task(SpecialistPlanner(session.deps).advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            workers = await ready_workers(session.deps, session.runtime, max_instances=10)
            assert any(str(worker.active_task_id) == task_id for worker in workers)
        finally:
            llm.request_release.set()

        assert await planning == []
        sequence = await session.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        )
        assert sequence is not None


async def test_ready_witness_candidate_graph_drift_invalidates_unchanged_task(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate

    llm = PlannerDouble([])
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        task_id = await register(
            session, "candidate-witness", updates=[candidate()], hypotheses={"local": 1},
        )
        state = session.deps.central_state
        task_before = await state.get_task(task_id)
        current = await AttackGraphService(session.deps.graph_store).get_hypothesis("local")
        llm.decisions.append(lambda _payload: {
            "reviewed_task_ids": [task_id], "reason": "Continue this candidate-bound task",
        })
        llm.request_release.clear()
        planning = asyncio.create_task(SpecialistPlanner(session.deps).advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            await register(
                session, "revise-witness-candidate",
                updates=[candidate(current=current.model_dump(mode="json"), status="held")],
                hypotheses={"local": 2},
            )
        finally:
            llm.request_release.set()

        assert await planning == []
        assert await state.get_task(task_id) == task_before
        assert task_before.hypothesis_revisions == {current.hypothesis_id: 1}
        assert current.revision == 1
        passes = await state.transaction(lambda tx: tx.db.execute(
            "SELECT COUNT(*) FROM state_requests WHERE run_id=? "
            "AND kind='hypothesis_planner_pass'",
            (tx.run_id,),
        ).fetchone()[0], write=False)
        assert passes == 0
        sequence = await state.transaction(
            lambda tx: tx.latest_event_sequence([PLANNING_INVALIDATED]), write=False,
        )
        assert sequence is not None


async def test_candidate_only_continue_repairs_to_replan_in_both_probe_schemas(tmp_path):
    from tests.integration.test_hypothesis_planner import candidate

    for index, profile in enumerate(("native-probes-legacy.yaml", "native-probes.yaml")):
        llm = PlannerDouble([
            {"updates": [candidate()], "reason": "Revise this candidate"},
            {"updates": [candidate()], "reason": "Request a bounded planning pass",
             "termination": "replan"},
        ])
        async with profile_selection(
            tmp_path / str(index), profile=PROFILE.with_name(profile), llm=llm,
        ) as (session, llm, tools):
            assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
            assert len(llm.requests) == 2
            assert "continue_requires_proposed_or_existing_work" in (
                llm.requests[1]["messages"][-1]["content"]
            )
            hypotheses = await AttackGraphService(session.deps.graph_store).list_hypotheses()
            assert len(hypotheses) == 1 and hypotheses[0].revision == 1
