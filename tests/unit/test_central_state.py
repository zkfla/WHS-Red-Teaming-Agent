"""Real SQLite/process competition; synthetic evidence and no provider traffic."""

import asyncio
import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pytest

from redteam.app.bootstrap import build_central_storage
from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.engagement import Engagement, EngagementScope
from redteam.core.contracts.events import EventEnvelope
from redteam.core.contracts.execution import ExecutionState, TaskExecution
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.mission import (
    InvestigationResult,
    MissionPlan,
    MissionTask,
    OperatorIntent,
)
from redteam.core.contracts.network import GrantRefused, NetworkGrant
from redteam.core.contracts.policy import Approval
from redteam.core.contracts.tool import ToolInvocation, ToolResult
from redteam.core.convert import digest_payload
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import MissionId, OperatorIntentId
from redteam.core.tool_binding import execution_fingerprint
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.central_state import (
    CentralStateStore,
    CentralTransaction,
    check_evidence,
)
from redteam.orchestration.coordination import CoordinationError, StateConflict, content_hash
from redteam.orchestration.execution_journal import append_execution_receipt
from redteam.orchestration.llm_requests import RequestAdmissionError
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from tests.unit.test_network_ledger import budget, caps

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
RUN, ENG, MISSION = "run-central", "eng-central", "mission-central"
BINDING = "b" * 64


def mission(identity=MISSION, engagement=ENG):
    return MissionPlan.model_validate(
        {
            "mission_id": identity,
            "engagement_id": engagement,
            "objective": "Investigate scope",
            "success_criteria": ["Evaluate authorization"],
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
            "provenance": {"source": "test"},
        }
    )


def task(identity="task-a", mission_id=MISSION):
    return MissionTask.model_validate(
        {
            "task_id": identity,
            "mission_id": mission_id,
            "title": "Authorization question",
            "objective": "Check authorization",
            "status": "ready",
            "assigned_agent": "web",
            "created_at": NOW,
            "updated_at": NOW,
            "provenance": {"source": "test"},
        }
    )


async def world(tmp_path, *, run=RUN, engagement=ENG, mission_id=MISSION):
    state = CentralStateStore(tmp_path / "state.db", run_id=run, engagement_id=engagement)
    await state.setup()
    await state.create_mission(mission(mission_id, engagement))
    await state.bind_run(mission_id, scope=EngagementScope(), binding_hash=BINDING)
    return state


async def owner(state, identity="task-a", worker="worker-a", instance="instance-a"):
    await state.register_task(f"register-{identity}", task(identity))
    claimed = await state.claim_task(identity, owner=worker, instance_id=instance)
    await state.transaction(
        lambda tx: tx.entering(
            identity,
            owner=worker,
            version=claimed.ownership_version,
            instance_id=instance,
        )
    )
    return claimed


async def original(
    store,
    identity,
    *,
    action="action-a",
    invocation="invocation-a",
    run=RUN,
    task_id="task-a",
    instance_id="instance-a",
    uncertain=False,
):
    from redteam.core.contracts.action import ActionProposal
    from redteam.core.contracts.evidence import EvidenceRecord

    if identity.startswith(("proposal-", "result-")):
        proposal = ActionProposal.model_validate(
            {
                "action_id": action,
                "run_id": run,
                "engagement_id": ENG,
                "agent_id": "web",
                "instance_id": instance_id,
                "binding_mode": "selected_tool",
                "tool_id": "tool-any",
                "target": None,
                "arguments": {},
                "reason": "Synthetic proposal",
                "created_at": NOW,
            }
        )
        invocation_record = ToolInvocation.model_validate(
            {
                "invocation_id": invocation,
                "action_id": action,
                "run_id": run,
                "tool_id": "tool-any",
                "binding_mode": "selected_tool",
                "arguments": {},
                "execution_context": {
                    "engagement_id": ENG,
                    "run_id": run,
                    "agent_id": "web",
                    "instance_id": instance_id,
                },
                "created_at": NOW,
            }
        )
        step = PlanStep.model_validate(
            {
                "step_id": "synthetic",
                "tool_id": "tool-any",
                "binding_mode": "selected_tool",
                "execution_origin_version": 1,
                "mission_task_id": task_id,
                "task_purpose_hash": completion_contract_hash(task(task_id)),
                "model_tool_call_id": invocation.replace("invocation-", "call-"),
                "reason": "Synthetic proposal",
            }
        )
        raw_result = None
        if identity.startswith("result-"):
            raw_result = ToolResult.model_validate(
                {
                    "invocation_id": invocation,
                    "action_id": action,
                    "tool_id": "tool-any",
                    "success": True,
                    "output": {"synthetic": True},
                    "started_at": NOW,
                    "finished_at": NOW,
                    "metadata": {"execution_uncertain": uncertain},
                }
            )
        # Preserve the existing receipt contract, without writing a second
        # differently identified copy. append_execution_receipt assigns IDs.
        record = await append_execution_receipt(
            store,
            invocation=invocation_record,
            proposal=proposal,
            step=step,
            work_id=None,
            result=raw_result,
        )
        return await check_evidence(
            store,
            run_id=run,
            engagement_id=ENG,
            evidence_ids=[str(record.evidence_id)],
        )
    record = EvidenceRecord.model_validate(
        {
            "evidence_id": identity,
            "run_id": run,
            "engagement_id": ENG,
            "source_type": "system",
            "source_id": "synthetic-receipt",
            "action_id": action,
            "invocation_id": invocation,
            "content_type": "application/json",
            "digest": "d" * 64,
            "created_at": NOW,
            "summary": "Synthetic original",
        }
    )
    await store.append(record)
    return await check_evidence(store, run_id=run, engagement_id=ENG, evidence_ids=[identity])


def execution(**overrides):
    return TaskExecution.model_validate(
        {
            "engagement_id": ENG,
            "run_id": RUN,
            "task_id": "task-a",
            "instance_id": "instance-a",
            "ownership_version": 1,
            "action_id": "action-a",
            "invocation_id": "invocation-a",
            "model_tool_call_id": "call-a",
            "tool_id": "tool-any",
            "input_digest": digest_payload({}),
            "proposal_evidence_id": "proposal-a",
            "resource_keys": ["session-a"],
            "resource_lease_until": 200,
            **overrides,
        }
    )


def _claim_in_process(path, worker):
    async def compete():
        state = CentralStateStore(path, run_id=RUN, engagement_id=ENG)
        await state.setup()
        try:
            claimed = await state.claim_task("task-a", owner=worker, instance_id=worker)
        except StateConflict:
            return False
        return claimed.occupant_owner == worker

    return asyncio.run(compete())


async def test_process_claim_has_one_owner_and_stale_generation_cannot_write(tmp_path):
    state = await world(tmp_path)
    await state.register_task("register", task())
    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context("spawn"),
    ) as pool:
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            *(
                loop.run_in_executor(pool, _claim_in_process, str(state.path), worker)
                for worker in ("worker-a", "worker-b")
            )
        )
    assert sum(results) == 1
    claimed = await state.get_task("task-a")
    assert claimed is not None
    previous = claimed.occupant_owner
    transferred = await state.transaction(
        lambda tx: tx.recover_unentered(
            "task-a",
            previous_owner=previous,
            version=claimed.ownership_version,
            owner="replacement",
            instance_id="replacement-instance",
        )
    )
    assert transferred.ownership_version > claimed.ownership_version
    with pytest.raises(StateConflict, match="stale_task_ownership"):
        await state.transaction(
            lambda tx: tx.release_task(
                "task-a",
                owner=previous,
                version=claimed.ownership_version,
            )
        )


async def test_registration_exact_replay_is_atomic_and_equal_subjects_remain_allowed(tmp_path):
    state = await world(tmp_path)
    second = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    first, replay = await asyncio.gather(
        state.register_task("request-a", task()),
        second.register_task("request-a", task()),
    )
    assert first == replay
    with pytest.raises(StateConflict, match="registration_request_identity_conflict"):
        await state.register_task("request-a", task().model_copy(update={"objective": "Other"}))
    await second.register_task("request-b", task("task-b"))
    assert len(await state.list_tasks()) == 2
    changed = await second.claim_task("task-a", owner="worker", instance_id="instance")
    assert (await state.get_task("task-a")) == changed
    # Replay returns the current row, not a second original/ready snapshot.
    assert (await state.register_task("request-a", task())).occupant_owner == "worker"
    with sqlite3.connect(state.path) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM state_tasks WHERE task_id='task-a'",
            ).fetchone()[0]
        )
    assert not {"priority", "dependency_task_ids", "parent_task_id"} & payload.keys()


@pytest.mark.parametrize(
    "field,value",
    [
        ("dependency_task_ids", ["parent"]),
        ("parent_task_id", "parent"),
        ("priority", 5),
    ],
)
async def test_no_dag_or_candidate_scheduler_is_recreated(tmp_path, field, value):
    state = await world(tmp_path)
    with pytest.raises(CoordinationError, match="without_dag"):
        await state.register_task("request", task().model_copy(update={field: value}))


async def test_unknown_cancellation_preserves_resource_conflicts_and_blocks_reassignment(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    intent = execution(proposal_evidence_id=proposal[0].record.evidence_id)
    await state.transaction(
        lambda tx: tx.record_execution(
            intent,
            owner="worker-a",
            checked=proposal,
            now=100,
        )
    )
    entered = await state.transaction(
        lambda tx: tx.update_execution(
            intent.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a",
            now=100,
        )
    )
    assert entered.resource_execution_pending
    cancelled = await state.transaction(
        lambda tx: tx.update_execution(
            entered.model_copy(update={"state": ExecutionState.CANCELLED}),
            owner="worker-a",
            now=500,
        )
    )
    assert cancelled.state is ExecutionState.UNKNOWN and cancelled.resource_execution_pending
    with pytest.raises(StateConflict, match="unresolved_activity"):
        await state.transaction(lambda tx: tx.release_task("task-a", owner="worker-a", version=1))
    with pytest.raises(StateConflict, match="not_assignable"):
        await state.claim_task("task-a", owner="another", instance_id="another")
    with pytest.raises(StateConflict, match="cannot_be_replayed"):
        await state.transaction(
            lambda tx: tx.update_execution(
                cancelled.model_copy(update={"state": ExecutionState.RUNNING}),
                owner="worker-a",
                now=500,
            )
        )
    await owner(state, "task-b", "worker-b", "instance-b")
    another = execution(
        task_id="task-b",
        instance_id="instance-b",
        action_id="action-b",
        invocation_id="invocation-b",
        proposal_evidence_id="proposal-b",
        model_tool_call_id="call-b",
        resource_lease_until=600,
    )
    proof_b = await original(
        evidence,
        "proposal-b",
        action="action-b",
        invocation="invocation-b",
        task_id="task-b",
        instance_id="instance-b",
    )
    another = another.model_copy(update={"proposal_evidence_id": proof_b[0].record.evidence_id})
    with pytest.raises(StateConflict, match="shared_resource"):
        await state.transaction(
            lambda tx: tx.record_execution(
                another,
                owner="worker-b",
                checked=proof_b,
                now=500,
            )
        )
    resolved_proof = await original(evidence, "result-a")
    resolved = await state.transaction(
        lambda tx: tx.update_execution(
            cancelled.model_copy(
                update={
                    "state": ExecutionState.SUCCEEDED,
                    "result_evidence_id": resolved_proof[0].record.evidence_id,
                }
            ),
            owner="worker-a",
            checked=resolved_proof,
            now=500,
        )
    )
    assert not resolved.resource_execution_pending and resolved.resource_lease_until == 0
    assert (await state.get_task("task-a")).status is TaskStatus.RUNNING
    # Confirmed earlier calls do not consume/finish the investigation itself.
    next_call = execution(
        invocation_id="invocation-c",
        action_id="action-c",
        model_tool_call_id="call-c",
        proposal_evidence_id="proposal-c",
        resource_keys=[],
        resource_lease_until=0,
    )
    proof_c = await original(evidence, "proposal-c", action="action-c", invocation="invocation-c")
    next_call = next_call.model_copy(update={"proposal_evidence_id": proof_c[0].record.evidence_id})
    await state.transaction(
        lambda tx: tx.record_execution(
            next_call,
            owner="worker-a",
            checked=proof_c,
            now=500,
        )
    )
    await state.transaction(
        lambda tx: tx.record_execution(
            another,
            owner="worker-b",
            checked=proof_b,
            now=500,
        )
    )


@pytest.mark.parametrize("wait", ["approval", "job"])
async def test_waits_hold_original_action_and_cannot_be_general_claims(tmp_path, wait):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    intent = execution(
        proposal_evidence_id=proposal[0].record.evidence_id,
        wait_reason="approval" if wait == "approval" else None,
        approval_ref="approval-a" if wait == "approval" else None,
        approval_fingerprint=execution_fingerprint("tool-any", None, {})
        if wait == "approval"
        else None,
    )
    await state.transaction(
        lambda tx: tx.record_execution(
            intent,
            owner="worker-a",
            checked=proposal,
            now=100,
        )
    )
    if wait == "job":
        entered = await state.transaction(
            lambda tx: tx.update_execution(
                intent.model_copy(update={"state": ExecutionState.RUNNING}),
                owner="worker-a",
                now=100,
            )
        )
        result = await original(evidence, "result-a")
        await state.transaction(
            lambda tx: tx.update_execution(
                entered.model_copy(
                    update={
                        "state": ExecutionState.SUCCEEDED,
                        "wait_reason": "job",
                        "job_ref": "original-job",
                        "result_evidence_id": result[0].record.evidence_id,
                    }
                ),
                owner="worker-a",
                checked=result,
                now=100,
            )
        )
    with pytest.raises(StateConflict, match="not_assignable"):
        await state.claim_task("task-a", owner="replacement", instance_id="replacement")
    with pytest.raises(StateConflict, match="recovery_not_proven"):
        await state.transaction(
            lambda tx: tx.recover_unentered(
                "task-a",
                previous_owner="worker-a",
                version=1,
                owner="replacement",
                instance_id="replacement",
            )
        )
    saved = await state.transaction(lambda tx: tx.get_execution("invocation-a"), write=False)
    assert str(saved.action_id) == "action-a"
    assert saved.wait_reason == wait
    if wait == "approval":
        approved = Approval.model_validate(
            {
                "approval_id": "approval-a",
                "action_id": "action-a",
                "status": "approved",
                "requested_at": NOW,
                "execution_fingerprint": execution_fingerprint("tool-any", None, {}),
            }
        )
        with pytest.raises(StateConflict, match="approval_original"):
            await state.transaction(
                lambda tx: tx.resolve_approval(
                    "invocation-a",
                    owner="worker-a",
                    version=1,
                    approval=approved.model_copy(update={"execution_fingerprint": "a" * 64}),
                )
            )
        await state.transaction(
            lambda tx: tx.resolve_approval(
                "invocation-a",
                owner="worker-a",
                version=1,
                approval=approved,
            )
        )
        assert (await state.get_task("task-a")).status is TaskStatus.RUNNING
        assert (
            await state.transaction(
                lambda tx: tx.get_execution("invocation-a"),
                write=False,
            )
        ).wait_reason is None


async def test_opaque_physical_resource_conflicts_across_runs_without_cross_run_reads(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    first = execution(proposal_evidence_id=proposal[0].record.evidence_id)
    await state.transaction(
        lambda tx: tx.record_execution(
            first,
            owner="worker-a",
            checked=proposal,
            now=100,
        )
    )
    await state.transaction(
        lambda tx: tx.update_execution(
            first.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a",
            now=100,
        )
    )
    other = await world(tmp_path, run="other-run", mission_id="other-mission")
    await other.register_task("register", task("task-b", mission_id="other-mission"))
    await other.claim_task("task-b", owner="worker-b", instance_id="instance-b")
    await other.transaction(
        lambda tx: tx.entering(
            "task-b",
            owner="worker-b",
            version=1,
            instance_id="instance-b",
        )
    )
    proof_b = await original(
        evidence,
        "proposal-b",
        action="action-b",
        invocation="invocation-b",
        task_id="task-b",
        instance_id="instance-b",
        run="other-run",
    )
    second = execution(
        run_id="other-run",
        task_id="task-b",
        instance_id="instance-b",
        invocation_id="invocation-b",
        action_id="action-b",
        model_tool_call_id="call-b",
        proposal_evidence_id=proof_b[0].record.evidence_id,
        resource_lease_until=600,
    )
    with pytest.raises(StateConflict, match="shared_resource"):
        await other.transaction(
            lambda tx: tx.record_execution(
                second,
                owner="worker-b",
                checked=proof_b,
                now=500,
            )
        )
    assert await other.get_task("task-a") is None
    assert await other.transaction(lambda tx: tx.get_execution("invocation-a"), write=False) is None


async def test_one_approval_cannot_clear_another_unknown_execution_wait(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    first_proof = await original(evidence, "proposal-a")
    first = execution(proposal_evidence_id=first_proof[0].record.evidence_id, resource_keys=[])
    await state.transaction(
        lambda tx: tx.record_execution(
            first,
            owner="worker-a",
            checked=first_proof,
            now=100,
        )
    )
    second_proof = await original(
        evidence,
        "proposal-b",
        action="action-b",
        invocation="invocation-b",
    )
    second = execution(
        invocation_id="invocation-b",
        action_id="action-b",
        model_tool_call_id="call-b",
        proposal_evidence_id=second_proof[0].record.evidence_id,
        resource_keys=[],
        wait_reason="approval",
        approval_ref="approval-b",
        approval_fingerprint=execution_fingerprint("tool-any", None, {}),
    )
    await state.transaction(
        lambda tx: tx.record_execution(
            second,
            owner="worker-a",
            checked=second_proof,
            now=100,
        )
    )
    await state.transaction(
        lambda tx: tx.update_execution(
            first.model_copy(update={"state": ExecutionState.UNKNOWN}),
            owner="worker-a",
            now=100,
        )
    )
    approved = Approval.model_validate(
        {
            "approval_id": "approval-b",
            "action_id": "action-b",
            "status": "approved",
            "requested_at": NOW,
            "execution_fingerprint": second.approval_fingerprint,
        }
    )
    await state.transaction(
        lambda tx: tx.resolve_approval(
            "invocation-b",
            owner="worker-a",
            version=1,
            approval=approved,
        )
    )
    assert (await state.get_task("task-a")).status is TaskStatus.BLOCKED
    assert (await state.get_task("task-a")).blocking_reason == "unknown:action-a"


async def test_cancelled_task_still_accepts_original_resolution_without_becoming_running(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    intent = execution(proposal_evidence_id=proposal[0].record.evidence_id)
    await state.transaction(
        lambda tx: tx.record_execution(
            intent,
            owner="worker-a",
            checked=proposal,
            now=100,
        )
    )
    entered = await state.transaction(
        lambda tx: tx.update_execution(
            intent.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a",
            now=100,
        )
    )
    # Represent a later operator consumer's cancellation in the stored row.
    # The execution intent/owner are deliberately retained for settlement.
    await state.transaction(
        lambda tx: (
            tx.db.execute(
                "UPDATE state_tasks SET payload=json_set(payload,'$.status','cancelled') "
                "WHERE run_id=? AND task_id='task-a'",
                (RUN,),
            ).rowcount
        )
    )
    result = await original(evidence, "result-a")
    resolved = await state.transaction(
        lambda tx: tx.update_execution(
            entered.model_copy(
                update={
                    "state": ExecutionState.SUCCEEDED,
                    "result_evidence_id": result[0].record.evidence_id,
                }
            ),
            owner="worker-a",
            checked=result,
            now=100,
        )
    )
    assert not resolved.resource_execution_pending
    assert (await state.get_task("task-a")).status is TaskStatus.CANCELLED


async def test_entered_actor_is_never_recovered_from_an_empty_execution_inventory(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    with pytest.raises(StateConflict, match="recovery_not_proven"):
        await state.transaction(
            lambda tx: tx.recover_unentered(
                "task-a",
                previous_owner="worker-a",
                version=1,
                owner="other",
                instance_id="other",
            )
        )
    with sqlite3.connect(state.path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(state_activations)")}
    assert "runtime" not in columns and "launch_stage" in columns


async def test_owned_update_checks_only_related_task_and_stale_goal_blocks_new_intent(tmp_path):
    state = await world(tmp_path)
    current = await owner(state)
    await state.register_task("sibling", task("task-b"))
    changed = await state.transaction(
        lambda tx: tx.update_task(
            current.model_copy(update={"objective": "Check another angle"}),
            owner="worker-a",
            version=1,
            expected_hash=content_hash(current),
        )
    )
    with pytest.raises(StateConflict, match="task_inputs"):
        await state.transaction(
            lambda tx: tx.update_task(
                current,
                owner="worker-a",
                version=1,
                expected_hash=content_hash(current),
            )
        )
    assert changed.objective == "Check another angle"
    goal = await state.get_mission(MissionId(MISSION))
    await state.update_mission(goal.model_copy(update={"objective": "New goal", "revision": 2}))
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    with pytest.raises(StateConflict, match="goal_revision_changed"):
        await state.transaction(
            lambda tx: tx.record_execution(
                execution(proposal_evidence_id=proposal[0].record.evidence_id),
                owner="worker-a",
                checked=proposal,
                now=100,
            )
        )


async def test_original_result_receipts_cannot_resolve_changed_identity_or_uncertainty(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    proposal = await original(evidence, "proposal-a")
    intent = execution(proposal_evidence_id=proposal[0].record.evidence_id)
    # Original invocation arguments and tool, not just action IDs, bind this row.
    with pytest.raises(StateConflict, match="original_receipt"):
        await state.transaction(
            lambda tx: tx.record_execution(
                intent.model_copy(update={"input_digest": "f" * 64}),
                owner="worker-a",
                checked=proposal,
                now=100,
            )
        )
    await state.transaction(
        lambda tx: tx.record_execution(
            intent,
            owner="worker-a",
            checked=proposal,
            now=100,
        )
    )
    entered = await state.transaction(
        lambda tx: tx.update_execution(
            intent.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a",
            now=100,
        )
    )
    uncertain = await original(evidence, "result-a", uncertain=True)
    with pytest.raises(StateConflict, match="original_receipt_mismatch_or_uncertain"):
        await state.transaction(
            lambda tx: tx.update_execution(
                entered.model_copy(
                    update={
                        "state": ExecutionState.SUCCEEDED,
                        "result_evidence_id": uncertain[0].record.evidence_id,
                    }
                ),
                owner="worker-a",
                checked=uncertain,
                now=100,
            )
        )
    assert (
        await state.transaction(
            lambda tx: tx.get_execution("invocation-a"),
            write=False,
        )
    ).resource_execution_pending


async def test_legacy_outbox_dispatcher_publishes_without_a_database_write_lock(tmp_path):
    state = await world(tmp_path)
    envelope = EventEnvelope.model_validate(
        {
            "event_id": "event-a",
            "event_type": "task_completed",
            "run_id": RUN,
            "engagement_id": ENG,
            "source": "test",
            "timestamp": NOW,
        }
    )
    await state.transaction(lambda tx: tx.queue(envelope))

    class Publisher:
        calls = 0

        async def publish(self, event):
            assert event == envelope
            # If delivery held BEGIN IMMEDIATE, this independent writer would
            # fail. Publication can be slow without blocking storage.
            with sqlite3.connect(state.path, timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE publication_probe (identity INTEGER)")
            self.calls += 1

    publisher = Publisher()
    assert await state.deliver_outbox(publisher) == 0
    restarted = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    assert await restarted.deliver_outbox(publisher) == 0
    assert publisher.calls == 1


async def test_malformed_llm_scope_cannot_hide_previous_charges(tmp_path):
    state = await world(tmp_path)
    ledger = state.request_ledger()
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    request = await ledger.reserve(RUN, owner="worker", kind="native")
    with sqlite3.connect(state.path) as db:
        row = json.loads(
            db.execute(
                "SELECT payload FROM state_requests WHERE kind='llm_request'",
            ).fetchone()[0]
        )
        row["run_id"] = "other-run"
        db.execute(
            "UPDATE state_requests SET payload=? WHERE identity=?",
            (json.dumps(row), request),
        )
    with pytest.raises(RequestAdmissionError, match="record_invalid"):
        await ledger.reserve(RUN, owner="new-worker", kind="native")


async def test_result_questions_refs_and_future_graph_participant_roll_back_together(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    checked = await original(evidence, "receipt-a")
    result = InvestigationResult.model_validate(
        {
            "result_id": "result-a",
            "outcome": "refuted",
            "summary": "No issue reproduced",
            "evidence_ids": ["receipt-a"],
            "completion_receipt_id": "receipt-a",
            "questions": [
                {
                    "question_id": "q1",
                    "question": "Check another principal",
                    "required_for_goal": True,
                    "reason": "Goal includes authorization",
                    "evidence_ids": ["receipt-a"],
                }
            ],
        }
    )

    def fail(tx: CentralTransaction):
        tx.finish_task("task-a", result, owner="worker-a", version=1, checked=checked)
        tx.db.execute("CREATE TABLE future_graph_probe (identity TEXT PRIMARY KEY)")
        tx.db.execute("INSERT INTO future_graph_probe VALUES('node')")
        raise RuntimeError("participant failed")

    with pytest.raises(RuntimeError, match="participant failed"):
        await state.transaction(fail)
    assert (await state.get_task("task-a")).result is None
    assert (await state.inspect_evidence(evidence))["external_only"] == ["receipt-a"]
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM state_evidence_refs").fetchone()[0] == 0
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='future_graph_probe'",
        ).fetchone()


async def test_completed_negative_result_is_idempotent_and_follow_up_is_only_explicit(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    result = InvestigationResult.model_validate(
        {
            "result_id": "result-a",
            "outcome": "inconclusive",
            "summary": "Insufficient evidence",
            "questions": [{"question_id": "q1", "question": "Try another angle"}],
        }
    )
    completed = await state.transaction(
        lambda tx: tx.finish_task(
            "task-a",
            result,
            owner="worker-a",
            version=1,
        )
    )
    assert completed.status is TaskStatus.COMPLETED
    assert (await state.get_mission(MissionId(MISSION))).status is MissionStatus.ACTIVE
    assert len(await state.list_tasks()) == 1
    assert (
        await state.transaction(
            lambda tx: tx.finish_task(
                "task-a",
                result,
                owner="worker-a",
                version=1,
            )
        )
    ) == completed
    with pytest.raises(StateConflict, match="result_identity_conflict"):
        await state.transaction(
            lambda tx: tx.finish_task(
                "task-a",
                result.model_copy(update={"summary": "Different"}),
                owner="worker-a",
                version=1,
            )
        )
    follow_up = task("task-b").model_copy(
        update={
            "source_task_id": completed.task_id,
            "source_result_id": "result-a",
            "source_question_id": "q1",
        }
    )
    await state.register_task("follow-up", follow_up)
    assert (await state.get_task("task-a")).result.questions[0].question_id == "q1"
    assert (await state.get_task("task-b")).source_task_id == completed.task_id


async def test_llm_usage_unknown_and_reservations_survive_restart_without_refund(tmp_path):
    state = await world(tmp_path)
    ledger = state.request_ledger()
    policy = LLMRequestPolicy(mode="bounded", max_requests=3, max_concurrent_requests=2)
    await ledger.bind(RUN, policy)
    request = await ledger.reserve(RUN, owner="worker-a", kind="native")
    await ledger.dispatch(RUN, request)
    await ledger.settle(RUN, request, unknown=True)
    pending = await ledger.reserve(RUN, owner="planner", kind="planning")
    restarted = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    await restarted.setup()
    resumed = restarted.request_ledger()
    await resumed.bind(RUN, policy)
    snapshot = await resumed.snapshot(RUN)
    assert snapshot["unknown_dispatch"] == 1 and snapshot["reserved"] == 1
    await resumed.settle(RUN, request, unknown=True)
    with pytest.raises(RequestAdmissionError, match="cannot_be_refunded"):
        await resumed.release_undispatched(RUN, request)
    await resumed.release_undispatched(RUN, pending)
    await resumed.release_undispatched(RUN, pending)
    second = await resumed.reserve(RUN, owner="worker-b", kind="native")
    await resumed.dispatch(RUN, second)
    await resumed.settle(RUN, second, total_tokens=17)
    third = await resumed.reserve(RUN, owner="worker-c", kind="native")
    await resumed.dispatch(RUN, third)
    with pytest.raises(RequestAdmissionError, match="limit_exhausted"):
        await ledger.reserve(RUN, owner="new-worker", kind="native")
    assert (await ledger.snapshot(RUN))["known_tokens"] == 17
    with pytest.raises(RequestAdmissionError, match="policy_changed"):
        await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=4))


async def test_llm_parallel_admission_uses_one_pool_and_other_runs_are_isolated(tmp_path):
    state = await world(tmp_path)
    ledger = state.request_ledger()
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    results = await asyncio.gather(
        *(
            CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
            .request_ledger()
            .reserve(
                RUN,
                owner=worker,
                kind="native",
            )
            for worker in ("a", "b")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in results) == 1
    assert sum(isinstance(result, RequestAdmissionError) for result in results) == 1
    other = await world(
        tmp_path,
        run="other-run",
        engagement="other-eng",
        mission_id="other-mission",
    )
    with pytest.raises(CoordinationError):
        await ledger.reserve("other-run", owner="worker", kind="native")
    assert await other.get_task("task-a") is None
    assert await other.get_mission(MissionId(MISSION)) is None


def engagement():
    return Engagement.model_validate(
        {
            "engagement_id": ENG,
            "name": "Synthetic",
            "scope": {},
            "created_at": NOW,
            "execution_budget": {"max_actions": 10},
            "rate_limit_policy": {"max_actions_per_minute": 10},
            "approval_policy": {},
        }
    )


async def test_explicit_bootstrap_network_coexists_and_default_paths_are_untouched(tmp_path):
    path = tmp_path / "state.db"
    state, absent = await build_central_storage(engagement(), run_id=RUN, path=path)
    assert absent is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='network_windows'",
        ).fetchone()
    _, network = await build_central_storage(
        engagement(),
        run_id=RUN,
        path=path,
        network_budget=budget(),
    )
    await state.create_mission(mission())
    await state.bind_run(MISSION, scope=EngagementScope(), binding_hash=BINDING)
    await state.register_task("register", task())
    await state.claim_task("task-a", owner="worker-a", instance_id="instance-a")
    assert network is not None
    window = await network.request_window(
        run_id=RUN,
        instance_id="instance-a",
        epoch=1,
        caps=caps(),
    )
    assert isinstance(window, NetworkGrant)
    with pytest.raises(StateConflict, match="unreconciled_network_activity"):
        await state.transaction(
            lambda tx: tx.recover_unentered(
                "task-a",
                previous_owner="worker-a",
                version=1,
                owner="replacement",
                instance_id="replacement",
            )
        )
    settlement = await network.settle_window(
        window_id=window.window_id,
        meter=None,
        verified_closed=False,
    )
    _, resumed = await build_central_storage(
        engagement(),
        run_id=RUN,
        path=path,
        network_budget=budget(),
    )
    assert resumed is not None
    assert (await resumed.window_grant(window_id=window.window_id)) == window
    assert (
        await resumed.settle_window(
            window_id=window.window_id,
            meter=None,
            verified_closed=False,
        )
    ) == settlement
    refusal = await resumed.request_window(
        run_id=RUN,
        instance_id="instance-b",
        epoch=1,
        caps=caps(),
    )
    assert isinstance(refusal, GrantRefused)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM state_tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM network_windows").fetchone()[0] == 1
    from redteam.orchestration.settings import OrchestrationSettings

    defaults = OrchestrationSettings.for_durable_run()
    assert defaults.central_state_path == "data/orchestration/state.sqlite3"
    assert (
        not {"task_graph_path", "work_path", "attack_graph_path"}
        & type(defaults).model_fields.keys()
    )


async def test_intent_effect_and_outbox_are_atomic_idempotent_and_pause_blocks_admission(tmp_path):
    state = await world(tmp_path)
    intent = OperatorIntent.model_validate(
        {
            "intent_id": "intent-a",
            "mission_id": MISSION,
            "intent_type": "pause",
            "instruction": "Pause",
            "created_at": NOW,
            "provenance": {"source": "operator"},
        }
    )
    received = IntentRecord(intent=intent, run_id=RUN, correlation_id="intent-a")
    await state.append(received)
    # A received pause closes admission even before its domain effect is applied.
    with pytest.raises(StateConflict, match="pending_operator"):
        await state.register_task("register", task())
    applied = received.model_copy(update={"status": IntentStatus.APPLIED, "outcome": "paused"})
    event = EventEnvelope.model_validate(
        {
            "event_id": "event-a",
            "event_type": "operator_intent_applied",
            "run_id": RUN,
            "engagement_id": ENG,
            "source": "test",
            "timestamp": NOW,
        }
    )

    def pause(tx: CentralTransaction):
        current = tx.get_mission(MISSION)
        assert current is not None
        tx.update_mission(
            current.model_copy(
                update={
                    "status": MissionStatus.PAUSED,
                    "revision": current.revision + 1,
                }
            ),
            expected_revision=current.revision,
        )
        tx.queue(event)

    await state.transaction(lambda tx: tx.apply_intent(applied, pause))
    await state.transaction(lambda tx: tx.apply_intent(applied, pause))
    assert (await state.get_mission(MissionId(MISSION))).revision == 2
    assert (await state.get(OperatorIntentId("intent-a"))).status is IntentStatus.APPLIED
    await state.append(received)
    assert (await state.get(OperatorIntentId("intent-a"))).status is IntentStatus.APPLIED
    with pytest.raises(CoordinationError, match="mission_not_active"):
        await state.register_task("register", task())
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM state_outbox").fetchone()[0] == 1
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='operator_intents'",
        ).fetchone()


async def test_evidence_integrity_is_per_id_and_detects_orphan_missing_changed(tmp_path):
    state = await world(tmp_path)
    evidence = FilesystemEvidenceStore(tmp_path / "evidence")
    checked = await original(evidence, "evidence-a")
    await state.transaction(lambda tx: tx.add_evidence_refs(checked))
    await original(evidence, "orphan-b")
    index = tmp_path / "evidence" / "_index.jsonl"
    entries = [json.loads(line) for line in index.read_text().splitlines()]
    index.write_text(
        "\n".join(json.dumps(e) for e in entries if e["evidence_id"] != "orphan-b") + "\n",
    )
    assert (await state.inspect_evidence(evidence))["file_index_disagreement"] == ["orphan-b"]
    # The unrelated orphan does not globally fence another original.
    assert await check_evidence(
        evidence,
        run_id=RUN,
        engagement_id=ENG,
        evidence_ids=["evidence-a"],
    )
    with pytest.raises(StateConflict, match="file_index_disagreement"):
        await check_evidence(evidence, run_id=RUN, engagement_id=ENG, evidence_ids=["orphan-b"])
    run_file = tmp_path / "evidence" / f"{RUN}.jsonl"
    records = [json.loads(line) for line in run_file.read_text().splitlines()]
    records[0]["summary"] = "Tampered"
    run_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    report = await state.inspect_evidence(evidence)
    assert report["changed"] == ["evidence-a"]
    run_file.write_text("")
    report = await state.inspect_evidence(evidence)
    assert report["missing"] == ["evidence-a"]


async def test_invalid_schema_path_binding_and_awaiting_callbacks_are_refused(tmp_path):
    state = await world(tmp_path)
    with pytest.raises(StateConflict, match="binding_changed"):
        await state.bind_run(
            MISSION, scope=EngagementScope(allowed_hosts=["other"]), binding_hash=BINDING
        )

    async def bad(tx):
        await asyncio.sleep(0)

    with pytest.raises(TypeError, match="synchronous"):
        await state.transaction(bad)
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE state_schema SET version=999")
    with pytest.raises(StateConflict, match="schema_version"):
        await state.setup()
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE legacy_tasks (identity TEXT)")
        db.execute("INSERT INTO legacy_tasks VALUES('untouched')")
    with pytest.raises(StateConflict, match="noncentral_storage"):
        await CentralStateStore(legacy, run_id=RUN, engagement_id=ENG).setup()
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT identity FROM legacy_tasks").fetchone()[0] == "untouched"
