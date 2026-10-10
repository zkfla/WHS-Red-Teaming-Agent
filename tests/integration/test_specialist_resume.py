"""Specialist approval/cohort continuation through native checkpoint recovery.

Scripted LLM and synthetic tools only; no live provider or external target.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli.commands.resume import _drive_cohort_guarded
from redteam.app.cli.commands.run import _wait_for_approval
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResourceBinding
from redteam.core.enums import ApprovalStatus, MissionStatus, PolicyVerdict, TaskStatus
from redteam.orchestration.checkpointing import list_checkpoint_threads
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.execution_reconciliation import ExecutionReconciliation
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.operator_intents.service import OperatorIntentService
from redteam.orchestration.resource_conflicts import resource_conflict_keys
from redteam.orchestration.state import TerminationReason
from tests.fakes import FakeControlPlane
from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled
from tests.unit.test_resource_conflicts import resource_work


class OneApprovalControl(FakeControlPlane):
    def __init__(self, *, at=1):
        super().__init__()
        self.at = at
        self.restored = []

    async def evaluate(self, proposal, context):
        self.default_verdict = (PolicyVerdict.REQUIRE_APPROVAL
                                if len(self.evaluated) + 1 == self.at else PolicyVerdict.ALLOW)
        return await super().evaluate(proposal, context)

    async def rehydrate(self, engagement_id, **kwargs):
        self.restored.append(kwargs)

    def approve(self, pending):
        self.resolve_approval(pending.pending_approval_id, PolicyDecision(
            action_id=pending.pending_action_id, verdict=PolicyVerdict.ALLOW,
            evaluated_at=datetime.now(timezone.utc), approval_id=pending.pending_approval_id,
        ))


async def identities(deps, run_id):
    return await list_checkpoint_threads(
        backend=deps.settings.checkpoint_backend,
        path=deps.settings.checkpoint_path, run_id=str(run_id),
    )


async def resume(deps, pending, *, selection=None):
    cohort = await identities(deps, pending.run_id)
    finals = []
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        code = await _drive_cohort_guarded(
            driver, SimpleNamespace(run_id=str(pending.run_id)), deps,
            cohort if selection is None else selection(cohort), cohort,
            on_finished=finals.append,
        )
    return code, finals


async def test_resource_wait_keeps_task_contract_and_resumes_full_specialist_loop(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider, durable=True)
    gateway._tools[0].resource_bindings = [
        ToolResourceBinding(namespace="browser.profile", key="synthetic-profile"),
    ]
    deps = replace(deps, settings=deps.settings.model_copy(update={
        "resource_wait_timeout_seconds": 0.05,
    }))
    key = resource_conflict_keys(gateway._tools[0], {})[0]
    blocker = resource_work("original-owner", key=key, run="independent-run")
    await deps.work_store.add(blocker)
    now = datetime.now(timezone.utc)
    await deps.work_store.acquire(blocker.work_id, owner="independent-run", now=now,
                                  ttl=timedelta(seconds=10))
    pending = await run_engagement(engagement, deps)
    assert pending.termination_reason == TerminationReason.RESOURCE_WAIT
    assert not gateway.invocations and provider.native_turns == 1
    (task,) = await deps.task_graph_store.list_tasks(mission_id=pending.mission_id)
    assert task.status is TaskStatus.RUNNING
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)

    async def held_state(tx):
        activation = next(row for (kind, _), row in tx.records.items()
                          if kind == "task_activation" and row["run_id"] == str(pending.run_id))
        return tx.records[("specialist_lifecycle", str(pending.run_id))]["state"], activation

    state, activation = await coordinator.change(held_state)
    assert state == "held" and activation["state"] == "held"
    original_hash = activation["completion_contract_hash"]
    assert await deps.work_store.release(blocker.work_id, owner="independent-run", now=now)
    code, finals = await resume(deps, pending)
    assert code == 0 and len(finals) == 2
    assert len(gateway.invocations) == 2
    original_tasks = await deps.task_graph_store.list_tasks(mission_id=pending.mission_id)
    assert len(original_tasks) == 2
    assert all(t.status is TaskStatus.COMPLETED for t in original_tasks)
    assert pending.instance_id in {final.instance_id for final in finals}
    _, original_activation = await coordinator.change(held_state)
    assert original_activation["completion_contract_hash"] == original_hash
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    assert mission.status is MissionStatus.COMPLETED


@pytest.mark.parametrize("entrypoint", ["cohort", "watch"])
async def test_exact_approval_resume_creates_followup_without_budget_or_identity_reset(
    tmp_path, monkeypatch, entrypoint,
):
    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    assert pending.termination_reason == TerminationReason.APPROVAL_PENDING
    assert not gateway.invocations
    ledger = RunRequestLedger(coordinator_for(deps.task_graph_store, deps.path_store))
    before = await ledger.snapshot(str(pending.run_id))
    control.approve(pending)
    if entrypoint == "cohort":
        code, finals = await resume(deps, pending)
        assert code == 0
    else:
        from redteam.app.cli.commands import run as run_cmd

        monkeypatch.setattr(run_cmd, "_APPROVAL_POLL_SECONDS_IN_SESSION", 0.001)
        final = await _wait_for_approval(deps, pending, echo=lambda _: None, in_session=True)
        assert final.pending_approval_id is None
        # Enumerate the actual original and followup checkpoints after watch.
        finals = await identities(deps, pending.run_id)
    assert len(gateway.invocations) == 2
    assert str(gateway.invocations[0].action_id) == str(pending.pending_action_id)
    assert len({final.instance_id for final in finals}) == 2
    assert pending.instance_id in {final.instance_id for final in finals}
    assert control.restored[0]["actions_consumed"] == 0
    tasks = await deps.task_graph_store.list_tasks(mission_id=pending.mission_id)
    assert len(tasks) == 2 and all(task.status == TaskStatus.COMPLETED for task in tasks)
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    assert mission.status == MissionStatus.COMPLETED
    after = await ledger.snapshot(str(pending.run_id))
    assert after["spent"] > before["spent"]
    assert after["spent"] == len(provider.requests)
    assert after["reserved"] == 0


class TwoTaskProvider(ClosedLoopProvider):
    async def complete_structured(self, **kwargs):
        result = await super().complete_structured(**kwargs)
        props = kwargs["response_schema"].get("properties", {})
        if "questions" in props:
            if self.strategies != 1:
                return {"questions": [], "reason": "No new grounded followup"}
            question = result["questions"][0]
            result["questions"].append({**question, "question_id": "second"})
        if "tasks" in props:
            # The fixture's two independent questions retain identical conditions.
            result["tasks"].append({**result["tasks"][0], "task_key": "second",
                                    "question_id": "second"})
        return result


async def test_pending_runtime_keeps_own_checkpoint_counters_not_sibling_totals(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider, control = TwoTaskProvider(), OneApprovalControl(at=2)
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    assert pending.pending_approval_id is not None
    assert len(gateway.invocations) == 1
    assert pending.actions_executed == 0
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        recovered = await driver.runner_for(pending).recover_runtime(
            pending.run_id, instance_id=pending.instance_id,
        )
    assert recovered is not None
    assert recovered.pending_action_id == pending.pending_action_id
    assert recovered.actions_executed == pending.actions_executed
    assert recovered.model_requests_used == pending.model_requests_used
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)

    async def state(tx):
        return tx.records[("specialist_lifecycle", str(pending.run_id))]["state"]

    assert await coordinator.change(state) == "held"


@pytest.mark.parametrize("partial", [True, False])
async def test_resume_does_not_replay_completed_sibling_or_close_partial_cohort(
    tmp_path, monkeypatch, partial,
):
    monkeypatch.chdir(tmp_path)
    provider, control = TwoTaskProvider(), OneApprovalControl(at=2)
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    control.approve(pending)
    planning_before = provider.strategies
    code, finals = await resume(
        deps, pending, selection=(lambda cohort: [
            row for row in cohort if str(row.instance_id) == str(pending.instance_id)
        ]) if partial else None,
    )
    assert code == 0 and len(finals) == (1 if partial else 2)
    assert pending.instance_id in {final.instance_id for final in finals}
    assert len(gateway.invocations) == 2
    assert control.restored[0]["actions_consumed"] == 1  # FULL cohort, not selection
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    if partial:
        assert provider.strategies == planning_before
        assert mission.status != MissionStatus.COMPLETED
    else:
        assert mission.status == MissionStatus.COMPLETED


async def test_unresolved_approval_tick_does_not_invoke_model_tool_or_new_task(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    calls = len(provider.requests)
    code, finals = await resume(deps, pending)
    assert code == 0 and len(finals) == 1
    assert finals[0].pending_approval_id == pending.pending_approval_id
    assert finals[0].pending_action_id == pending.pending_action_id
    assert not gateway.invocations and len(provider.requests) == calls
    assert len(await deps.task_graph_store.list_tasks(mission_id=pending.mission_id)) == 1


async def test_rejected_approval_does_not_execute_or_fabricate_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    control.resolve_approval(pending.pending_approval_id, PolicyDecision(
        action_id=pending.pending_action_id, verdict=PolicyVerdict.DENY,
        evaluated_at=datetime.now(timezone.utc), approval_id=pending.pending_approval_id,
    ))
    # An alternative is allowed to be proposed, but needs a NEW approval.
    # Denying the original action is not an engagement-wide permission change.
    control.at = len(control.evaluated) + 1
    code, _ = await resume(deps, pending)
    assert code == 0 and not gateway.invocations
    assert provider.strategies == 1
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    assert mission.status != MissionStatus.COMPLETED


@pytest.mark.parametrize("instruction", ["pause", "stop"])
async def test_approval_watch_tick_never_implicitly_resumes_operator_suspension(
    tmp_path, monkeypatch, instruction,
):
    monkeypatch.chdir(tmp_path)
    provider, control = ClosedLoopProvider(), OneApprovalControl()
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    service = OperatorIntentService.from_deps(deps)
    assert service is not None
    application = await service.receive_and_apply(
        instruction, mission_id=pending.mission_id, run_id=pending.run_id,
    )
    assert application.applied
    control.approve(pending)
    calls = len(provider.requests)
    cohort = await identities(deps, pending.run_id)
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        await _drive_cohort_guarded(
            driver, SimpleNamespace(run_id=str(pending.run_id)), deps, cohort, cohort,
            resume_paused=False,
        )
    assert not gateway.invocations and len(provider.requests) == calls
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    assert mission.status == (MissionStatus.PAUSED if instruction == "pause"
                              else MissionStatus.CANCELLED)


async def test_real_control_requires_separate_approval_for_followup_and_never_replays_sibling(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(
        tmp_path, provider, durable=True, real_control=True,
    )
    first = await run_engagement(engagement, deps)
    assert first.pending_approval_id is not None and not gateway.invocations
    await deps.control_plane.resolve_approval(
        first.pending_approval_id, ApprovalStatus.APPROVED, "synthetic-operator",
    )
    code, finals = await resume(deps, first)
    assert code == 0 and len(gateway.invocations) == 1
    assert str(gateway.invocations[0].action_id) == str(first.pending_action_id)
    second = next(item for item in finals if item.pending_approval_id)
    assert second.pending_approval_id != first.pending_approval_id
    assert second.instance_id != first.instance_id
    mission = await deps.task_graph_store.get_mission(first.mission_id)
    assert mission.status != MissionStatus.COMPLETED
    await deps.control_plane.resolve_approval(
        second.pending_approval_id, ApprovalStatus.APPROVED, "synthetic-operator",
    )
    code, _ = await resume(deps, second)
    assert code == 0 and len(gateway.invocations) == 2
    assert str(gateway.invocations[1].action_id) == str(second.pending_action_id)
    mission = await deps.task_graph_store.get_mission(first.mission_id)
    assert mission.status == MissionStatus.COMPLETED
    ledger = RunRequestLedger(coordinator_for(deps.task_graph_store, deps.path_store))
    assert (await ledger.snapshot(str(first.run_id)))["spent"] == len(provider.requests)


async def test_unknown_sibling_defers_new_tasks_and_goal_but_retains_full_accounting(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    provider, control = TwoTaskProvider(), OneApprovalControl(at=2)
    engagement, deps, gateway = await assembled(tmp_path, provider, control=control, durable=True)
    pending = await run_engagement(engagement, deps)
    control.approve(pending)
    before = provider.strategies
    cohort = await identities(deps, pending.run_id)
    async with InstanceDriver(deps, settings=deps.settings) as driver:
        sibling = next(row for row in cohort if row.instance_id != pending.instance_id)
        runner = driver.runner_for(pending.model_copy(update={"instance_id": sibling.instance_id}))
        recover = runner.recover_reconciling

        async def unknown(*args, **kwargs):
            runtime, _ = await recover(*args, **kwargs)
            return runtime, ExecutionReconciliation(
                runtime=runtime, pending_invocations=["synthetic-unknown-invocation"],
            )

        monkeypatch.setattr(runner, "recover_reconciling", unknown)
        code = await _drive_cohort_guarded(
            driver, SimpleNamespace(run_id=str(pending.run_id)), deps, cohort, cohort,
        )
    assert code == 0 and len(gateway.invocations) == 2
    assert control.restored[0]["actions_consumed"] == 1
    assert provider.strategies == before
    mission = await deps.task_graph_store.get_mission(pending.mission_id)
    assert mission.status != MissionStatus.COMPLETED
    coordinator = coordinator_for(deps.task_graph_store, deps.path_store)

    async def state(tx):
        return tx.records[("specialist_lifecycle", str(pending.run_id))]["state"]

    assert await coordinator.change(state) == "held"
