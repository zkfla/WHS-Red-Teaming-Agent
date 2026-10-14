"""Durable bounded replan budget, its single wake and exhaustion, through the real planner.

Only explicit doubles are used: the fake planner model, the probe worker double and the
original-protocol tool double. Judgments are committed through the central transaction.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from redteam.app.central_run import open_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.enums import TaskStatus
from redteam.orchestration.central_results import RESULT_UPDATED, commit_result, result_for
from redteam.orchestration.central_runtime import ready_workers, runtime_token
from redteam.orchestration.coordination import PlanningInputsChanged, StateConflict, content_hash
from redteam.orchestration.planner_progress import (
    PLANNING_INVALIDATED,
    REPLAN_BUDGET_KIND,
    REPLAN_EXHAUSTED_CODE,
    REPLAN_REQUESTED_REASON,
)
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TARGET, gateway
from tests.integration.test_central_parallel import limits
from tests.integration.test_hypothesis_planner import PlannerDouble, candidate, held, probe
from tests.integration.test_probe_workers import PROFILE, register

LEGACY = "native-probes-legacy.yaml"
SPLIT = "native-probes.yaml"
WITNESS = {"eligible_roles": ["probe-worker"], "require_probe_limits": True}


@asynccontextmanager
async def budgeted(tmp_path, *, llm, cap=3, profile=LEGACY, resume=None, tools=None):
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(
        specialist_profile_path=str(PROFILE.with_name(profile)),
        planner_replan_max_passes=cap,
    )
    config = AppConfig(
        engagement=EngagementConfig.model_validate(
            original.model_dump(exclude={"engagement_id", "created_at"}),
        )
    )
    if tools is None:
        tools = OriginalProtocolDouble(gateway())
    extra = {}
    if resume is not None:
        run_id, engagement_id = resume
        extra = {"resume": True, "run_id": run_id, "engagement_id": engagement_id}
    async with open_central_run(
        config,
        directory=tmp_path / "run",
        settings=configured,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
        **extra,
    ) as session:
        yield session, llm, tools


async def projection(state):
    return await state.transaction(lambda tx: tx.planner_progress_status(), write=False)


def _outbox(tx):
    rows = tx.db.execute(
        "SELECT payload FROM state_outbox WHERE run_id=? ORDER BY sequence",
        (tx.run_id,),
    )
    return [json.loads(raw)["envelope"] for (raw,) in rows]


async def wakes(state):
    envelopes = await state.transaction(_outbox, write=False)
    return [item["payload"] for item in envelopes if item["event_type"] == PLANNING_INVALIDATED]


async def event_types(state):
    envelopes = await state.transaction(_outbox, write=False)
    return [item["event_type"] for item in envelopes]


async def planner_passes(state):
    return await state.transaction(
        lambda tx: tx.db.execute(
            "SELECT COUNT(*) FROM state_requests WHERE run_id=? AND kind='hypothesis_planner_pass'",
            (tx.run_id,),
        ).fetchone()[0],
        write=False,
    )


@pytest.mark.parametrize("profile", [LEGACY, SPLIT])
async def test_one_replan_queues_one_wake_and_projects_the_absolute_count(tmp_path, profile):
    llm = PlannerDouble([{"reason": "Request one bounded planning pass", "termination": "replan"}])
    async with budgeted(tmp_path, llm=llm, profile=profile) as (session, llm, tools):
        state = session.deps.central_state
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        status = await projection(state)
        assert status.kind == "replan_queued"
        assert (status.replan_count, status.replan_limit) == (1, 3)
        assert status.cause_code == REPLAN_REQUESTED_REASON and status.diagnostic is None
        queued = await wakes(state)
        assert [item["reason_code"] for item in queued] == [REPLAN_REQUESTED_REASON]
        assert len(llm.requests) == 1


async def test_replan_with_candidate_update_then_actual_probe_task(tmp_path):
    llm = PlannerDouble(
        [
            {
                "updates": [candidate()],
                "termination": "replan",
                "reason": "Revise the candidate before choosing a probe",
            },
            lambda payload: {
                "tasks": [
                    probe(
                        "observe", candidate=payload["candidates"][0]["hypothesis_id"], revision=1
                    )
                ],
                "reason": "Observe the revised candidate",
            },
        ]
    )
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        planner = SpecialistPlanner(session.deps)
        assert await planner.advance(session.runtime) == []
        assert (await projection(state)).kind == "replan_queued"
        identities = await planner.advance(session.runtime)
        assert len(identities) == 1
        assert (await state.get_task(identities[0])).status is TaskStatus.READY
        status = await projection(state)
        assert status.kind == "work_proposed" and status.replan_count == 1
        assert len(await wakes(state)) == 1


async def test_three_empty_replans_queue_three_wakes_then_the_fourth_is_exhausted(tmp_path):
    llm = PlannerDouble(
        [{"reason": f"Empty follow-up {index}", "termination": "replan"} for index in range(4)]
    )
    async with budgeted(tmp_path, llm=llm, cap=3) as (session, llm, tools):
        state = session.deps.central_state
        planner = SpecialistPlanner(session.deps)
        for expected in (1, 2, 3):
            assert await planner.advance(session.runtime) == []
            status = await projection(state)
            assert (status.kind, status.replan_count) == ("replan_queued", expected)
        assert await planner.advance(session.runtime) == []
        status = await projection(state)
        assert status.kind == "replan_exhausted"
        assert (status.replan_count, status.replan_limit) == (3, 3)
        assert status.diagnostic.code == REPLAN_EXHAUSTED_CODE
        assert status.diagnostic.retryable is False and status.diagnostic.kind == "no_progress"
        assert len(await wakes(state)) == 3
        assert len(llm.requests) == 4
        # The listener stays operational: no further wake or model request without new input.
        assert await planner.advance(session.runtime) == []
        assert len(llm.requests) == 4
        assert len(await wakes(state)) == 3
        assert (await state.get_mission(session.runtime.mission_id)).status.value == "active"


async def test_cap_zero_denies_the_first_follow_up_without_a_wake(tmp_path):
    llm = PlannerDouble([{"reason": "Try one follow-up", "termination": "replan"}])
    async with budgeted(tmp_path, llm=llm, cap=0) as (session, llm, tools):
        state = session.deps.central_state
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        status = await projection(state)
        assert (status.kind, status.replan_count, status.replan_limit) == (
            "replan_exhausted",
            0,
            0,
        )
        assert await wakes(state) == []


async def test_duplicate_delivery_and_resume_do_not_multiply_count_or_requests(tmp_path):
    llm = PlannerDouble(
        [
            {"reason": "Request one bounded pass", "termination": "replan"},
            held(),
        ]
    )
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        # The second pass consumes the queued wake exactly once.
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        requests = len(llm.requests)
        for _ in range(2):  # Redelivered or resumed planners reuse the consumed cursor.
            assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(llm.requests) == requests
        status = await projection(state)
        assert (status.kind, status.replan_count) == ("held", 1)
        assert len(await wakes(state)) == 1


async def test_cas_failure_spends_no_replan_and_commits_no_partial_candidate(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        source_id = await register(session, "source", actions=2)
        workers = await ready_workers(session.deps, session.runtime)
        source = next(item for item in workers if str(item.active_task_id) == source_id)
        llm.decisions.append(
            {
                "updates": [candidate()],
                "reviewed_task_ids": [source_id],
                "termination": "replan",
                "reason": "Revise the candidate after this source",
            }
        )
        llm.request_release.clear()
        planning = asyncio.create_task(SpecialistPlanner(session.deps).advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            task = await state.get_task(source_id)
            result = result_for(
                task,
                outcome="inconclusive",
                summary="Changed during planning",
                evidence_ids=[],
                observations=["synthetic observation"],
            )
            await commit_result(
                session.deps,
                source,
                task,
                result,
                await runtime_token(session.deps, source),
                final=False,
            )
        finally:
            llm.request_release.set()
        assert await planning == []
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        assert await projection(state) is None
        assert await planner_passes(state) == 0
        reasons = [item["reason_code"] for item in await wakes(state)]
        assert reasons == ["related_planning_inputs_changed"]


async def test_unrelated_sibling_change_does_not_invalidate_a_local_replan(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        llm.decisions.append(
            {
                "updates": [candidate()],
                "termination": "replan",
                "reason": "Revise one candidate without reviewing any Task",
            }
        )
        llm.request_release.clear()
        planning = asyncio.create_task(SpecialistPlanner(session.deps).advance(session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(1), 3)
            await register(session, "sibling")
        finally:
            llm.request_release.set()
        assert await planning == []
        hypotheses = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert len(hypotheses) == 1 and hypotheses[0].revision == 1
        status = await projection(state)
        assert (status.kind, status.replan_count) == ("replan_queued", 1)
        assert [item["reason_code"] for item in await wakes(state)] == [REPLAN_REQUESTED_REASON]


async def test_spent_cap_with_running_sibling_then_new_original_result_plans_work(tmp_path):
    llm = PlannerDouble(
        [
            {"reason": "First empty follow-up", "termination": "replan"},
            {"reason": "Second empty follow-up", "termination": "replan"},
            lambda payload: {
                "tasks": [probe("fresh")],
                "reason": "Observe the new original result",
            },
        ]
    )
    async with budgeted(tmp_path, llm=llm, cap=1) as (session, llm, tools):
        state = session.deps.central_state
        sibling_id = await register(session, "running", actions=2)
        workers = await ready_workers(session.deps, session.runtime)
        running = next(item for item in workers if str(item.active_task_id) == sibling_id)
        planner = SpecialistPlanner(session.deps)
        assert await planner.advance(session.runtime) == []
        assert await planner.advance(session.runtime) == []
        status = await projection(state)
        assert (status.kind, status.replan_count, status.replan_limit) == (
            "replan_exhausted",
            1,
            1,
        )
        requests = len(llm.requests)
        assert await planner.advance(session.runtime) == []
        assert len(llm.requests) == requests

        task = await state.get_task(sibling_id)
        result = result_for(
            task,
            outcome="inconclusive",
            summary="New original result",
            evidence_ids=[],
            observations=["new observation"],
        )
        await commit_result(
            session.deps,
            running,
            task,
            result,
            await runtime_token(session.deps, running),
            final=False,
        )
        assert RESULT_UPDATED in await event_types(state)
        identities = await planner.advance(session.runtime)
        assert len(identities) == 1 and len(llm.requests) == requests + 1
        status = await projection(state)
        assert (status.kind, status.replan_count, status.replan_limit) == ("work_proposed", 1, 1)
        assert len(await wakes(state)) == 1


async def test_finish_rejects_an_unclaimable_continue_witness_and_writes_nothing(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        task_id = await register(session, "witness", updates=[candidate()], hypotheses={"local": 1})
        (current,) = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        await register(
            session,
            "revise",
            updates=[
                candidate(
                    current=current.model_dump(mode="json"),
                    status="held",
                )
            ],
            hypotheses={"local": 2},
        )
        # The READY witness still names revision 1 while the canonical candidate is revision 2.
        judgment = {
            "termination": "continue",
            "reason": "Continue on the ready task",
            "resume_conditions": [],
            "reviewed_task_ids": [task_id],
            "sequence": 0,
            "witness_inputs": WITNESS,
            "replan_max_passes": 3,
        }
        with pytest.raises(PlanningInputsChanged):
            await state.transaction(
                lambda tx: tx.finish_hypothesis_judgment("pass-witness", judgment),
            )
        assert await projection(state) is None
        assert await planner_passes(state) == 0
        assert await wakes(state) == []


async def test_current_ready_witness_is_admitted_at_the_canonical_commit(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        task_id = await register(session, "witness", updates=[candidate()], hypotheses={"local": 1})
        judgment = {
            "termination": "continue",
            "reason": "Continue on the current ready task",
            "resume_conditions": [],
            "reviewed_task_ids": [task_id],
            "sequence": 0,
            "witness_inputs": WITNESS,
            "replan_max_passes": 3,
        }
        await state.transaction(lambda tx: tx.finish_hypothesis_judgment("pass-ok", judgment))
        status = await projection(state)
        assert (status.kind, status.replan_count, status.replan_limit) == ("existing_work", 0, 3)
        assert await wakes(state) == []


async def test_replayed_replan_registration_returns_the_receipt_before_any_write(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm) as (session, llm, tools):
        state = session.deps.central_state
        service = AttackGraphService(session.deps.graph_store)
        mission = await state.get_mission(session.runtime.mission_id)
        binding = await state.transaction(lambda tx: tx.binding()[1], write=False)
        proposal = HypothesisProbeProposal.model_validate({"updates": [candidate()], "tasks": []})
        judgment = {
            "termination": "replan",
            "reason": "Revise once",
            "resume_conditions": [],
            "sequence": 0,
            "replan_max_passes": 3,
            "witness_inputs": WITNESS,
        }
        kwargs = {
            "proposal_id": "replay-pass",
            "proposal": proposal,
            "expected_revision": mission.revision,
            "expected_binding_hash": binding,
            "graph_read": {TARGET: content_hash(await service.get_node(TARGET))},
            "known_targets": {TARGET},
            "known_principals": set(),
            "max_tasks": 4,
            "planner_judgment": judgment,
        }
        first = await register_probe_tasks(session.deps, **kwargs)
        second = await register_probe_tasks(session.deps, **kwargs)
        assert second == first
        status = await projection(state)
        assert (status.kind, status.replan_count) == ("replan_queued", 1)
        assert len(await wakes(state)) == 1
        assert await planner_passes(state) == 1
        hypotheses = await service.list_hypotheses()
        assert len(hypotheses) == 1 and hypotheses[0].revision == 1
        assert (await state.get_mission(session.runtime.mission_id)).revision == mission.revision


async def test_replan_cap_joins_the_run_identity_and_changes_are_refused(tmp_path):
    llm = PlannerDouble([{"reason": "Request one bounded pass", "termination": "replan"}])
    async with budgeted(tmp_path, llm=llm, cap=3) as (session, llm, tools):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        resume = (session.runtime.run_id, session.runtime.engagement_id)
    requests = len(llm.requests)
    # The same value resumes the saved Run and keeps its spent count, with no planner call.
    async with budgeted(tmp_path, llm=llm, cap=3, resume=resume, tools=tools) as (session, _, _):
        status = await projection(session.deps.central_state)
        assert (status.kind, status.replan_count, status.replan_limit) == ("replan_queued", 1, 3)
        assert len(llm.requests) == requests
    # A lower cap would undercount and a higher cap would refill the spent Run. The existing
    # component binding hashes the durable settings payload, so bind_run refuses both before
    # any planner work starts (the run_configuration comparison is a second guard).
    for changed in (2, 5):
        with pytest.raises(StateConflict, match="run_binding_changed_new_run_required"):
            async with budgeted(tmp_path, llm=llm, cap=changed, resume=resume, tools=tools):
                pass
    assert len(llm.requests) == requests


async def test_commit_refuses_out_of_range_mismatched_or_malformed_trusted_controls(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm, cap=3) as (session, llm, tools):
        state = session.deps.central_state
        base = {
            "termination": "replan",
            "reason": "Empty follow-up",
            "resume_conditions": [],
            "sequence": 0,
            "replan_max_passes": 3,
            "witness_inputs": WITNESS,
        }
        refused = [
            ({"replan_max_passes": 33}, "planner_replan_limit_invalid"),
            ({"replan_max_passes": True}, "planner_replan_limit_invalid"),
            ({"replan_max_passes": 2}, "planner_replan_limit_mismatch"),
            (
                {
                    "termination": "continue",
                    "witness_inputs": {
                        "eligible_roles": "probe-worker",
                        "require_probe_limits": True,
                    },
                },
                "planner_witness_inputs_invalid",
            ),
        ]
        for index, (changed, code) in enumerate(refused):
            judgment = {**base, **changed}
            with pytest.raises(StateConflict, match=code):
                await state.transaction(
                    lambda tx, key=f"refused-{index}", value=judgment: (
                        tx.finish_hypothesis_judgment(key, value)
                    ),
                )
        assert await projection(state) is None
        assert await planner_passes(state) == 0
        assert await wakes(state) == []


def _replan_judgment(limit):
    return {
        "termination": "replan",
        "reason": "Empty follow-up",
        "resume_conditions": [],
        "sequence": 0,
        "replan_max_passes": limit,
        "witness_inputs": WITNESS,
    }


def _rewrite_run_configuration(tx, mutate):
    row = tx.db.execute(
        "SELECT payload FROM state_requests WHERE run_id=? AND kind='run_configuration' "
        "AND identity=?",
        (tx.run_id, tx.run_id),
    ).fetchone()
    stored = json.loads(row[0])
    mutate(stored)
    tx.db.execute(
        "UPDATE state_requests SET payload=? WHERE run_id=? AND kind='run_configuration' "
        "AND identity=?",
        (json.dumps(stored), tx.run_id, tx.run_id),
    )


def _drop_cap_field(stored):
    del stored["settings"]["planner_replan_max_passes"]


def _settings_not_a_mapping(stored):
    stored["settings"] = "not-a-mapping"


def _malformed_cap_value(stored):
    stored["settings"]["planner_replan_max_passes"] = "many"


def _corrupt_drop_cap(tx):
    _rewrite_run_configuration(tx, _drop_cap_field)


def _corrupt_settings_type(tx):
    _rewrite_run_configuration(tx, _settings_not_a_mapping)


def _corrupt_cap_value(tx):
    _rewrite_run_configuration(tx, _malformed_cap_value)


def _delete_run_configuration(tx):
    tx.db.execute(
        "DELETE FROM state_requests WHERE run_id=? AND kind='run_configuration' AND identity=?",
        (tx.run_id, tx.run_id),
    )


async def _budget_rows(state):
    return await state.transaction(
        lambda tx: tx.db.execute(
            "SELECT COUNT(*) FROM state_requests WHERE run_id=? AND kind=?",
            (tx.run_id, REPLAN_BUDGET_KIND),
        ).fetchone()[0],
        write=False,
    )


async def test_historical_run_without_the_cap_field_normalizes_to_the_owned_default(tmp_path):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm, cap=3) as (session, llm, tools):
        state = session.deps.central_state
        await state.transaction(_corrupt_drop_cap)
        # The stored historical settings normalize to the owned default 3, not to the caller.
        with pytest.raises(StateConflict, match="planner_replan_limit_mismatch"):
            await state.transaction(
                lambda tx: tx.finish_hypothesis_judgment("historical-32", _replan_judgment(32)),
            )
        assert await projection(state) is None
        assert await planner_passes(state) == 0
        assert await wakes(state) == []
        assert await _budget_rows(state) == 0
        await state.transaction(
            lambda tx: tx.finish_hypothesis_judgment("historical-3", _replan_judgment(3)),
        )
        status = await projection(state)
        assert (status.kind, status.replan_count, status.replan_limit) == ("replan_queued", 1, 3)
        assert len(await wakes(state)) == 1
        assert await _budget_rows(state) == 1


@pytest.mark.parametrize(
    ("corrupt", "code"),
    [
        (_delete_run_configuration, "planner_run_configuration_missing"),
        (_corrupt_settings_type, "planner_run_configuration_invalid"),
        (_corrupt_cap_value, "planner_run_configuration_invalid"),
    ],
    ids=["absent", "settings-not-mapping", "cap-not-integer"],
)
async def test_absent_or_malformed_run_configuration_fails_closed(tmp_path, corrupt, code):
    llm = PlannerDouble([])
    async with budgeted(tmp_path, llm=llm, cap=3) as (session, llm, tools):
        state = session.deps.central_state
        await state.transaction(corrupt)
        # Fail closed: the caller's 3 is never accepted as a fallback for a missing bound.
        with pytest.raises(StateConflict, match=code):
            await state.transaction(
                lambda tx: tx.finish_hypothesis_judgment("bad-config", _replan_judgment(3)),
            )
        assert await projection(state) is None
        assert await planner_passes(state) == 0
        assert await wakes(state) == []
        assert await _budget_rows(state) == 0
