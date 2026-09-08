import pytest

from redteam_core.models.priority import PriorityState
from tests.integration.test_durable_priority import setup_priority


def test_in_memory_priority_reset_cannot_overwrite_durable_aging(tmp_path):
    coordinator, providers, _, _ = setup_priority(tmp_path)
    try:
        coordinator.select(providers[1])
        coordinator.runtime.priority = PriorityState()
        with pytest.raises(RuntimeError, match="durable priority"):
            coordinator.select(providers[1])
        restored = coordinator.orchestrator.resume(expected_run_id=coordinator.runtime.run_id)
        assert restored.runtime_state.priority.batch_sequence == 1
    finally:
        coordinator.orchestrator.close()


def test_priority_preserves_current_opaque_domain_envelopes_by_default(tmp_path):
    from redteam_core.runtime.domain_state import encode_domain_state
    from tests.unit.test_runtime_checkpoint import (
        ReferenceDomainState,
        _domain_state_catalog,
        _domain_state_registry,
    )

    cat = _domain_state_catalog()
    envelope = encode_domain_state(
        bundle_id="reference",
        state=ReferenceDomainState(value="opaque", generation=7),
        catalog=cat,
        registry=_domain_state_registry(),
    )
    coordinator, providers, _, _ = setup_priority(tmp_path, extension_catalog=cat)
    try:
        coordinator.orchestrator.checkpoint(
            coordinator.session,
            coordinator.runtime,
            reason="domain-update",
            domain_states=(envelope,),
        )
        coordinator.select(providers[1])
        restored = coordinator.orchestrator.resume(expected_run_id=coordinator.runtime.run_id)
        assert restored.domain_states == (envelope,)
    finally:
        coordinator.orchestrator.close()


def test_checkpoint_change_during_scoring_prevents_lost_update(tmp_path):
    coordinator, providers, _, _ = setup_priority(tmp_path)
    base = providers[1]

    class CheckpointChangingProvider:
        descriptor = base.descriptor

        def score(self, facts):
            coordinator.orchestrator.checkpoint(
                coordinator.session, coordinator.runtime, reason="another-host-update"
            )
            return base.score(facts)

    try:
        with pytest.raises(RuntimeError, match="checkpoint changed"):
            coordinator.select(CheckpointChangingProvider())
        assert coordinator.runtime.priority.batch_sequence == 0
        with pytest.raises(RuntimeError, match="reload"):
            coordinator.select(providers[1])
    finally:
        coordinator.orchestrator.close()
