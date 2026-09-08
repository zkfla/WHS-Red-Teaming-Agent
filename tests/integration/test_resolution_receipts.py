import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from redteam_core.investigation.resolution import ResolverOutput
from redteam_core.models.provenance import Provenance, TrustClass, TypedPayloadEnvelope
from redteam_core.models.requirements import EvidenceRequirement, Observation, ObservationValidity
from redteam_core.models.resolution_receipt import ResolutionReceipt
from redteam_core.runtime.clock import FixedClock
from redteam_core.runtime.resolution_commit import commit_resolution_receipts
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.helpers import SamplePayload
from tests.integration.test_durable_resolver_dispatch import registry
from tests.integration.test_durable_resolver_dispatch import setup as durable_setup

setup = durable_setup


async def test_response_is_durable_before_semantic_commit_and_resume_does_not_recall(setup):
    dispatcher, resolver, req, permissions = setup
    observation = await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert dispatcher.session.investigation.observations == {}
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert resumed.runtime_state.resolution_receipts[0].observation == observation
    recovered = DurableResolverDispatcher(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=dispatcher.orchestrator,
        registry=registry(resolver),
        permissions=lambda: frozenset(permissions),
    )
    result = commit_resolution_receipts(recovered, attempt_ids=("one",), expected_revision=0)
    assert result.recorded_observation_ids == (observation.observation_id,)
    assert resolver.calls == 1
    repeat = commit_resolution_receipts(
        recovered, attempt_ids=("one",), expected_revision=result.revision
    )
    assert repeat.recorded_observation_ids == ()
    assert repeat.revision == result.revision
    after = recovered.orchestrator.resume(expected_run_id=recovered.runtime.run_id)
    assert after.session.investigation.observations[observation.observation_id] == observation
    assert len(after.runtime_state.committed_resolution_hashes) == 1


async def test_permission_revocation_retains_receipt_without_commit(setup):
    dispatcher, resolver, req, permissions = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    permissions.clear()
    with pytest.raises(PermissionError):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations
    assert not dispatcher.runtime.committed_resolution_hashes
    assert len(dispatcher.runtime.resolution_receipts) == resolver.calls == 1


async def test_commit_checkpoint_failure_does_not_publish_and_receipt_survives(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    original = dispatcher.orchestrator.checkpoint

    def fail(*args, **kwargs):
        if kwargs["reason"] == "resolution-receipts-committed":
            raise OSError("checkpoint unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatcher.orchestrator, "checkpoint", fail)
    with pytest.raises(OSError):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert len(resumed.runtime_state.resolution_receipts) == 1
    assert not resumed.runtime_state.committed_resolution_hashes
    assert resolver.calls == 1


async def test_uncertain_failure_receipt_does_not_expose_adapter_error(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup

    async def fail(requirement):
        resolver.calls += 1
        raise RuntimeError("secret-canary-value")

    monkeypatch.setattr(resolver, "resolve", fail)
    with pytest.raises(RuntimeError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    receipt = dispatcher.runtime.resolution_receipts[0]
    assert "secret-canary-value" not in receipt.model_dump_json()
    assert receipt.failure is not None and not receipt.failure.retryable
    commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert receipt.failure.failure_id in dispatcher.session.investigation.resolution_failures
    assert not dispatcher.session.investigation.observations
    assert dispatcher.runtime.operational_budget.active_requests == 1


async def test_unknown_and_mutated_receipts_are_rejected(setup):
    dispatcher, _, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    with pytest.raises(ValueError, match="unknown durable"):
        commit_resolution_receipts(dispatcher, attempt_ids=("forged",), expected_revision=0)
    receipt = dispatcher.runtime.resolution_receipts[0]
    payload = receipt.model_dump(mode="python")
    payload["run_id"] = "other"
    with pytest.raises(ValueError, match="byte count|content hash"):
        ResolutionReceipt.model_validate(payload)
    receipt.requirement.reason = "mutated nested object"
    with pytest.raises(ValueError):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations


async def test_changed_requirement_and_stale_revision_do_not_publish(setup):
    dispatcher, _, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    with pytest.raises(RuntimeError, match="revision"):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=99)
    dispatcher.session.investigation.requirements[req.requirement_id].reason = "changed"
    with pytest.raises(ValueError, match="requirement changed"):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations


async def test_crash_after_durable_commit_recovers_without_duplicate_call(setup, monkeypatch):
    dispatcher, resolver, req, permissions = setup
    observation = await dispatcher.resolve(req.requirement_id, attempt_id="one")
    original = dispatcher.orchestrator.checkpoint

    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["reason"] == "resolution-receipts-committed":
            raise OSError("process lost response after durable write")
        return result

    monkeypatch.setattr(dispatcher.orchestrator, "checkpoint", fail_after_write)
    with pytest.raises(OSError):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert observation.observation_id in resumed.session.investigation.observations
    recovered = DurableResolverDispatcher(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=dispatcher.orchestrator,
        registry=registry(resolver),
        permissions=lambda: frozenset(permissions),
    )
    result = commit_resolution_receipts(
        recovered, attempt_ids=("one",), expected_revision=resumed.session.knowledge.revision
    )
    assert not result.recorded_observation_ids
    assert resolver.calls == 1


async def test_commit_marker_cannot_hide_missing_canonical_result(setup):
    dispatcher, _, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    result = commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    dispatcher.session.investigation.observations.clear()
    with pytest.raises(ValueError, match="missing from canonical"):
        commit_resolution_receipts(
            dispatcher, attempt_ids=("one",), expected_revision=result.revision
        )


async def test_expired_receipt_is_recorded_as_stale_without_refreshing_acquired_at(
    setup, monkeypatch
):
    dispatcher, resolver, req, _ = setup
    acquired = dispatcher.orchestrator.clock.now()

    async def response(requirement):
        resolver.calls += 1
        return ResolverOutput(
            payload=SamplePayload(value="observed"),
            validity=ObservationValidity(not_after=acquired + timedelta(seconds=1)),
        )

    monkeypatch.setattr(resolver, "resolve", response)
    observation = await dispatcher.resolve(req.requirement_id, attempt_id="one")
    dispatcher.orchestrator.clock = FixedClock(acquired + timedelta(seconds=2))
    commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    slot = dispatcher.session.knowledge.slots[req.requirement_id]
    assert slot.status.value == "UNKNOWN"
    assert slot.stale_observation_ids == (observation.observation_id,)
    assert slot.effective_payload_hash is None
    assert (
        dispatcher.session.investigation.observations[
            observation.observation_id
        ].provenance.acquired_at
        == acquired
    )
    assert resolver.calls == 1


async def test_rehashed_in_memory_receipt_is_not_a_durable_response(setup):
    dispatcher, _, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    receipt = dispatcher.runtime.resolution_receipts[0]
    fields = receipt.model_dump(mode="python", exclude={"receipt_hash"})
    fields["requirement"] = receipt.requirement
    fields["observation"] = receipt.observation
    fields["descriptor_hash"] = "sha256:" + "f" * 64
    forged = ResolutionReceipt.create(**fields)
    dispatcher.runtime.resolution_receipts = (forged,)
    with pytest.raises(ValueError, match="differ from durable"):
        commit_resolution_receipts(dispatcher, attempt_ids=("one",), expected_revision=0)
    assert not dispatcher.session.investigation.observations


@pytest.mark.parametrize("setup", [{"op_requests": 2}], indirect=True)
async def test_receipt_capacity_is_checked_before_another_call(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    monkeypatch.setattr("redteam_core.runtime.resolver_dispatch.MAX_RESOLUTION_RECEIPTS", 1)
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    with pytest.raises(RuntimeError, match="receipt capacity exhausted"):
        await dispatcher.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == dispatcher.runtime.usage.requests == 1


@pytest.mark.parametrize("setup", [{"op_requests": 2}], indirect=True)
async def test_receipt_byte_capacity_is_reserved_before_another_call(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    monkeypatch.setattr(
        "redteam_core.runtime.resolver_dispatch.MAX_RESOLUTION_RECEIPT_LEDGER_BYTES",
        1_048_576,
    )
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    with pytest.raises(RuntimeError, match="receipt byte capacity"):
        await dispatcher.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == dispatcher.runtime.usage.requests == 1


def test_oversized_typed_receipt_is_rejected_even_without_raw_artifact():
    payload_json = '{"value":"' + "x" * 1_100_000 + '"}'
    digest = hashlib.sha256(payload_json.encode()).hexdigest()
    envelope = TypedPayloadEnvelope(
        type_id="reference.observation.large@1",
        schema_version="1",
        canonical_payload_json=payload_json,
        content_hash=digest,
    )
    acquired = datetime(2026, 9, 8, tzinfo=UTC)
    provenance = Provenance(
        source_kind="resolver",
        producer_id="reference.resolver@1",
        trust_class=TrustClass.OBSERVED,
        acquired_at=acquired,
        content_hash=digest,
    )
    req = EvidenceRequirement.create(
        requirement_type_id="reference.requirement.large@1",
        provider_id="reference.local",
        subject_ref="large",
        reason="size boundary",
    )
    observation = Observation.create(
        requirement_id=req.requirement_id,
        resolver_id="reference.resolver@1",
        subject_ref=req.subject_ref,
        payload=envelope,
        provenance=provenance,
    )
    with pytest.raises(ValueError, match="less than or equal"):
        ResolutionReceipt.create(
            run_id="one",
            attempt_id="one",
            composition_hash="sha256:" + "a" * 64,
            limits_hash="sha256:" + "b" * 64,
            descriptor_hash="sha256:" + "c" * 64,
            requirement=req,
            observation=observation,
        )
