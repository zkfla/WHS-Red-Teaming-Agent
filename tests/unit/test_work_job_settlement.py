"""Work ledger job settlement plus pending-refresh and stale-replace (new)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from redteam.core.ids import AgentId, GraphNodeId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore

RUN = "run-job-1"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _item(
    work_id,
    candidate=None,
    path_id=None,
    path_version=None,
    path_step=None,
    hypothesis=None,
    job=None,
    run_id=RUN,
):
    return WorkItem(
        work_id=work_id,
        run_id=RunId(run_id),
        candidate_key=candidate or ("candidate:" + work_id),
        agent_id=AgentId("agent"),
        target_node_ids=[GraphNodeId("host:a.local")],
        priority=PriorityScore(total=1.0),
        created_at=NOW,
        updated_at=NOW,
        path_id=path_id,
        path_version=path_version,
        path_step_id=path_step,
        hypothesis_id=hypothesis,
        job_id=job,
    )


async def _leased_with_job(store, work_id, job_id, owner="worker", now=FUTURE):
    assert await store.add(_item(work_id))
    lease = await store.acquire(work_id, owner=owner, now=now, ttl=timedelta(hours=1))
    assert lease is not None
    assert await store.bind_job(work_id, owner=owner, job_id=job_id) is True


async def test_reconcile_accepts_only_terminal_and_checks_identity(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    await _leased_with_job(store, "w1", "job-1")
    assert await store.reconcile_job("w1", job_id="job-1", status="running", now=FUTURE) is False
    assert await store.reconcile_job("w1", job_id="job-1", status="blocked", now=FUTURE) is False
    assert await store.reconcile_job("w1", job_id="job-1", status="stale", now=FUTURE) is False
    assert await store.reconcile_job("w1", job_id="job-1", status="nope", now=FUTURE) is False
    assert (
        await store.reconcile_job("missing", job_id="job-1", status="succeeded", now=FUTURE)
        is False
    )
    assert (
        await store.reconcile_job("w1", job_id="job-other", status="succeeded", now=FUTURE) is False
    )
    assert await store.reconcile_job("w1", job_id="job-1", status="succeeded", now=FUTURE) is True
    stored = await store.get("w1")
    assert stored is not None and stored.status is WorkStatus.SUCCEEDED
    assert stored.job_id == "job-1"
    assert stored.outcome_reason is not None and "job_reconciled:succeeded" in stored.outcome_reason


async def test_reconcile_idempotent_when_already_desired_terminal(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    await _leased_with_job(store, "w1", "job-1")
    assert await store.reconcile_job("w1", job_id="job-1", status="cancelled", now=FUTURE) is True
    first = await store.get("w1")
    assert first is not None and first.status is WorkStatus.CANCELLED
    assert await store.reconcile_job("w1", job_id="job-1", status="cancelled", now=FUTURE) is True
    second = await store.get("w1")
    assert second is not None and second.status is WorkStatus.CANCELLED
    assert second.job_id == "job-1"
    assert second.outcome_reason == first.outcome_reason
    assert second.outcome_reason.count("job_reconciled:cancelled") == 1


async def test_failed_stale_recovery_allowed_and_clears_lease(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    await _leased_with_job(store, "w-failed", "job-9")
    assert (
        await store.complete(
            "w-failed", owner="worker", status=WorkStatus.FAILED, now=FUTURE, reason="tool-boom"
        )
        is True
    )
    assert (
        await store.reconcile_job("w-failed", job_id="job-9", status="succeeded", now=FUTURE)
        is True
    )
    stored = await store.get("w-failed")
    assert stored is not None and stored.status is WorkStatus.SUCCEEDED
    assert stored.job_id == "job-9"
    assert "job_reconciled:succeeded" in (stored.outcome_reason or "")
    assert "tool-boom" in (stored.outcome_reason or "")
    stale_store = SQLiteWorkStore(tmp_path / "work2.sqlite3")
    assert await stale_store.add(_item("w-s", job="job-11"))
    assert (
        await stale_store.acquire("w-s", owner="worker", now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
    assert await stale_store.bind_job("w-s", owner="worker", job_id="job-11") is True
    assert await stale_store.reclaim_expired(now=FUTURE + timedelta(hours=2)) == ["w-s"]
    mid = await stale_store.get("w-s")
    assert mid is not None and mid.status is WorkStatus.STALE
    assert mid.job_id == "job-11"
    assert (
        await stale_store.reconcile_job("w-s", job_id="job-11", status="failed", now=FUTURE) is True
    )
    assert (await stale_store.get("w-s")).status is WorkStatus.FAILED


async def test_bind_rejects_expired_lease(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    assert await store.add(_item("w-exp"))
    assert (
        await store.acquire("w-exp", owner="worker", now=PAST, ttl=timedelta(seconds=1)) is not None
    )
    assert await store.bind_job("w-exp", owner="worker", job_id="job-x") is False
    assert (await store.get("w-exp")).job_id is None


async def test_pending_candidate_refresh_updates_path_fields_without_new_row(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    first = _item(
        "w-old",
        candidate="cand-refresh",
        path_id="path-old",
        path_version=1,
        path_step="s-old",
        hypothesis="hyp-keep",
        job="job-keep",
    )
    assert await store.add(first) is True
    second = _item(
        "w-new",
        candidate="cand-refresh",
        path_id="path-new",
        path_version=2,
        path_step="s-new",
        hypothesis="hyp-new",
        job="job-new",
    )
    assert await store.add(second) is False
    items = await store.list_items(run_id=RunId(RUN))
    assert len(items) == 1
    stored = await store.get("w-old")
    assert stored is not None
    assert stored.path_id == "path-new"
    assert stored.path_version == 2
    assert stored.path_step_id == "s-new"
    assert stored.work_id == "w-old"
    assert stored.created_at == first.created_at
    assert stored.attempt == 0
    assert stored.hypothesis_id is not None and str(stored.hypothesis_id) != ""
    assert str(stored.hypothesis_id) == "hyp-keep"
    assert stored.job_id == "job-keep"
    assert await store.get("w-new") is None
    assert (
        await store.add(
            _item("w-leased", candidate="cand-leased", path_id="p1", path_version=1, path_step="s1")
        )
        is True
    )
    assert (
        await store.acquire("w-leased", owner="worker", now=FUTURE, ttl=timedelta(hours=1))
        is not None
    )
    assert (
        await store.add(
            _item(
                "w-leased-new",
                candidate="cand-leased",
                path_id="p2",
                path_version=2,
                path_step="s2",
            )
        )
        is False
    )
    kept = await store.get("w-leased")
    assert kept is not None and kept.path_id == "p1"


async def test_pending_refresh_takes_new_hypothesis_when_missing(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    assert (
        await store.add(
            _item(
                "w-h-old",
                candidate="cand-h",
                path_id="p-old",
                path_version=1,
                path_step="s-old",
                hypothesis=None,
            )
        )
        is True
    )
    assert (
        await store.add(
            _item(
                "w-h-new",
                candidate="cand-h",
                path_id="p-new",
                path_version=2,
                path_step="s-new",
                hypothesis="hyp-new",
            )
        )
        is False
    )
    stored = await store.get("w-h-old")
    assert stored is not None
    assert str(stored.hypothesis_id) == "hyp-new"
    assert stored.path_id == "p-new"
    assert await store.get("w-h-new") is None


async def test_stale_unexecuted_does_not_block_dedup(tmp_path):
    store = SQLiteWorkStore(tmp_path / "work.sqlite3")
    assert (
        await store.add(
            _item(
                "w-first", candidate="cand-stale", path_id="path-1", path_version=1, path_step="s-1"
            )
        )
        is True
    )
    changed = await store.invalidate_pending(
        run_id=RunId(RUN), path_id="path-1", step_ids={"s-1"}, now=NOW
    )
    assert changed == ["w-first"]
    mid = await store.get("w-first")
    assert mid is not None and mid.status is WorkStatus.STALE
    assert mid.attempt == 0
    assert mid.job_id is None
    assert (
        await store.add(
            _item(
                "w-second",
                candidate="cand-stale",
                path_id="path-1",
                path_version=2,
                path_step="s-1",
            )
        )
        is True
    )
