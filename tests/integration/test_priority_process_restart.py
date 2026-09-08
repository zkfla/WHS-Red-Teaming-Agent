import os
import subprocess
import sys
from pathlib import Path

import pytest

from redteam_core.investigation.priority import (
    BaselinePriorityProvider,
    ExpectedUtilityPriorityProvider,
)
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.priority import PriorityCoordinator
from tests.helpers import catalog
from tests.integration.test_durable_priority import setup_priority
from tests.integration.test_durable_resolver_dispatch import FakeResolver, registry
from tests.unit.test_priority import NOW


def crash_worker(directory, cut):
    coordinator, providers, _, _ = setup_priority(Path(directory))
    original = coordinator.orchestrator.checkpoint

    def crash(*args, **kwargs):
        if cut == "after":
            original(*args, **kwargs)
        os._exit(79)

    coordinator.orchestrator.checkpoint = crash
    coordinator.select(providers[1], limit=2)
    raise AssertionError("crash cut not reached")


@pytest.mark.parametrize("cut,expected_batch", [("before", 0), ("after", 1)])
def test_actual_process_restart_preserves_exact_published_priority_history(
    tmp_path, cut, expected_batch
):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.integration.test_priority_process_restart import crash_worker; "
            "import sys; crash_worker(sys.argv[1], sys.argv[2])",
            str(tmp_path),
            cut,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 79, child.stdout + child.stderr
    resolvers = registry(FakeResolver(max_calls=20))
    providers = BaselinePriorityProvider(), ExpectedUtilityPriorityProvider()
    cat = catalog()
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=resolvers.descriptors(),
        priority_provider_ids=tuple(provider.descriptor.binding_id for provider in providers),
    )
    clock = FixedClock(NOW)
    manager = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        EventJournal(tmp_path / "events.jsonl", run_id="priority:test", clock=clock),
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=20),
    )
    try:
        recovered = manager.resume(expected_run_id="priority:test")
        runtime = recovered.runtime_state
        assert runtime.priority.batch_sequence == expected_batch
        assert (
            sum(row.selection_count for row in runtime.priority.roots.values())
            == expected_batch * 2
        )
        coordinator = PriorityCoordinator(
            session=recovered.session,
            runtime=runtime,
            orchestrator=manager,
            registry=resolvers,
            permissions=lambda: frozenset({"read"}),
        )
        coordinator.select(providers[1], limit=2)
        assert runtime.priority.batch_sequence == expected_batch + 1
        assert (
            sum(row.selection_count for row in runtime.priority.roots.values())
            == (expected_batch + 1) * 2
        )
        assert runtime.operational_budget.charged_requests == 0
    finally:
        manager.close()
