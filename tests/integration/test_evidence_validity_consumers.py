"""Native completion must not ignore append-only reuse conditions."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import AgentPhase, EvidenceSourceType, GraphNodeType, TaskStatus
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.evidence_validity import (
    assess,
    capture_validity,
    current_validity,
    refresh_validity,
)
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_evidence import PlanningEvidenceStore
from redteam.orchestration.planning_graph import PlanningGraphStore
from tests.integration.test_completion_review_atomicity import setup


@pytest.mark.parametrize("mode", ["step", "task", "goal"])
async def test_invalid_reuse_binding_cannot_support_completion(mode):
    world, runtime, path, reviewer = await setup(mode)
    await world.evidence.append(EvidenceRecord(
        evidence_id="invalid-binding", run_id=runtime.run_id,
        engagement_id=runtime.engagement_id, source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.evidence_validity", content_type="application/json",
        created_at=datetime.now(timezone.utc), digest="tampered",
        metadata={"record_kind": "evidence_validity_binding_v1",
                  "claim_evidence_id": "proof-race", "schema_version": "1"},
    ))
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    decision = TurnDecision(
        mode=f"complete_{mode}", path_step_id="step-1", reason="Original observation",
        completion={"outcome": "success", "reason": "Original observation",
                    "evidence_ids": ["proof-race"]},
    )
    if mode == "step":
        _, errors = await engine.complete_step(path, decision, runtime, context)
    elif mode == "task":
        errors = await engine.complete_task(path, decision, runtime, context)
    else:
        errors = await engine.complete_goal(path, decision, runtime, context)
    assert errors, "a malformed reuse binding must not be silently ignored"
    assert reviewer.calls == 0
    if mode == "task":
        assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


async def measured_world(mode="task"):
    world, runtime, path, reviewer = await setup(mode)
    coordinator = coordinator_for(world.tasks, world.paths)
    admission = PlanningAdmission(coordinator)
    world.evidence = PlanningEvidenceStore(world.evidence, admission)
    world.graph_store = PlanningGraphStore(world.graph_store, admission)
    world.deps.evidence_store = world.evidence
    world.deps.graph_store = world.graph_store
    world.deps.coordinator = coordinator
    await measured(world, runtime, path.steps[0].target_node_ids[0], "app_version", "v1")
    return world, runtime, path, reviewer


async def measured(world, runtime, node_id, key, value):
    with llm_request_scope(str(runtime.run_id), "synthetic-graph-update"):
        await AttackGraphService(world.graph_store).upsert_node(
            GraphNodeType.HOST, node_id=node_id, facts={key: value},
            provenance=Provenance(run_id=str(runtime.run_id), evidence_id="proof-race",
                                  confidence=Confidence.MEASURED, source="synthetic-observation"),
        )


async def bind(world, runtime, node, key="app_version", test="unchanged"):
    return await assess({"evidence_id": "proof-race", "conditions": [{
        "node_id": node, "container": "fact", "key": key, "test": test,
    }]}, LocalToolContext(world.deps, runtime))


async def test_native_binding_read_context_and_completion_review_share_current_status():
    world, runtime, path, reviewer = await measured_world()
    node = path.steps[0].target_node_ids[0]
    result = await bind(world, runtime, node)
    assert result.ok and result.payload["status"] == "valid"
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    assert context["evidence_validity"][0]["status"] == "valid"
    review = await engine.review_context(runtime, context, ["proof-race"])
    assert review["evidence_validity"][0]["status"] == "valid"
    assert (await engine.review_context(runtime, {}, []))["evidence_validity"] == []
    before = (await world.evidence.get("proof-race")).model_dump()
    await measured(world, runtime, node, "app_version", "v2")
    current = await current_validity(world.evidence, AttackGraphService(world.graph_store),
                                     str(runtime.run_id), "proof-race")
    assert current.status == "stale"
    assert (await world.evidence.get("proof-race")).model_dump() == before
    assert await engine.complete_task(path, TurnDecision(
        mode="complete_task", reason="Test", completion={"outcome": "success",
        "reason": "Test", "evidence_ids": ["proof-race"]},
    ), runtime, context)
    assert reviewer.calls == 0


async def test_later_binding_cannot_erase_earlier_condition_and_replay_is_deduplicated():
    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    count = len(await world.evidence.list_for_run(runtime.run_id))
    assert (await bind(world, runtime, node)).ok
    assert len(await world.evidence.list_for_run(runtime.run_id)) == count
    await measured(world, runtime, node, "app_version", "v2")
    result = await bind(world, runtime, node)
    assert result.ok and result.payload["status"] == "stale"
    assert len(result.payload["binding_ids"]) == 2


@pytest.mark.parametrize("change", ["related", "unrelated", "new-binding", "expired"])
async def test_relevant_completion_fence_and_grounded_deadline(change):
    from datetime import timedelta

    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    world.deps.specialist_policy = SimpleNamespace()
    read = await capture_validity(world.deps, runtime, ["proof-race"])
    if change == "related":
        await measured(world, runtime, node, "app_version", "v2")
    elif change == "unrelated":
        await measured(world, runtime, "host:unrelated", "app_version", "v2")
    elif change == "new-binding":
        await measured(world, runtime, node, "session_state", "live")
        await bind(world, runtime, node, "session_state")
    else:
        read = replace(read, valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))

    async def commit(tx):
        read.check(tx)

    if change == "unrelated":
        await world.deps.coordinator.change(commit)
    else:
        with pytest.raises(StateConflict):
            await world.deps.coordinator.change(commit)


async def test_status_event_replay_does_not_generate_new_work_or_model_calls():
    from redteam.core.contracts.events import EventType

    world, runtime, path, reviewer = await measured_world()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    async def rows(tx):
        return [row for (kind, _), row in tx.records.items() if kind == "outbox"
                and row["envelope"]["event_type"] == EventType.EVIDENCE_VALIDITY_CHANGED]
    assert len(await world.deps.coordinator.change(rows)) == 1
    await refresh_validity(world.deps, runtime)
    assert len(await world.deps.coordinator.change(rows)) == 1
    await measured(world, runtime, node, "app_version", "v2")
    await refresh_validity(world.deps, runtime)
    assert len(await world.deps.coordinator.change(rows)) == 2
    assert reviewer.calls == 0
    assert len(await world.tasks.list_tasks(mission_id=runtime.mission_id)) == 1


@pytest.mark.parametrize("bad", ["unmeasured", "timezone", "missing", "wrong-run", "failed"])
async def test_reuse_conditions_do_not_guess_or_promote_bad_sources(bad):
    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    if bad == "timezone":
        await measured(world, runtime, node, "expiry", "2030-01-01T00:00:00")
        result = await bind(world, runtime, node, "expiry", "not_expired")
    elif bad == "unmeasured":
        await AttackGraphService(world.graph_store).upsert_node(
            GraphNodeType.HOST, node_id=node, facts={"declared": "v1"},
            provenance=Provenance(confidence=Confidence.SERVER_DECLARED),
        )
        result = await bind(world, runtime, node, "declared")
    else:
        record = await world.evidence.get("proof-race")
        if bad == "missing":
            identity = "missing"
        else:
            identity = "proof-race"
            if bad == "wrong-run":
                record.run_id = "different-run"
            else:
                record.metadata["tool_result"]["success"] = False
        result = await assess({"evidence_id": identity}, LocalToolContext(world.deps, runtime))
    assert not result.ok
    assert result.runtime_outcome is None
    assert not [r for r in await world.evidence.list_for_run(runtime.run_id)
                if r.source_id == "orchestration.evidence_validity"]


async def test_explicit_expiry_projects_stale_without_mutating_original_or_using_a_timer():
    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    await measured(world, runtime, node, "expiry", "2020-01-01T00:00:00+00:00")
    result = await bind(world, runtime, node, "expiry", "not_expired")
    assert result.ok and result.payload["status"] == "stale"
    assert result.payload["valid_until"] == "2020-01-01T00:00:00Z"


@pytest.mark.parametrize("change", ["aba", "unrelated", "new-binding"])
async def test_actual_task_commit_rechecks_related_versions_after_model_review(change):
    world, runtime, path, reviewer = await measured_world()
    world.deps.specialist_policy = SimpleNamespace()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    await measured(world, runtime, "host:other", "app_version", "v1")

    async def mutate():
        if change == "aba":
            await measured(world, runtime, node, "app_version", "v2")
            await measured(world, runtime, node, "app_version", "v1")
        elif change == "new-binding":
            assert (await bind(world, runtime, "host:other")).ok
        else:
            await measured(world, runtime, "host:other", "app_version", "v2")

    reviewer.hook = mutate
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    decision = TurnDecision(mode="complete_task", reason="Test", completion={
        "outcome": "success", "reason": "Test", "evidence_ids": ["proof-race"],
    })
    if change == "unrelated":
        assert await engine.complete_task(path, decision, runtime, context) == []
        assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.COMPLETED
    else:
        with pytest.raises(StateConflict):
            await engine.complete_task(path, decision, runtime, context)
        assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


async def test_report_distinguishes_current_staleness_from_historical_observation():
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.orchestration.reporting import ReportingAgent

    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    await measured(world, runtime, node, "app_version", "v2")
    runtime.phase = AgentPhase.FINISHED
    runtime.termination_reason = "investigation_held"
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(world.graph_store), evidence_store=world.evidence,
        work_store=world.deps.work_store, task_store=world.tasks,
    )
    assert report.evidence_validity[0]["status"] == "stale"
    assert "proof-race: stale" in _render_markdown(report)
    assert "proof-race" in report.evidence_ids


@pytest.mark.parametrize("change", ["removed", "lookup-failed", "fixture", "wrong-engagement"])
async def test_lost_grounding_is_not_a_guess_about_historical_truth(change):
    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    if change == "removed":
        with llm_request_scope(str(runtime.run_id), "synthetic-graph-update"):
            await world.graph_store.remove_node(node)
    elif change == "lookup-failed":
        class UnavailableGraph:
            async def list_nodes(self):
                raise OSError("synthetic read failure")
        world.graph_store = UnavailableGraph()
    else:
        # A deliberately corrupted test store; production wrapper refuses
        # replacement by ID. Readers still cannot upgrade the bad original.
        original = await world.evidence.get("proof-race")
        if change == "fixture":
            original.metadata["fixture_only"] = True
        else:
            original.engagement_id = "other-engagement"
    current = await current_validity(world.evidence, AttackGraphService(world.graph_store),
                                     str(runtime.run_id), "proof-race")
    assert current.status == ("inconclusive" if change in {"removed", "lookup-failed"}
                              else "invalid")


async def test_public_specialist_assembly_registers_assessment_schema(tmp_path):
    from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled

    _, deps, _ = await assembled(tmp_path, ClosedLoopProvider())
    descriptor = next(d for d in deps.local_tools.descriptors() if d.tool_id == "evidence.assess")
    assert descriptor.input_schema["required"] == ["evidence_id"]
    assert "conditions" in descriptor.input_schema["properties"]
    assert deps.local_tools.get("evidence.assess") is not None
    assert isinstance(deps.evidence_store, PlanningEvidenceStore)


@pytest.mark.parametrize("change", ["new-binding", "expired-before-context"])
async def test_planner_deadline_comes_from_captured_model_input_not_earlier_refresh(
    monkeypatch, change,
):
    import json
    from datetime import timedelta

    from redteam.core.contracts.specialists import SpecialistRunPolicy
    from redteam.orchestration import evidence_validity, planning_admission
    from redteam.orchestration.specialist_planner import SpecialistPlanner

    world, runtime, path, _ = await measured_world()
    node = path.steps[0].target_node_ids[0]
    world.deps.specialist_policy = SpecialistRunPolicy(
        fingerprint="0" * 64, max_instances=2, max_activations=8,
        max_strategy_revisions=4, max_tasks_per_strategy=8,
    )
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    expiry = start + timedelta(seconds=1)

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(evidence_validity, "datetime", Clock)
    monkeypatch.setattr(planning_admission, "datetime", Clock)
    original_bind = PlanningAdmission.bind_inputs
    if change == "expired-before-context":
        await measured(world, runtime, node, "expiry", expiry.isoformat())
        assert (await bind(world, runtime, node, "expiry", "not_expired")).ok

    async def new_condition_before_capture(self, run_id, inputs):
        await original_bind(self, run_id, inputs)
        if change == "expired-before-context":
            Clock.current = expiry + timedelta(seconds=1)
            return
        # No binding at refresh; a new grounded expiry exists before the
        # captured read set and the model's actual context are produced.
        await measured(world, runtime, node, "expiry", expiry.isoformat())
        assert (await bind(world, runtime, node, "expiry", "not_expired")).ok

    monkeypatch.setattr(PlanningAdmission, "bind_inputs", new_condition_before_capture)

    class PlannerProvider:
        calls = 0

        async def complete_structured(self, **kwargs):
            self.calls += 1
            payload, _ = json.JSONDecoder().raw_decode(kwargs["messages"][0]["content"])
            if "questions" in kwargs["response_schema"]["properties"]:
                expected = "valid" if change == "new-binding" else "stale"
                assert payload["evidence_validity"][0]["status"] == expected
                Clock.current = expiry + timedelta(seconds=1)
                return {"questions": [{"question_id": "expiry-check",
                    "objective": "Investigate the observed host", "target_node_ids": [node],
                    "completion_criteria": [{"kind": "semantic",
                                             "description": "Preserve original observations"}],
                }], "reason": "Synthetic grounded followup"}
            question = payload["questions"][0]
            return {"tasks": [{"task_key": "atomic", "question_id": question["question_id"],
                "title": "Inspect host", "objective": question["objective"],
                "role_id": "web-api-agent", "target_node_ids": [node],
                "completion_criteria": question["completion_criteria"],
            }]}

    provider = PlannerProvider()
    world.deps.llm_client = provider
    if change == "new-binding":
        with pytest.raises(StateConflict, match="stale_plan_evidence_expired"):
            await SpecialistPlanner(world.deps).advance(runtime)
    else:
        # Current stale evidence may inform a bounded revalidation proposal;
        # no target execution or success follows from this task registration.
        assert len(await SpecialistPlanner(world.deps).advance(runtime)) == 1
    assert provider.calls == 2
    expected_tasks = 1 if change == "new-binding" else 2
    assert len(await world.tasks.list_tasks(mission_id=runtime.mission_id)) == expected_tasks


async def test_current_release_does_not_rewrite_old_goal_or_block_on_unused_sibling():
    from redteam.orchestration.reporting import ReportingAgent

    world, runtime, path, _ = await measured_world("goal")
    node = path.steps[0].target_node_ids[0]
    assert (await bind(world, runtime, node)).ok
    engine = InvestigationEngine(world.deps)
    assert await engine.complete_goal(path, TurnDecision(
        mode="complete_goal", reason="Test", completion={"outcome": "success",
        "reason": "Test", "evidence_ids": ["proof-race"]},
    ), runtime, await engine.context(runtime, GraphObservation())) == []
    runtime.phase = AgentPhase.FINISHED
    runtime.termination_reason = "goal_achieved"

    async def report():
        return await ReportingAgent().project(
            runtime, graph=AttackGraphService(world.graph_store), evidence_store=world.evidence,
            work_store=world.deps.work_store, task_store=world.tasks,
        )
    assert (await report()).ready_for_release
    # Unused malformed condition exists alongside the goal's valid original.
    record = (await world.evidence.list_for_run(runtime.run_id))[-1].model_copy(deep=True)
    record.evidence_id = "unused-binding"
    record.source_id = "orchestration.evidence_validity"
    record.metadata = {"record_kind": "evidence_validity_binding_v1",
                       "claim_evidence_id": "unused-origin"}
    await world.evidence.append(record)
    assert (await report()).ready_for_release
    await measured(world, runtime, node, "app_version", "v2")
    current = await report()
    assert not current.ready_for_release
    assert current.evidence_validity_blockers == ["proof-race"]
    assert current.mission.status == "completed"
    assert current.mission.success_criteria[0].met
