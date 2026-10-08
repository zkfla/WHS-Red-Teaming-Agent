"""P1 contracts with fixture decisions/stores; no model or network calls."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)
from redteam.orchestration.coordination import CoordinationError
from redteam.orchestration.dispatch import WorkerOutcome, merge_worker_outcomes
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.nodes.aggregate import aggregate_node
from redteam.orchestration.outbox import consume_pending
from redteam.orchestration.path_events import (
    PATH_ACCEPTED,
    queue_path_accepted,
    reconcile_path_events,
)
from redteam.orchestration.paths import PathPlan, PathStep
from redteam.orchestration.paths.writing import writing_schema
from redteam.orchestration.planning import BudgetedInference, PlanningRequest
from redteam.orchestration.resource_conflicts import execution_conflicts
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planning import (
    AttackPathProposal,
    SerialSpecialistPlanningProvider,
    validate_revision,
)
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.work.models import WorkItem, WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.fakes import build_test_application
from tests.integration import _engine_llm_harness as h
from tests.unit.test_coordination_sqlite import ENG, RUN, records, seed, stores
from tests.unit.test_fanout_approval_accounting import dispatch_item, runtime, state_with
from tests.unit.test_specialist_profile import (
    PROFILE,
    SpecialistLLM,
    _application,
    _atomized,
    _strategy,
)

P1_PROFILE = "config/agents/specialists-p1.yaml"


def previous_path():
    strategy = _strategy()
    second = deepcopy(strategy["questions"][0])
    second.update(question_id="q2", objective="Independent observation")
    strategy["questions"].append(second)
    atomic = _atomized()
    second_step = deepcopy(atomic["path"]["steps"][0])
    second_step.update(step_id="s2", objective="Independent observation")
    atomic["path"]["steps"].append(second_step)
    atomic["origins"]["s2"] = "q2"
    path = PathPlan.model_validate(
        {
            **atomic["path"],
            "path_id": "path",
            "run_id": "run",
            "goal": "Observe",
            "version": 1,
            "status": "active",
            "planning_artifacts": {
                "specialist_decomposition": {
                    "version": "1",
                    "strategy": deepcopy(strategy),
                    "origins": deepcopy(atomic["origins"]),
                },
            },
        }
    )
    path.steps[1].status = "completed"
    path.steps[1].evidence_ids = ["original-independent"]
    return path, strategy, atomic


def test_p0_preserved_and_p1_explicitly_enabled():
    assert load_specialist_profile(PROFILE).profile.workflow == "serial"
    p1 = load_specialist_profile(P1_PROFILE)
    assert p1.profile.workflow == "durable"
    role = next(r for r in p1.profile.roles if r.definition.schedulable)
    assert role.definition.max_concurrency == 2
    assert p1.fingerprint != load_specialist_profile(PROFILE).fingerprint
    settings = OrchestrationSettings(global_concurrency=1)
    assert p1.configure_execution(settings).max_actions_per_turn == 1
    assert load_specialist_profile(PROFILE).configure_execution(settings) == settings


async def test_engine_revision_keeps_completed_independent_progress(tmp_path):
    from redteam.attack_graph.observation import GraphObservation

    llm = SpecialistLLM()
    world, engagement, deps, gateway = await _application(tmp_path, llm)
    deps = replace(
        deps,
        planning_provider=SerialSpecialistPlanningProvider(
            load_specialist_profile(P1_PROFILE).prompts,
            enable_replanning=True,
        ),
    )
    old, strategy, atomized = previous_path()
    runtime_state = h.make_runtime("run-specialist", str(engagement.engagement_id))
    old.run_id = str(runtime_state.run_id)
    for steps in (old.steps, atomized["path"]["steps"]):
        for step in steps:
            if isinstance(step, dict):
                step["target_node_ids"] = [world.node_id]
            else:
                step.target_node_ids = [world.node_id]
    for questions in (
        strategy["questions"],
        old.planning_artifacts["specialist_decomposition"]["strategy"]["questions"],
    ):
        for question in questions:
            question["target_node_ids"] = [world.node_id]
    await deps.path_store.register(old)
    strategy["questions"][0]["objective"] = "Changed observation question"
    atomized["path"]["steps"][0]["objective"] = "Changed observation question"
    llm.add("next_action", {"mode": "revise_path", "reason": "One condition changed"})
    llm.add("attack_path_proposal", strategy)
    llm.add("task_atomization", atomized)
    llm.add("path_review", h.review_accept())
    llm.add("next_action", {"mode": "hold", "reason": "Fixture stops after revision"})
    await InvestigationEngine(deps).decide(runtime_state, GraphObservation())
    active = await deps.path_store.get_active(str(runtime_state.run_id))
    assert active.path_id == old.path_id and active.version == 2
    assert active.changed_step_ids == ["s1"]
    assert active.steps[0].status == "pending"
    assert active.steps[1].status == "completed"
    assert active.steps[1].evidence_ids == ["original-independent"]
    assert gateway.invocations == []
    assert [c["route"] for c in llm.calls] == [
        "next_action",
        "attack_path_proposal",
        "task_atomization",
        "path_review",
        "next_action",
    ]


async def test_durable_path_events_do_not_arrive_after_terminal_report(tmp_path, monkeypatch):
    from redteam.app.bootstrap import run_engagement
    from redteam.core.contracts.events import EventType
    from redteam.orchestration.events import REPORT_PROJECTED
    from redteam.orchestration.planning import StructuredLLMPlanningProvider
    from tests.integration.test_end_to_end import _world

    engagement, deps, _ = _world()
    original = deps.planning_provider or StructuredLLMPlanningProvider()

    class ExplicitFixtureProcedure:
        enable_replanning = True

        async def propose(self, request):
            return await original.propose(request)

    deps = replace(deps, planning_provider=ExplicitFixtureProcedure())
    monkeypatch.setattr("redteam.app.bootstrap.save_terminal_report", lambda report: None)
    events = []

    async def collect(event):
        events.append(event)

    await deps.event_bus.subscribe("*", collect)
    await run_engagement(engagement, deps=deps)
    report = next(e for e in events if e.event_type == REPORT_PROJECTED)
    finished = next(e for e in events if e.event_type == EventType.RUN_FINISHED)
    path_events = [e for e in events if e.event_type in {PATH_ACCEPTED, "PathStepSettled"}]
    assert {e.event_type for e in path_events} == {PATH_ACCEPTED, "PathStepSettled"}
    assert all(events.index(e) < events.index(report) for e in path_events)
    assert report.causation_id == events[events.index(report) - 1].event_id
    assert finished.causation_id == report.event_id


@pytest.mark.parametrize("case", ["no_change", "unrelated", "unknown_provenance", "question_only"])
def test_revision_rejects_resetting_unaffected_work(case):
    old, strategy, atomized = previous_path()
    if case != "no_change":
        strategy["questions"][0]["objective"] = "Observe changed conditions"
        atomized["path"]["steps"][0]["objective"] = "Observe changed conditions"
    if case == "unrelated":
        atomized["path"]["steps"][1]["objective"] = "Secretly reset independent work"
    elif case == "unknown_provenance":
        old.planning_artifacts = {}
    elif case == "question_only":
        atomized["path"]["steps"][0]["objective"] = old.steps[0].objective
    with pytest.raises(ValueError, match="specialist_revision"):
        validate_revision(
            atomized["path"],
            AttackPathProposal.model_validate(strategy),
            atomized["origins"],
            old.model_dump(mode="json"),
        )


async def test_p1_revision_uses_planner_atomizer_and_shared_budget():
    old, strategy, atomized = previous_path()
    strategy["questions"][0]["objective"] = "Observe changed conditions"
    atomized["path"]["steps"][0]["objective"] = "Observe changed conditions"
    llm = SpecialistLLM()
    llm.add("next_action", {"mode": "revise_path", "reason": "A relevant condition changed"})
    llm.add("attack_path_proposal", strategy)
    llm.add("task_atomization", atomized)
    budget = DecisionBudget.reasoning_chain(8, 3)
    provider = SerialSpecialistPlanningProvider(
        load_specialist_profile(P1_PROFILE).prompts,
        enable_replanning=True,
    )
    result = await provider.propose(
        PlanningRequest(
            stage="next_action",
            payload={
                "active_path": old.model_dump(mode="json"),
                "knowledge": {"known_targets": ["host:test"]},
            },
            response_schema=writing_schema(TurnDecision, selected_tool=True),
            inference=BudgetedInference(StructuredDecisionClient(llm), budget),
        )
    )
    assert result["mode"] == "revise_path" and result["path"]
    assert budget.used == 3
    assert [c["route"] for c in llm.calls] == [
        "next_action",
        "attack_path_proposal",
        "task_atomization",
    ]
    assert result["path"]["steps"][1]["step_id"] == "s2"


async def test_p0_rejects_replanning_without_enabling_it():
    llm = SpecialistLLM()
    for _ in range(3):
        llm.add("next_action", {"mode": "revise_path", "reason": "not enabled"})
    provider = SerialSpecialistPlanningProvider(load_specialist_profile(PROFILE).prompts)
    with pytest.raises(DecisionFailure) as failure:
        await provider.propose(
            PlanningRequest(
                stage="next_action",
                payload={},
                response_schema=writing_schema(TurnDecision, selected_tool=True),
                inference=BudgetedInference(StructuredDecisionClient(llm), DecisionBudget(3)),
            )
        )
    assert len(llm.calls) == 3
    assert failure.value.diagnostic.last_cause.kind == "format"


async def test_path_and_outbox_commit_survive_crash_before_work_invalidation(tmp_path):
    db, audit = tmp_path / "state.db", tmp_path / "audit.db"
    _, paths, _, service, publisher = await seed(db, audit)
    work = SQLiteWorkStore(tmp_path / "work.db")
    old = PathPlan(
        path_id="p",
        run_id=str(RUN),
        version=1,
        status="active",
        goal="Observe",
        reason="first",
        steps=[
            PathStep(step_id="changed", objective="first"),
            PathStep(step_id="independent", objective="keep"),
        ],
    )
    await paths.register(old)
    now = h.utcnow()
    for identity in ("changed", "independent"):
        await work.add(
            WorkItem(
                work_id=identity,
                run_id=RUN,
                candidate_key=identity,
                agent_id="fixture",
                path_id=old.path_id,
                path_version=1,
                path_step_id=identity,
                target_node_ids=["host:test"],
                priority=PriorityScore(total=1),
                created_at=now,
                updated_at=now,
            )
        )

    async def commit(change):
        new = old.model_copy(deep=True, update={"version": 2, "parent_version": 1})
        new.steps[0].objective = "changed condition"
        saved = await change.paths.register(new, expected_version=1)
        queue_path_accepted(change, publisher, saved, old)

    await service._coordinator.change(commit)
    assert (await work.get("changed")).status is WorkStatus.PENDING
    _, _, _, recovered, _ = stores(db, audit)
    await reconcile_path_events(
        recovered._coordinator, work, run_id=str(RUN), engagement_id=str(ENG)
    )
    assert (await work.get("changed")).status is WorkStatus.STALE
    assert (await work.get("independent")).status is WorkStatus.PENDING
    before = await records(recovered._coordinator)
    await reconcile_path_events(
        recovered._coordinator, work, run_id=str(RUN), engagement_id=str(ENG)
    )
    assert await records(recovered._coordinator) == before


async def test_consumer_dedup_and_failed_delivery_recover_without_resetting_cursor(tmp_path):
    _, _, _, service, publisher = await seed(tmp_path / "state.db", tmp_path / "audit.db")
    coordinator = service._coordinator

    async def enqueue(change):
        for _ in range(2):
            change.queue(publisher.build(PATH_ACCEPTED, {"dedup_key": "same-object-version"}))

    await coordinator.change(enqueue)
    calls = []

    async def failing(event):
        calls.append(str(event.event_id))
        raise RuntimeError("crash after effect")

    params = dict(
        run_id=str(RUN),
        engagement_id=str(ENG),
        consumer_id="fixture:v1",
        event_types=frozenset({PATH_ACCEPTED}),
    )
    with pytest.raises(RuntimeError):
        await consume_pending(coordinator, handler=failing, **params)

    async def recovered(event):
        calls.append(str(event.event_id))

    await consume_pending(coordinator, handler=recovered, **params)
    assert len(calls) == 2 and calls[0] == calls[1]
    await consume_pending(coordinator, handler=recovered, **params)
    assert len(calls) == 2


async def test_consumer_claim_is_exclusive_across_two_coordinators(tmp_path):
    db, audit = tmp_path / "state.db", tmp_path / "audit.db"
    _, _, _, first, publisher = await seed(db, audit)
    _, _, _, second, _ = stores(db, audit)

    async def enqueue(change):
        change.queue(publisher.build(PATH_ACCEPTED, {}))

    await first._coordinator.change(enqueue)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event):
        entered.set()
        await release.wait()

    params = dict(
        run_id=str(RUN),
        engagement_id=str(ENG),
        consumer_id="fixture:v1",
        event_types=frozenset({PATH_ACCEPTED}),
        handler=handler,
    )
    task = asyncio.create_task(consume_pending(first._coordinator, **params))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(CoordinationError, match="consumer_busy"):
            await consume_pending(second._coordinator, **params)
    finally:
        release.set()
        await task


async def test_cancelled_consumer_releases_lease_for_recovery(tmp_path):
    _, _, _, service, publisher = await seed(tmp_path / "state.db", tmp_path / "audit.db")

    async def enqueue(change):
        change.queue(publisher.build(PATH_ACCEPTED, {"dedup_key": "version-1"}))

    await service._coordinator.change(enqueue)
    entered = asyncio.Event()

    async def interrupted(event):
        entered.set()
        await asyncio.Event().wait()

    params = dict(
        run_id=str(RUN),
        engagement_id=str(ENG),
        consumer_id="cancel:v1",
        event_types=frozenset({PATH_ACCEPTED}),
    )
    task = asyncio.create_task(
        consume_pending(
            service._coordinator,
            handler=interrupted,
            **params,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recovered = []

    async def finish(event):
        recovered.append(str(event.event_id))

    await consume_pending(service._coordinator, handler=finish, **params)
    assert len(recovered) == 1


async def test_consumer_conflicting_dedup_and_wrong_binding_fail_closed(tmp_path):
    _, _, _, service, publisher = await seed(tmp_path / "state.db", tmp_path / "audit.db")

    async def enqueue(change):
        for value in ("first", "conflicting"):
            change.queue(publisher.build(PATH_ACCEPTED, {"dedup_key": "version-1", "value": value}))

    await service._coordinator.change(enqueue)
    applied = []

    async def effect(event):
        applied.append(event.payload["value"])

    params = dict(
        run_id=str(RUN),
        consumer_id="conflict:v1",
        event_types=frozenset({PATH_ACCEPTED}),
        handler=effect,
    )
    with pytest.raises(CoordinationError, match="binding_mismatched"):
        await consume_pending(service._coordinator, engagement_id="other-engagement", **params)
    assert not applied
    with pytest.raises(CoordinationError, match="dedup_conflict"):
        await consume_pending(service._coordinator, engagement_id=str(ENG), **params)
    assert applied == ["first"]


def test_result_merge_is_idempotent_commutative_and_keeps_conflicts():
    a = WorkerOutcome(
        action_id="a", work_id="w", executed=True, succeeded=True, evidence_ids=["original-a"]
    )
    b = a.model_copy(update={"succeeded": False, "evidence_ids": ["original-b"]})
    assert merge_worker_outcomes({"a": a}, {"a": a}) == {"a": a}
    merged = merge_worker_outcomes({"a": a}, {"a": b})
    assert merged == merge_worker_outcomes({"a": b}, {"a": a})
    assert merged == merge_worker_outcomes(merged, {"a": a})
    assert merged["a"].error == "worker_outcome_conflict"
    assert len(merged["a"].conflicting_results) == 2
    c = a.model_copy(update={"work_id": "different-work"})
    assert merge_worker_outcomes(merged, {"a": c}) == merge_worker_outcomes(
        {"a": a},
        merge_worker_outcomes({"a": b}, {"a": c}),
    )
    with pytest.raises(ValueError, match="identity_mismatch"):
        merge_worker_outcomes({"wrong": a}, {})


async def test_aggregate_replayed_result_does_not_consume_budget_twice():
    item = dispatch_item("a", "test")
    outcome = WorkerOutcome(
        action_id="a",
        work_id=item.work_id,
        executed=True,
        succeeded=True,
        policy_verdict=PolicyVerdict.ALLOW,
    )
    deps = build_test_application()
    state = state_with(
        runtime(remaining_budget=5), dispatch_items=[item], worker_outcomes={"a": outcome}
    )
    first = await aggregate_node(state, deps=deps)
    restored = RuntimeState.model_validate_json(first["runtime"].model_dump_json())
    second = await aggregate_node({**state, "runtime": restored}, deps=deps)
    assert second["runtime"].actions_executed == 1
    assert second["runtime"].remaining_budget == 4
    changed = outcome.model_copy(update={"succeeded": False})
    conflict = await aggregate_node(
        {**state, "runtime": restored, "worker_outcomes": {"a": changed}}, deps=deps
    )
    assert conflict["runtime"].actions_executed == 1
    assert conflict["runtime"].termination_reason == TerminationReason.EXECUTION_ERROR
    assert len(conflict["runtime"].worker_result_conflicts["a"]) == 2


def test_shared_backend_authority_and_resource_references_conflict():
    tool = h.inventory_descriptor()
    target = HostTarget(host="first")
    a = set(execution_conflicts(target, {"first_session_ref": "same"}, tool))
    b = set(
        execution_conflicts(
            HostTarget(host="other"),
            {"second_session_ref": "same"},
            tool.model_copy(update={"server_id": "another"}),
        )
    )
    assert a & b  # Same session across otherwise independent targets/backends.
    c = set(execution_conflicts(HostTarget(host="other"), {}, tool))
    assert a & c  # Unknown shared backend is conservative/exclusive.


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_work_claim_prevents_duplicate_owner_and_dependency_execution(tmp_path, backend):
    from redteam.orchestration.work.memory import InMemoryWorkStore

    work = InMemoryWorkStore() if backend == "memory" else SQLiteWorkStore(tmp_path / "work.db")
    now = h.utcnow()
    base = WorkItem(
        work_id="first",
        run_id="run",
        candidate_key="first",
        agent_id="fixture",
        target_node_ids=["host:test"],
        priority=PriorityScore(total=1),
        created_at=now,
        updated_at=now,
    )
    await work.add(base)
    await work.add(
        base.model_copy(
            update={"work_id": "child", "candidate_key": "child", "prerequisite_ids": ["first"]}
        )
    )
    assert await work.acquire("child", owner="role", now=now, ttl=timedelta(seconds=60)) is None
    results = await asyncio.gather(
        *[
            work.acquire("first", owner=owner, now=now, ttl=timedelta(seconds=60))
            for owner in ("worker-a", "worker-b")
        ]
    )
    assert sum(result is not None for result in results) == 1
