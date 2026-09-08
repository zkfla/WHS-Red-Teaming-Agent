import pytest

from redteam_core.knowledge import CoreSession
from redteam_core.models.runtime import RuntimeState
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    CheckpointEnvelope,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from tests.helpers import catalog
from tests.performance.test_oracles import NOW


@pytest.mark.parametrize(
    "old_version", ["0.1.0", "0.1.1", "0.2.0", "0.2.1", "0.2.2", "0.3.0", "0.4.0"]
)
def test_corrected_runtime_rejects_old_semantic_runtime_checkpoint(tmp_path, old_version):
    cat = catalog()
    clock = FixedClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="compat", clock=clock)
    checkpoint = AtomicCheckpointStore(tmp_path / "checkpoint.json").write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("compat"),
        catalog=cat,
        composition_fingerprint=build_runtime_composition_fingerprint(catalog=cat),
        journal=journal,
        clock=clock,
        reason="compatibility-test",
    )
    assert checkpoint.core_runtime_version == "0.4.1"
    payload = checkpoint.model_dump(mode="python")
    payload["core_runtime_version"] = old_version
    with pytest.raises(ValueError, match="unsupported core runtime version"):
        CheckpointEnvelope.model_validate(payload)
