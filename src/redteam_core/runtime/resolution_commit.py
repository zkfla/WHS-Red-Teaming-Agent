"""Single-writer replay of locally persisted passive receipts, never network replay."""

from collections.abc import Callable

from redteam_core.knowledge.commit import apply_observations_atomic
from redteam_core.knowledge.recompute import SemanticRecomputer
from redteam_core.knowledge.state import CommitResult
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.resolution_batch import ResolutionBatch
from redteam_core.models.runtime import RuntimeState
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher


def commit_resolution_receipts(
    dispatcher: DurableResolverDispatcher,
    *,
    attempt_ids: tuple[str, ...],
    expected_revision: int,
    recomputers: tuple[SemanticRecomputer, ...] = (),
    batch_authorization_check: Callable[[ResolutionBatch], None] | None = None,
) -> CommitResult:
    """Only stored IDs are accepted. Validate on private snapshots, then checkpoint and publish."""
    dispatcher._check_binding()
    if len(attempt_ids) > 10_000 or len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError("receipt commit requires bounded unique attempts")
    manager = dispatcher.orchestrator
    stored = manager.checkpoint_store.load(
        catalog=manager.catalog,
        composition_fingerprint=manager.composition_fingerprint,
        journal=manager.journal,
        expected_run_id=dispatcher.runtime.run_id,
    )
    if stored.runtime_state.resolution_receipts != dispatcher.runtime.resolution_receipts:
        raise ValueError("receipts differ from durable checkpoint")
    if (
        stored.runtime_state.committed_resolution_hashes
        != dispatcher.runtime.committed_resolution_hashes
    ):
        raise ValueError("receipt commit markers differ from durable checkpoint")
    by_attempt = {r.attempt_id: r for r in stored.runtime_state.resolution_receipts}
    if set(attempt_ids) - by_attempt.keys():
        raise ValueError("unknown durable receipt attempt")
    descriptors = {d.resolver_id: d for d in dispatcher.registry.descriptors()}
    receipts = sorted(
        (by_attempt[key] for key in attempt_ids),
        key=lambda r: (
            r.requirement.requirement_id,
            (r.observation or r.failure).resolver_id,
            r.attempt_id,
        ),
    )
    staging = dispatcher.session.model_copy(deep=True)
    runtime = dispatcher.runtime.model_copy(deep=True)
    already = set(runtime.committed_resolution_hashes)
    pending = []
    for receipt in receipts:
        for batch in stored.runtime_state.resolution_batches:
            item = next((i for i in batch.items if receipt.attempt_id in i.attempt_ids), None)
            if item is None:
                continue
            if not batch.complete or receipt.receipt_hash not in item.receipt_hashes:
                raise ValueError("batch result is not settled")
            if receipt.observation is not None and item.failure_kind is not None:
                raise ValueError("late or rejected batch response cannot be committed")
            if batch_authorization_check is None:
                raise PermissionError("batch receipt requires current authorization context")
            batch_authorization_check(batch)
        if receipt.receipt_hash in already:
            persisted_result = receipt.observation or receipt.failure
            collection = (
                staging.investigation.observations
                if receipt.observation is not None
                else staging.investigation.resolution_failures
            )
            result_id = (
                receipt.observation.observation_id
                if receipt.observation is not None
                else receipt.failure.failure_id
            )
            if collection.get(result_id) != persisted_result:
                raise ValueError("committed receipt is missing from canonical state")
            continue
        result = receipt.observation or receipt.failure
        descriptor = descriptors.get(result.resolver_id)
        if descriptor is None or receipt.descriptor_hash != "sha256:" + sha256_text(
            canonical_json(descriptor.model_dump(mode="json"))
        ):
            raise ValueError("receipt resolver descriptor mismatch")
        if not set(descriptor.required_permissions) <= set(dispatcher.permissions()):
            raise PermissionError("receipt commit permission revoked")
        if (
            staging.investigation.requirements.get(receipt.requirement.requirement_id)
            != receipt.requirement
        ):
            raise ValueError("receipt canonical requirement changed")
        pending.append(receipt)
    # The existing commit boundary revalidates payload schemas, provenance and freshness.
    result = apply_observations_atomic(
        staging,
        tuple(r.observation for r in pending if r.observation is not None),
        catalog=manager.catalog,
        evaluation_time=manager.clock.now(),
        expected_revision=expected_revision,
        recomputers=recomputers,
    )
    for receipt in pending:
        if receipt.failure is not None:
            failure = receipt.failure
            existing = staging.investigation.resolution_failures.get(failure.failure_id)
            if existing is not None and existing != failure:
                raise ValueError("resolution failure identity collision")
            staging.investigation.resolution_failures[failure.failure_id] = failure
    if not pending:
        return result
    runtime.committed_resolution_hashes = tuple(sorted(already | {r.receipt_hash for r in pending}))
    runtime.revision += 1
    runtime = RuntimeState.model_validate(runtime.model_dump(mode="python"))
    try:
        manager.checkpoint(
            staging,
            runtime,
            reason="resolution-receipts-committed",
            domain_states=dispatcher.domain_states,
        )
    except BaseException:
        dispatcher._broken = True
        raise
    # No await/callback after durable publication and before local publication.
    dispatcher.session.investigation = staging.investigation
    dispatcher.session.knowledge = staging.knowledge
    dispatcher.session.semantic = staging.semantic
    dispatcher.runtime.committed_resolution_hashes = runtime.committed_resolution_hashes
    dispatcher.runtime.revision = runtime.revision
    return result
