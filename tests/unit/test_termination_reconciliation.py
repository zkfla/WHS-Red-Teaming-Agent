"""End-to-end UNKNOWN termination receipt reconciliation; fixture-only provider."""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.execution import ExecutionState
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration import central_recovery
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.execution_journal import checked_receipt
from redteam.orchestration.state import RuntimeState
from tests.unit.test_central_state import ENG, RUN, execution
from tests.unit.test_execution_termination import ceased, setup_unknown


@pytest.mark.asyncio
async def test_reconcile_originals_returns_global_slot_and_reuses_proof_after_restart(
    tmp_path, monkeypatch,
):
    state, store, submission, _, original, reference = await setup_unknown(tmp_path)
    independent = execution(invocation_id="independent", resource_keys=["session-b"])
    with pytest.raises(StateConflict, match="central_tool_capacity_occupied"):
        await state.transaction(lambda tx: tx.check_execution_capacity(
            independent, max_calls=1, per_target=1,
        ))

    class Provider:
        def __init__(self):
            self.queried = []
            self.fetched = []

        def prepare_reference(self, invocation):
            assert str(invocation.invocation_id) == str(original.invocation_id)
            return reference

        async def query_execution(self, received):
            self.queried.append(received)
            assert received == reference
            return ceased()

        async def fetch_execution(self, received):
            self.fetched.append(received)
            pytest.fail("termination-only recovery must not fetch or invent a result")

    provider = Provider()

    # setup_unknown binds its real SQLite state to a fixed test sentinel, so it
    # cannot satisfy runtime_token's independent application-binding check.
    # Keep the real RuntimeState ownership fields and SQLite transaction path;
    # bypass only that unrelated binding preflight for this helper-built state.
    async def fixture_binding(_deps, _runtime, *, require_task=True, admit=True):
        return None

    monkeypatch.setattr(central_recovery, "runtime_token", fixture_binding)
    runtime = RuntimeState(
        run_id=RUN,
        engagement_id=ENG,
        mission_id="mission-central",
        instance_id="instance-a",
        task_owner="worker-a",
        task_ownership_version=1,
        active_task_id="task-a",
        current_agent="web",
    )

    def deps_for(central_state, evidence_store):
        return SimpleNamespace(
            central_state=central_state,
            evidence_store=evidence_store,
            execution_recovery=provider,
            long_job_adapter=None,
        )

    outcome = await central_recovery.reconcile_originals(
        deps_for(state, store), runtime,
    )
    assert outcome.errors == {}
    assert outcome.pending_invocations == [str(original.invocation_id)]
    assert not outcome.ready

    released = await state.get_execution(str(original.invocation_id))
    assert released.state is ExecutionState.UNKNOWN
    assert released.termination_evidence_id is not None
    assert released.result_evidence_id is None
    assert released.resource_execution_pending
    assert released.resource_keys == original.resource_keys
    assert unsafe(released)

    await state.transaction(lambda tx: tx.check_execution_capacity(
        independent, max_calls=1, per_target=1,
    ))
    with pytest.raises(StateConflict, match="shared_resource_execution_conflict"):
        await state.transaction(lambda tx: tx._resource_conflict(
            independent.model_copy(update={"resource_keys": original.resource_keys}),
            now=10_000,
        ))
    with pytest.raises(StateConflict, match="unknown_execution_cannot_be_replayed"):
        await state.transaction(lambda tx: tx.update_execution(
            released.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a",
            now=500,
        ))

    proof_id = released.termination_evidence_id
    proof = await store.get(proof_id)
    assert proof is not None
    proof_payload = checked_receipt(proof)
    assert proof_payload["receipt_kind"] == "execution_termination"
    assert proof_payload["submission_id"] == str(submission.evidence_id)
    assert proof_payload["submission_digest"] == submission.digest

    # Re-open both stores to exercise the persisted proof-ID path after restart.
    restarted_state = CentralStateStore(state.path, run_id=RUN, engagement_id=ENG)
    await restarted_state.setup()
    restarted_store = FilesystemEvidenceStore(tmp_path / "evidence")
    restarted = await restarted_state.get_execution(str(original.invocation_id))
    assert restarted.termination_evidence_id == proof_id
    assert await restarted_store.get(proof_id) is not None

    resumed = await central_recovery.reconcile_originals(
        deps_for(restarted_state, restarted_store), runtime,
    )
    assert resumed.errors == {}
    assert resumed.pending_invocations == [str(original.invocation_id)]
    after_resume = await restarted_state.get_execution(str(original.invocation_id))
    assert after_resume.termination_evidence_id == proof_id
    assert after_resume.state is ExecutionState.UNKNOWN and unsafe(after_resume)
    assert await restarted_state.transaction(lambda tx: tx.check_execution_capacity(
        independent, max_calls=1, per_target=1,
    ), write=False) is None
    terminations = [
        record for record in await restarted_store.list_for_action(submission.action_id)
        if record.metadata.get("receipt_kind") == "execution_termination"
    ]
    assert len(terminations) == 1
    assert provider.fetched == []
    assert provider.queried
    assert all(item == reference for item in provider.queried)
