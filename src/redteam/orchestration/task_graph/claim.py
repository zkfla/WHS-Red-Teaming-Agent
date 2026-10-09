"""Instance-targeted task claims assembled on existing staged contracts.

Claim = owner-field read -> ``update_task`` -> ``transition_task`` inside ONE
``coordinator.change(...)`` callback, using only the existing staged specs.
No conditional kwargs or sentinels were added to any store: the callback
observes a fresh transactional view (memory: deepcopy under one lock, swap
with no await between commits; SQLite: ``BEGIN IMMEDIATE`` snapshot, single
commit or rollback), so the read-check-write is atomic and the loser rolls
back via ``StateConflict``. Callers must never call the raw store inside the
callback and must publish events only after the commit returns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from redteam.core.enums import TaskStatus
from redteam.core.ids import TaskId
from redteam.orchestration.coordination import StateConflict


def _occupant(task: Any) -> str | None:
    return getattr(task, "occupant_owner", None)


async def claim_mission_task(
    change: Any, task_id: TaskId, *, owner: str, now: datetime
) -> Any:
    """Claim a READY task (or adopt an unowned RUNNING task) for ``owner``.

    Must run inside ``coordinator.change(...)`` on the staged ``change.tasks``.
    Raises ``StateConflict`` when the task is missing, not claimable, or owned
    by another instance (role filtering is the caller's job; this helper
    enforces ownership only).
    """
    staged = await change.tasks.get_task(task_id)
    if staged is None:
        raise StateConflict("task_claim_missing")
    occupant = _occupant(staged)
    if staged.status is TaskStatus.READY:
        if occupant is not None and occupant != owner:
            raise StateConflict("task_already_claimed")
        if occupant != owner:
            await change.tasks.update_task(
                staged.model_copy(
                    update={"occupant_owner": owner, "updated_at": now}
                )
            )
        return await change.tasks.transition_task(
            task_id, TaskStatus.RUNNING, now=now
        )
    if staged.status is TaskStatus.RUNNING:
        if occupant is not None and occupant != owner:
            raise StateConflict("task_already_claimed")
        if occupant is None:
            await change.tasks.update_task(
                staged.model_copy(
                    update={"occupant_owner": owner, "updated_at": now}
                )
            )
            claimed = await change.tasks.get_task(task_id)
            return claimed if claimed is not None else staged
        return staged
    raise StateConflict("task_not_claimable")


async def release_mission_task_claim(
    change: Any, task_id: TaskId, *, owner: str, now: datetime
) -> Any | None:
    """Release ``owner``'s claim without changing task status.

    Idempotent: missing tasks, tasks owned by another instance, and already
    released tasks are returned as-is (never stolen, never failed). Must run
    inside ``coordinator.change(...)``.
    """
    staged = await change.tasks.get_task(task_id)
    if staged is None:
        return None
    if _occupant(staged) != owner:
        return staged
    await change.tasks.update_task(
        staged.model_copy(update={"occupant_owner": None, "updated_at": now})
    )
    return await change.tasks.get_task(task_id)
