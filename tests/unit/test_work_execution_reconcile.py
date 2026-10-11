"""Work ledger SYNC-execution settlement keyed by ownership/correlation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from redteam.core.ids import AgentId, AgentInstanceId, GraphNodeId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus, work_owner_for
from redteam.orchestration.work.sqlite import SQLiteWorkStore

RUN = RunId("run-exec-1")
OTHER_RUN = RunId("run-exec-other")
INSTANCE = AgentInstanceId("instance-1")
OTHER_INSTANCE = AgentInstanceId("instance-other")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _item(work_id, candidate=None, run_id=RUN, instance_id=INSTANCE):
    return WorkItem(
        work_id=work_id,
        run_id=run_id,
        instance_id=instance_id,
        candidate_key=candidate or ("candidate:" + work_id),
        agent_id=AgentId("agent"),
        target_node_ids=[GraphNodeId("host:a.local")],
        priority=PriorityScore(total=1.0),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def backend(request):
    return request.param


def _store(backend, tmp_path, name):
    if backend == "memory":
        return InMemoryWorkStore()
    return SQLiteWorkStore(tmp_path / (name + ".sqlite3"))


def _owner():
    return work_owner_for(RUN, INSTANCE)


async def test_reconcile_execution_exact_match_without_lease(backend, tmp_path):
    store = _store(backend, tmp_path, "exact")
    assert await store.add(_item("w-exec", candidate="cand-a")) is True
    assert (
        await store.reconcile_execution(
            "missing",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-a",
            status="succeeded",
            now=FUTURE,
        )
        is False
    )
    assert (
        await store.reconcile_execution(
            "w-exec",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-a",
            status="running",
            now=FUTURE,
        )
        is False
    )
    assert (
        await store.reconcile_execution(
            "w-exec",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-a",
            status="nope",
            now=FUTURE,
        )
        is False
    )
    assert (
        await store.reconcile_execution(
            "w-exec",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-a",
            status="succeeded",
            now=FUTURE,
        )
        is True
    )
    stored = await store.get("w-exec")
    assert stored is not None and stored.status is WorkStatus.SUCCEEDED
    assert stored.outcome_reason is not None
    assert "execution_reconciled:succeeded" in stored.outcome_reason


async def test_reconcile_execution_rejects_wrong_scope(backend, tmp_path):
    store = _store(backend, tmp_path, "scope")
    assert await store.add(_item("w-scope", candidate="cand-s")) is True
    assert (
        await store.reconcile_execution(
            "w-scope",
            run_id=OTHER_RUN,
            instance_id=INSTANCE,
            candidate_key="cand-s",
            status="succeeded",
            now=FUTURE,
        )
        is False
    )
    assert (
        await store.reconcile_execution(
            "w-scope",
            run_id=RUN,
            instance_id=OTHER_INSTANCE,
            candidate_key="cand-s",
            status="succeeded",
            now=FUTURE,
        )
        is False
    )
    assert (
        await store.reconcile_execution(
            "w-scope",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-other",
            status="succeeded",
            now=FUTURE,
        )
        is False
    )
    stored = await store.get("w-scope")
    assert stored is not None and stored.status is WorkStatus.PENDING
    assert (
        await store.reconcile_execution(
            "w-scope",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-s",
            status="failed",
            now=FUTURE,
        )
        is True
    )
    assert (await store.get("w-scope")).status is WorkStatus.FAILED


async def test_reconcile_execution_idempotent_and_refuses_conflicting_terminal(
    backend, tmp_path
):
    store = _store(backend, tmp_path, "terminal")
    assert await store.add(_item("w-term", candidate="cand-t")) is True
    assert (
        await store.reconcile_execution(
            "w-term",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-t",
            status="succeeded",
            now=FUTURE,
        )
        is True
    )
    first = await store.get("w-term")
    assert first is not None and first.status is WorkStatus.SUCCEEDED
    assert (
        await store.reconcile_execution(
            "w-term",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-t",
            status="succeeded",
            now=FUTURE,
        )
        is True
    )
    second = await store.get("w-term")
    assert second is not None and second.status is WorkStatus.SUCCEEDED
    assert second.outcome_reason == first.outcome_reason
    assert second.outcome_reason.count("execution_reconciled:succeeded") == 1
    assert (
        await store.reconcile_execution(
            "w-term",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-t",
            status="cancelled",
            now=FUTURE,
        )
        is False
    )
    assert (await store.get("w-term")).status is WorkStatus.SUCCEEDED


async def test_reconcile_execution_permits_failed_and_stale(backend, tmp_path):
    store = _store(backend, tmp_path, "recovery")
    assert await store.add(_item("w-failed", candidate="cand-f")) is True
    assert (
        await store.acquire("w-failed", owner=_owner(), now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
    assert (
        await store.complete(
            "w-failed", owner=_owner(), status=WorkStatus.FAILED, now=FUTURE, reason="tool-boom"
        )
        is True
    )
    assert (
        await store.reconcile_execution(
            "w-failed",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-f",
            status="succeeded",
            now=FUTURE,
        )
        is True
    )
    stored = await store.get("w-failed")
    assert stored is not None and stored.status is WorkStatus.SUCCEEDED
    assert "execution_reconciled:succeeded" in (stored.outcome_reason or "")
    assert "tool-boom" in (stored.outcome_reason or "")
    assert await store.add(_item("w-stale", candidate="cand-st")) is True
    assert (
        await store.acquire("w-stale", owner=_owner(), now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
    assert await store.reclaim_expired(now=FUTURE + timedelta(hours=2)) == ["w-stale"]
    assert (
        await store.reconcile_execution(
            "w-stale",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-st",
            status="failed",
            now=FUTURE + timedelta(hours=2),
        )
        is True
    )
    assert (await store.get("w-stale")).status is WorkStatus.FAILED


async def test_reconcile_execution_preserves_live_lease(backend, tmp_path):
    store = _store(backend, tmp_path, "live")
    assert await store.add(_item("w-live", candidate="cand-l")) is True
    assert (
        await store.acquire("w-live", owner=_owner(), now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
    assert (
        await store.reconcile_execution(
            "w-live",
            run_id=RUN,
            instance_id=INSTANCE,
            candidate_key="cand-l",
            status="succeeded",
            now=FUTURE,
        )
        is False
    )
    stored = await store.get("w-live")
    assert stored is not None and stored.status is WorkStatus.LEASED
    assert (
        await store.heartbeat("w-live", owner=_owner(), now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
