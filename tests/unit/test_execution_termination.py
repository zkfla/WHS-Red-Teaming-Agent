"""Offline original termination proofs return slots but never authorize replay."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.convert import digest_payload
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_state import (
    check_evidence,
    execution_identity_hash,
    execution_occupies_capacity,
)
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.execution_collection import collect_execution_termination
from redteam.orchestration.execution_journal import (
    ExecutionReceiptError,
    append_execution_termination,
)
from tests.unit.test_central_state import ENG, RUN, execution, original, owner, world


async def setup_unknown(tmp_path):
    state = await world(tmp_path)
    await owner(state)
    seed = FilesystemEvidenceStore(tmp_path / "seed")
    source, = await original(seed, "proposal-a")
    reference = ExecutionReference(
        engagement_id=ENG, run_id=RUN, instance_id="instance-a", action_id="action-a",
        invocation_id="invocation-a", tool_id="tool-any", input_digest=digest_payload({}),
        provider_handle={"opaque": "original-only"},
    )
    metadata = {**source.record.metadata, "execution_reference": reference.model_dump(mode="json")}
    submission = source.record.model_copy(update={"metadata": metadata,
                                                "digest": digest_payload(metadata)})
    store = FilesystemEvidenceStore(tmp_path / "evidence")
    await store.append(submission)
    checked = await check_evidence(store, run_id=RUN, engagement_id=ENG,
                                   evidence_ids=[str(submission.evidence_id)])
    intent = execution(proposal_evidence_id=submission.evidence_id)
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=checked, now=100,
    ))
    entered = await state.transaction(lambda tx: tx.update_execution(
        intent.model_copy(update={"state": ExecutionState.RUNNING}), owner="worker-a", now=100,
    ))
    unknown = await state.transaction(lambda tx: tx.update_execution(
        entered.model_copy(update={"state": ExecutionState.CANCELLED}), owner="worker-a", now=500,
    ))
    return state, store, submission, checked[0], unknown, reference


def ceased():
    return ExecutionRecoveryAnswer(state=ExecutionState.UNKNOWN, verified=True,
                                   uncertain=True, execution_terminated=True)


async def test_termination_returns_capacity_one_but_keeps_resource_and_goal_fences(tmp_path):
    state, store, submission, checked, unknown, _ = await setup_unknown(tmp_path)
    other = execution(invocation_id="independent", resource_keys=["session-b"])
    with pytest.raises(StateConflict, match="central_tool_capacity_occupied"):
        await state.transaction(lambda tx: tx.check_execution_capacity(other, max_calls=1,
                                                                      per_target=1))
    proof = await append_execution_termination(store, submission, ceased())
    assert (await append_execution_termination(store, submission, ceased())).evidence_id == (
        proof.evidence_id
    )
    proof_checked, = await check_evidence(store, run_id=RUN, engagement_id=ENG,
                                          evidence_ids=[str(proof.evidence_id)])
    held = await state.transaction(lambda tx: tx.record_execution_termination(
        "invocation-a", submission=checked, proof=proof_checked, owner="worker-a", version=1,
    ))
    assert held.state is ExecutionState.UNKNOWN and held.resource_execution_pending
    assert held.resource_keys == unknown.resource_keys and held.result_evidence_id is None
    assert unsafe(held) and not execution_occupies_capacity(held)
    assert execution_identity_hash(held) == execution_identity_hash(unknown)
    await state.transaction(lambda tx: tx.check_execution_capacity(
        other, max_calls=1, per_target=1,
    ))
    with pytest.raises(StateConflict, match="shared_resource_execution_conflict"):
        await state.transaction(lambda tx: tx._resource_conflict(
            other.model_copy(update={"resource_keys": unknown.resource_keys}), now=9999,
        ))
    with pytest.raises(StateConflict, match="unknown_execution_cannot_be_replayed"):
        await state.transaction(lambda tx: tx.update_execution(
            held.model_copy(update={"state": ExecutionState.RUNNING}), owner="worker-a", now=500,
        ))
    reloaded = await state.get_execution("invocation-a")
    assert reloaded.termination_evidence_id == proof.evidence_id and unsafe(reloaded)


async def test_no_termination_can_be_forged_through_normal_execution_update(tmp_path):
    state, store, submission, _, unknown, _ = await setup_unknown(tmp_path)
    with pytest.raises(StateConflict, match="execution_termination_requires_verified_receipt"):
        await state.transaction(lambda tx: tx.update_execution(
            unknown.model_copy(update={"termination_evidence_id": "invented"}),
            owner="worker-a", now=500,
        ))
    with pytest.raises(ExecutionReceiptError, match="execution_termination_unverified"):
        await append_execution_termination(store, submission,
                                          ExecutionRecoveryAnswer(state=ExecutionState.UNKNOWN))
    assert execution_occupies_capacity(await state.get_execution("invocation-a"))


@pytest.mark.parametrize("state,verified", [(ExecutionState.RUNNING, True),
                                            (ExecutionState.UNKNOWN, False)])
def test_provider_termination_requires_verified_nonrunning_state(state, verified):
    with pytest.raises(ValidationError, match="termination_requires"):
        ExecutionRecoveryAnswer(state=state, verified=verified, execution_terminated=True)


async def test_collection_is_read_only_and_unverified_answer_retains_capacity(tmp_path):
    _, store, submission, _, _, reference = await setup_unknown(tmp_path)
    queries = []

    class Provider:
        answer = ExecutionRecoveryAnswer(state=ExecutionState.UNKNOWN)

        def prepare_reference(self, invocation):
            return reference

        async def query_execution(self, received):
            assert received == reference
            queries.append(received)
            return self.answer

    provider = Provider()
    deps = SimpleNamespace(execution_recovery=provider, evidence_store=store)
    assert await collect_execution_termination(deps, submission) is None
    provider.answer = ceased()
    proof = await collect_execution_termination(deps, submission)
    assert proof.metadata["receipt_kind"] == "execution_termination"
    assert "original_result" not in proof.metadata and len(queries) == 2


@pytest.mark.parametrize("corruption", ["submission_digest", "input_digest", "owner"])
async def test_termination_proof_rejects_wrong_original_or_owner(tmp_path, corruption):
    state, store, submission, checked, unknown, _ = await setup_unknown(tmp_path)
    proof = await append_execution_termination(store, submission, ceased())
    if corruption != "owner":
        # A digest-consistent forged record must still fail semantic correlation.
        payload = dict(proof.metadata)
        if corruption == "submission_digest":
            payload["submission_digest"] = "f" * 64
        else:
            payload["execution_reference"] = {
                **payload["execution_reference"], "input_digest": "f" * 64,
            }
        proof = proof.model_copy(update={
            "evidence_id": "evidence-forged", "metadata": payload,
            "digest": digest_payload(payload),
        })
        await store.append(proof)
    inspected, = await check_evidence(store, run_id=RUN, engagement_id=ENG,
                                      evidence_ids=[str(proof.evidence_id)])
    with pytest.raises((StateConflict, ExecutionReceiptError)):
        await state.transaction(lambda tx: tx.record_execution_termination(
            "invocation-a", submission=checked, proof=inspected,
            owner="foreign" if corruption == "owner" else "worker-a", version=1,
        ))
    assert await state.get_execution("invocation-a") == unknown
