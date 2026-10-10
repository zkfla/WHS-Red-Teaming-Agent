"""Crash-before-checkpoint recovery; synthetic providers/targets only."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli.commands.resume import _drive_cohort_guarded
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import TaskId
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.task_activation import TaskActivation
from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled
from tests.integration.test_specialist_resume import OneApprovalControl, identities
from tests.unit.test_resource_conflicts import resource_work


class NoFollowupProvider(ClosedLoopProvider):
    async def complete_structured(self, **kwargs):
        result = await super().complete_structured(**kwargs)
        if "questions" in kwargs["response_schema"].get("properties", {}) and self.strategies > 1:
            return {"questions": [], "reason": "No independent followup"}
        return result


async def reserved_peer(deps, pending):
    (task,) = await deps.task_graph_store.list_tasks(mission_id=pending.mission_id)
    await deps.task_graph_store.create_task(task.model_copy(update={
        "task_id": TaskId("task-reserved-peer"), "status": TaskStatus.READY,
        "occupant_owner": None, "completion_evidence_id": None,
    }))
    activation = TaskActivation(
        coordinator_for(deps.task_graph_store, deps.path_store), deps.agents,
    )
    policy = deps.specialist_policy
    (peer,) = await activation.ready(
        pending, max_instances=policy.max_instances, max_activations=policy.max_activations,
    )
    return activation, peer


async def drive_resume(deps, pending, *, partial=False):
    cohort = await identities(deps, pending.run_id)
    finals = []
    args = SimpleNamespace(run_id=str(pending.run_id),
                           instance=str(pending.instance_id) if partial else None)
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        code = await _drive_cohort_guarded(
            driver, args, deps, cohort, cohort, on_finished=finals.append,
        )
    return code, finals


async def test_missing_checkpoint_for_entered_peer_refuses_whole_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)

    async def entered(tx):
        tx.records[("task_activation", f"{peer.run_id}:{peer.active_task_id}")]["launch_stage"] = (
            "entered"
        )

    await activation.coordinator.change(entered)
    calls = len(provider.requests)
    control.approve(pending)
    code, finals = await drive_resume(deps, pending)
    assert code == 1 and not finals
    assert not gateway.invocations and len(provider.requests) == calls
    assert not control.restored  # no guessed full-cohort budget reset
    task = await deps.task_graph_store.get_task(peer.active_task_id)
    assert task.occupant_owner == peer.work_owner


@pytest.mark.parametrize("partial", [False, True])
async def test_reserved_peer_reuses_original_identity_only_on_full_resume(
    tmp_path, monkeypatch, partial,
):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)
    ledger = RunRequestLedger(activation.coordinator)
    before = await ledger.snapshot(str(peer.run_id))
    control.approve(pending)
    code, finals = await drive_resume(deps, pending, partial=partial)
    assert code == 0
    assert {final.instance_id for final in finals} == (
        {pending.instance_id} if partial else {pending.instance_id, peer.instance_id}
    )
    assert len(gateway.invocations) == (1 if partial else 2)
    assert control.restored[0]["actions_consumed"] == 0
    after = await ledger.snapshot(str(peer.run_id))
    assert after["spent"] > before["spent"] and after["spent"] == len(provider.requests)
    assert len(await deps.task_graph_store.list_tasks(mission_id=peer.mission_id)) == 2
    if partial:
        task = await deps.task_graph_store.get_task(peer.active_task_id)
        assert task.status == TaskStatus.RUNNING and task.occupant_owner == peer.work_owner
        assert provider.strategies == 1


@pytest.mark.parametrize("contradiction", [
    "work", "evidence", "checkpoint", "unavailable", "work_unavailable", "evidence_unavailable",
    "race",
])
async def test_reserved_proof_contradictions_refuse_before_model_tool_or_accounting(
    tmp_path, monkeypatch, contradiction,
):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)
    if contradiction == "work":
        item = resource_work("contradicting-work", run=str(peer.run_id))
        await deps.work_store.add(item.model_copy(update={"instance_id": peer.instance_id}))
    elif contradiction == "evidence":
        await deps.evidence_store.append(EvidenceRecord(
            evidence_id="contradicting-evidence", run_id=peer.run_id,
            engagement_id=peer.engagement_id, source_type=EvidenceSourceType.SYSTEM,
            source_id="synthetic-actor", content_type="application/json", digest="synthetic",
            created_at=datetime.now(timezone.utc), metadata={"instance_id": str(peer.instance_id)},
        ))
    elif contradiction in {"checkpoint", "unavailable"}:
        from redteam.orchestration.runner import AgentLoopRunner

        method = AsyncMock(return_value=True) if contradiction == "checkpoint" else AsyncMock(
            side_effect=RuntimeError("synthetic unavailable checkpoint store"),
        )
        monkeypatch.setattr(AgentLoopRunner, "checkpoint_exists", method)
    elif contradiction in {"work_unavailable", "evidence_unavailable"}:
        port, method = ((deps.work_store, "list_items") if contradiction == "work_unavailable"
                        else (deps.evidence_store, "list_for_run"))
        monkeypatch.setattr(port, method, AsyncMock(side_effect=RuntimeError(
            "synthetic unavailable activity store",
        )))
    else:
        original_list = deps.work_store.list_items

        async def mutate_during_read(**kwargs):
            await activation.entering(peer)
            return await original_list(**kwargs)

        monkeypatch.setattr(deps.work_store, "list_items", mutate_during_read)
    control.approve(pending)
    calls = len(provider.requests)
    code, finals = await drive_resume(deps, pending)
    assert code == 1 and not finals
    assert not gateway.invocations and len(provider.requests) == calls
    assert not control.restored


async def test_first_native_model_call_observes_committed_entered_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = NoFollowupProvider()
    engagement, deps, _ = await assembled(tmp_path, provider, durable=True)
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    native = provider.complete_native
    seen = []

    async def checking(*args, **kwargs):
        async def stages(tx):
            return [row["launch_stage"] for (kind, _), row in tx.records.items()
                    if kind == "task_activation"]
        seen.extend(await coordinator.change(stages))
        assert seen and set(seen) == {"entered"}
        return await native(*args, **kwargs)

    monkeypatch.setattr(provider, "complete_native", checking)
    final = await run_engagement(engagement, deps)
    assert final.is_terminal and seen


async def test_checkpoint_negative_check_uses_exact_original_instance_thread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, _ = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    _, peer = await reserved_peer(deps, pending)
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        assert await driver.runner_for(pending).checkpoint_exists(pending)
        assert not await driver.runner_for(peer).checkpoint_exists(peer)


async def test_graph_entry_failure_leaves_possible_entry_marker_not_fresh_proof(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation, peer = await reserved_peer(deps, pending)
    calls = len(provider.requests)
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        monkeypatch.setattr(driver.runner_for(peer), "_ensure_graph", AsyncMock(
            side_effect=RuntimeError("synthetic graph initialization failure"),
        ))
        events = [event async for event in driver.drive([peer])]
    assert len(events) == 1 and events[0].kind == "failed"
    assert not gateway.invocations and len(provider.requests) == calls
    with pytest.raises(StateConflict, match="^activation_checkpoint_required$"):
        await activation.unstarted(
            str(peer.run_id), checkpoint_instances={str(pending.instance_id)},
        )


@pytest.mark.parametrize("pending_kind", ["reservation", "outbox"])
async def test_lifecycle_finish_does_not_ignore_invisible_backlog(
    tmp_path, monkeypatch, pending_kind,
):
    from redteam.app.specialist_run import SpecialistLifecycle

    monkeypatch.chdir(tmp_path)
    provider, control = NoFollowupProvider(), OneApprovalControl()
    engagement, deps, _ = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    activation = TaskActivation(
        coordinator_for(deps.task_graph_store, deps.path_store), deps.agents,
    )
    # Isolate backlog from the visible actor; this is not task success or Control approval.
    await activation.returned(pending.model_copy(update={"pending_approval_id": None}))
    if pending_kind == "reservation":
        await reserved_peer(deps, pending)
    else:
        async def enqueue(tx):
            tx.queue(EventEnvelope(
                event_id="event-invisible", event_type=EventType.TASK_CREATED,
                run_id=pending.run_id, engagement_id=pending.engagement_id, source="test",
                timestamp=datetime.now(timezone.utc),
            ))
        await activation.coordinator.change(enqueue)
    await SpecialistLifecycle(pending, deps).finish([], covers_cohort=True)
    async def state(tx):
        return tx.records[("specialist_lifecycle", str(pending.run_id))]["state"]
    assert await activation.coordinator.change(state) == "held"


@pytest.mark.parametrize("pending_kind", ["reservation", "outbox"])
@pytest.mark.parametrize("when", ["before", "during-review"])
async def test_goal_checks_backlog_before_review_and_inside_completion_transaction(
    pending_kind, when,
):
    from redteam.attack_graph.observation import GraphObservation
    from redteam.orchestration.engine import InvestigationEngine, TurnDecision
    from tests.integration import _engine_llm_harness as h
    from tests.integration.test_completion_review_atomicity import setup

    world, runtime, path, reviewer = await setup("goal")
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    world.deps.specialist_policy = SimpleNamespace()
    coordinator = coordinator_for(world.tasks, world.paths)
    async def quiescent(tx):
        tx.records[("specialist_lifecycle", str(runtime.run_id))] = {"state": "quiescent"}
    await coordinator.change(quiescent)

    async def backlog():
        async def add(tx):
            if pending_kind == "reservation":
                tx.records[("task_activation", "original-orphan")] = {
                    "run_id": str(runtime.run_id), "state": "launched",
                }
            else:
                tx.queue(EventEnvelope(
                    event_id="event-late", event_type=EventType.TASK_CREATED,
                    run_id=runtime.run_id, engagement_id=runtime.engagement_id, source="test",
                    timestamp=datetime.now(timezone.utc),
                ))
        await coordinator.change(add)
    if when == "before":
        await backlog()
        assert await engine.complete_goal(
            path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, context,
        ) == ["specialist_followup_review_pending"]
        assert reviewer.calls == 0
    else:
        reviewer.hook = backlog
        with pytest.raises(StateConflict, match="^specialist_followup_review_pending$"):
            await engine.complete_goal(
                path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, context,
            )
        assert reviewer.calls == 1
    assert (await world.tasks.get_mission(runtime.mission_id)).status == MissionStatus.ACTIVE
