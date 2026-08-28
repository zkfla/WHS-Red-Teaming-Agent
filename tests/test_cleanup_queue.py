import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import ProbeRegistrationError, ToolProtocolError
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionProposal,
    Capability,
    CleanupStatus,
    RiskLevel,
    ToolBackend,
)
from whs_redteam.orchestration.adapters import LocalProbeAdapter, ProbeContext, ProbeRegistry
from whs_redteam.orchestration.cleanup import CleanupExecutor, CleanupQueue
from whs_redteam.orchestration.dispatcher import ActionDispatcher
from whs_redteam.orchestration.queue import ActionQueue, QueueExecutor
from whs_redteam.orchestration.retry import RetryPolicy
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.scope import ScopeGuard


class StatefulProbe:
    capability = Capability.CSRF_SAFE

    def __init__(self) -> None:
        self.calls = 0
        self.return_cleanup_arguments = True
        self.failure: Exception | None = None

    async def execute(self, proposal, context):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        output = {"changed": True}
        if self.return_cleanup_arguments:
            output["_cleanup_arguments"] = {"resource_id": "synthetic-123"}
        return output


def make_components(tmp_path: Path, *, with_cleanup: bool = True):
    config = EngagementConfig(
        name="cleanup-test",
        targets=("https://example.test",),
        allowed_methods=frozenset({"GET", "POST"}),
        allow_state_change=True,
        enabled_capabilities=frozenset({"csrf_safe"}),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    store = InventoryStore(config.inventory_db_path)
    logger = AuditLogger(config.audit_log_path)
    registry = ProbeRegistry()
    probe = StatefulProbe()
    registry.register(probe)
    cleaned: list[str] = []

    async def cleanup(arguments, context):
        cleaned.append(str(arguments["resource_id"]))
        return {"removed": True}

    if with_cleanup:
        registry.register_cleanup(Capability.CSRF_SAFE, cleanup)
    adapter = LocalProbeAdapter(registry)
    dispatcher = ActionDispatcher(
        CapabilityPolicy(config, ScopeGuard(config, logger)),
        registry,
        store,
        logger,
        tool_adapter=adapter,
    )
    return store, logger, probe, cleaned, adapter, dispatcher


def stateful_proposal() -> ActionProposal:
    return ActionProposal(
        capability=Capability.CSRF_SAFE,
        target_url="https://example.test/cart",
        method="POST",
        risk_level=RiskLevel.STATE_CHANGING,
        reason="synthetic state-change fixture",
    )


def test_state_change_enqueues_and_executes_persistent_cleanup(tmp_path: Path) -> None:
    store, logger, probe, cleaned, adapter, dispatcher = make_components(tmp_path)
    queue = CleanupQueue(store)
    try:
        result = asyncio.run(
            dispatcher.dispatch(
                stateful_proposal(),
                run_id="run-1",
                queue_id="queue-1",
                approval_verified=True,
            )
        )
        task = queue.claim_next("run-1", "cleanup-worker")
        assert task is not None
        asyncio.run(
            CleanupExecutor(
                queue,
                adapter,
                ProbeContext("run-1", store, logger),
            ).execute(task)
        )
        persisted = queue.get(task.cleanup_id)
    finally:
        store.close()

    assert result.output == {"changed": True}
    assert probe.calls == 1
    assert cleaned == ["synthetic-123"]
    assert persisted is not None
    assert persisted.status is CleanupStatus.SUCCEEDED


def test_cleanup_handler_is_required_before_state_change_executes(tmp_path: Path) -> None:
    store, _, probe, _, _, dispatcher = make_components(
        tmp_path, with_cleanup=False
    )
    try:
        with pytest.raises(ProbeRegistrationError, match="no cleanup handler"):
            asyncio.run(
                dispatcher.dispatch(
                    stateful_proposal(),
                    run_id="run-2",
                    approval_verified=True,
                )
            )
    finally:
        store.close()
    assert probe.calls == 0


def test_failed_state_change_keeps_pre_execution_cleanup_reservation(
    tmp_path: Path,
) -> None:
    store, _, probe, _, _, dispatcher = make_components(tmp_path)
    probe.return_cleanup_arguments = False
    queue = CleanupQueue(store)
    try:
        with pytest.raises(ToolProtocolError, match="_cleanup_arguments"):
            asyncio.run(
                dispatcher.dispatch(
                    stateful_proposal(),
                    run_id="run-reserved",
                    approval_verified=True,
                )
            )
        incomplete = queue.incomplete("run-reserved")
    finally:
        store.close()

    assert probe.calls == 1
    assert len(incomplete) == 1
    assert incomplete[0].status is CleanupStatus.RESERVED
    assert incomplete[0].arguments == {}


def test_cleanup_queue_retries_then_fails_terminally(tmp_path: Path) -> None:
    store, _, _, _, _, _ = make_components(tmp_path)
    queue = CleanupQueue(store)
    try:
        task, _ = queue.enqueue(
            run_id="run-3",
            parent_action_id="action-3",
            parent_queue_id=None,
            capability=Capability.CSRF_SAFE,
            backend=ToolBackend.LOCAL,
            tool_name="local.csrf_safe.cleanup",
            arguments={"resource_id": "synthetic-3"},
            max_attempts=2,
        )
        first = queue.claim_next("run-3", "worker")
        assert first is not None
        retried = queue.mark_failed(first.cleanup_id, TimeoutError("temporary"))
        second = queue.claim_next("run-3", "worker")
        assert second is not None
        failed = queue.mark_failed(second.cleanup_id, RuntimeError("still broken"))
    finally:
        store.close()

    assert retried.status is CleanupStatus.PENDING
    assert failed.status is CleanupStatus.FAILED
    assert failed.attempt_count == 2
    assert task.cleanup_id == failed.cleanup_id


def test_cleanup_required_action_is_not_retried_before_reconciliation(
    tmp_path: Path,
) -> None:
    store, _, probe, _, _, dispatcher = make_components(tmp_path)
    probe.failure = TimeoutError("state may already have changed")
    action_queue = ActionQueue(store)
    cleanup_queue = CleanupQueue(store)
    try:
        queued, _ = action_queue.enqueue(
            "run-no-retry", stateful_proposal(), max_attempts=3
        )
        store.connection.execute(
            """
            UPDATE action_queue SET status = 'PENDING', approval_verified = 1
            WHERE queue_id = ?
            """,
            (queued.queue_id,),
        )
        store.connection.commit()
        claimed = action_queue.claim_next("run-no-retry", "worker")
        assert claimed is not None
        with pytest.raises(TimeoutError, match="already have changed"):
            asyncio.run(
                QueueExecutor(
                    action_queue,
                    dispatcher,
                    RetryPolicy(base_delay_seconds=0, max_delay_seconds=0),
                ).execute(claimed)
            )
        failed = action_queue.get(queued.queue_id)
        cleanup = cleanup_queue.incomplete("run-no-retry")
    finally:
        store.close()

    assert probe.calls == 1
    assert failed is not None
    assert failed.status.value == "FAILED"
    assert failed.attempt_count == 1
    assert len(cleanup) == 1
    assert cleanup[0].status is CleanupStatus.RESERVED


def test_expired_cleanup_lease_is_recovered_after_restart(tmp_path: Path) -> None:
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    store, _, _, _, _, _ = make_components(tmp_path)
    queue = CleanupQueue(store, clock=lambda: current[0])
    try:
        task, _ = queue.enqueue(
            run_id="run-lease",
            parent_action_id="action-lease",
            parent_queue_id=None,
            capability=Capability.CSRF_SAFE,
            backend=ToolBackend.LOCAL,
            tool_name="local.csrf_safe.cleanup",
            arguments={"resource_id": "synthetic-lease"},
        )
        first = queue.claim_next("run-lease", "worker-a", lease_seconds=1)
        current[0] += timedelta(seconds=2)
        recovered = queue.claim_next("run-lease", "worker-b", lease_seconds=1)
    finally:
        store.close()

    assert first is not None
    assert recovered is not None
    assert recovered.cleanup_id == task.cleanup_id
    assert recovered.attempt_count == 2
    assert recovered.worker_id == "worker-b"
