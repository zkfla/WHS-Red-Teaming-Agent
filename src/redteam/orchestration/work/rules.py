"""Pure work-ledger transition rules shared by memory and SQLite stores.

Stores own persistence, lookup and concurrency only; every registration,
duplicate, lease, conflict and termination decision lives here so both
backends reach the same verdict on the same inputs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone

from redteam.orchestration.work.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    TaskLease,
    WorkItem,
    WorkStatus,
    is_unexecuted_stale,
)

ACTIVE_CONFLICT_STATUSES = frozenset({WorkStatus.LEASED, WorkStatus.RUNNING})

RECONCILABLE_STATUSES = frozenset({WorkStatus.SUCCEEDED, WorkStatus.FAILED, WorkStatus.CANCELLED})


def validate_lease_ttl(ttl: timedelta) -> None:
    if ttl.total_seconds() <= 0:
        raise ValueError("lease ttl must be positive")


def normalize_expiry(expires_at: datetime, now: datetime) -> tuple[datetime, datetime]:
    """Treat naive expiry as UTC so memory rows and SQLite ISO text agree."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at, now


def is_expired(expires_at: datetime, now: datetime) -> bool:
    expiry, current = normalize_expiry(expires_at, now)
    return expiry <= current


def lease_owned(lease: TaskLease | None, owner: str) -> bool:
    return lease is not None and lease.owner == owner


def decide_add(item: WorkItem, same_candidate: list[WorkItem]) -> tuple[str, WorkItem | None]:
    """Decide registration: ("accept"|"reject", refresh target or None).

    A pending same-candidate row wins over insertion; the caller refreshes
    it with :func:`refresh_pending_item` and reports ``False``.
    """
    if any(existing.status in ACTIVE_CONFLICT_STATUSES for existing in same_candidate):
        return ("reject", None)
    pending = [existing for existing in same_candidate if existing.status is WorkStatus.PENDING]
    if pending:
        return ("refresh", sorted(pending, key=lambda entry: entry.work_id)[0])
    blocking = [existing for existing in same_candidate if not is_unexecuted_stale(existing)]
    if blocking and not item.repeatable:
        return ("reject", None)
    return ("accept", None)


def refresh_pending_item(target: WorkItem, item: WorkItem) -> WorkItem:
    return item.model_copy(deep=True).model_copy(
        update={
            "work_id": target.work_id,
            "created_at": target.created_at,
            "attempt": target.attempt,
            "hypothesis_id": target.hypothesis_id or item.hypothesis_id,
            "job_id": target.job_id,
            "status": WorkStatus.PENDING,
            "outcome_reason": None,
        }
    )


def terminal_failure_in(prereq_states: Mapping[str, WorkStatus]) -> tuple[str, WorkStatus] | None:
    for dependency_id, status in prereq_states.items():
        if status in TERMINAL_STATUSES and status is not WorkStatus.SUCCEEDED:
            return dependency_id, status
    return None


def prereqs_succeeded(prereq_states: Mapping[str, WorkStatus | None]) -> bool:
    return all(status is WorkStatus.SUCCEEDED for status in prereq_states.values())


def blocked_for_dependency(
    item: WorkItem, dependency_id: str, status: WorkStatus, now: datetime
) -> WorkItem:
    return item.model_copy(
        update={
            "status": WorkStatus.BLOCKED,
            "updated_at": now,
            "outcome_reason": f"prerequisite_not_succeeded:{dependency_id}:{status.value}",
        }
    )


def has_conflict(item: WorkItem, others: Iterable[WorkItem]) -> bool:
    keys = set(item.conflict_keys)
    if not keys:
        return False
    return any(
        other.work_id != item.work_id
        and other.status in ACTIVE_CONFLICT_STATUSES
        and not keys.isdisjoint(other.conflict_keys)
        for other in others
    )


def new_lease(work_id: str, owner: str, now: datetime, ttl: timedelta) -> TaskLease:
    return TaskLease(
        work_id=work_id,
        owner=owner,
        issued_at=now,
        heartbeat_at=now,
        expires_at=now + ttl,
    )


def leased_item(item: WorkItem, now: datetime) -> WorkItem:
    return item.model_copy(
        update={"status": WorkStatus.LEASED, "attempt": item.attempt + 1, "updated_at": now}
    )


def running_item(item: WorkItem, now: datetime) -> WorkItem:
    return item.model_copy(update={"status": WorkStatus.RUNNING, "updated_at": now})


def renewed_lease(lease: TaskLease, now: datetime, ttl: timedelta) -> TaskLease:
    return lease.model_copy(update={"heartbeat_at": now, "expires_at": now + ttl})


def require_terminal(status: WorkStatus) -> None:
    if status not in TERMINAL_STATUSES:
        raise ValueError("complete requires a terminal status")


def stale_item(item: WorkItem, now: datetime, reason: str) -> WorkItem:
    return item.model_copy(
        update={"status": WorkStatus.STALE, "updated_at": now, "outcome_reason": reason}
    )


def finished_item(
    item: WorkItem, status: WorkStatus, now: datetime, reason: str | None
) -> WorkItem:
    return item.model_copy(update={"status": status, "updated_at": now, "outcome_reason": reason})


def pending_item(item: WorkItem, now: datetime) -> WorkItem:
    return item.model_copy(update={"status": WorkStatus.PENDING, "updated_at": now})


def failed_item(item: WorkItem, now: datetime, reason: str) -> WorkItem:
    return item.model_copy(
        update={"status": WorkStatus.FAILED, "updated_at": now, "outcome_reason": reason}
    )


def expired_lease_ids(leases: Mapping[str, datetime], now: datetime) -> list[str]:
    return sorted(work_id for work_id, expires_at in leases.items() if is_expired(expires_at, now))


def should_invalidate(item: WorkItem, run_id: object, path_id: str, step_ids: set[str]) -> bool:
    return (
        item.run_id == run_id
        and item.path_id == path_id
        and item.path_step_id in step_ids
        and item.status is WorkStatus.PENDING
    )


def abandon_candidate(item: WorkItem, run_id: object, owner: str, lease_owner: str | None) -> bool:
    if item.run_id != run_id or item.status not in ACTIVE_STATUSES:
        return False
    return lease_owner is None or lease_owner == owner


def can_bind(
    lease: TaskLease | None,
    item: WorkItem | None,
    *,
    owner: str,
    job_id: str,
    now: datetime,
) -> bool:
    if lease is None or item is None or lease.owner != owner:
        return False
    if is_expired(lease.expires_at, now):
        return False
    return not (item.job_id and item.job_id != job_id)


def reconcile_reason(previous: str | None, target: WorkStatus) -> str:
    marker = f"job_reconciled:{target.value}"
    return marker if not previous else f"{marker}:{previous}"


def decide_reconcile(
    item: WorkItem | None, job_id: str, status: str, now: datetime
) -> tuple[bool, WorkItem | None]:
    """Decide job settlement: (applied, updated item or None).

    ``updated`` is ``None`` when nothing changes but the lease is dropped
    (the already-reconciled repeat), or when the request is rejected.
    """
    try:
        target = WorkStatus(status)
    except (ValueError, TypeError):
        return (False, None)
    if target not in RECONCILABLE_STATUSES:
        return (False, None)
    if item is None or not item.job_id or item.job_id != job_id:
        return (False, None)
    if item.status is target:
        if item.outcome_reason and "job_reconciled" in item.outcome_reason:
            return (True, None)
        reason = reconcile_reason(item.outcome_reason, target)
        return (True, finished_item(item, target, now, reason))
    if item.status in {WorkStatus.FAILED, WorkStatus.STALE}:
        reason = reconcile_reason(item.outcome_reason, target)
        return (True, finished_item(item, target, now, reason))
    if item.status not in ACTIVE_STATUSES:
        return (False, None)
    reason = reconcile_reason(item.outcome_reason, target)
    return (True, finished_item(item, target, now, reason))
