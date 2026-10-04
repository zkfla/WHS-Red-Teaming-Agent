"""Deterministic candidate-to-work scheduling for sequential and Send fan-out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from redteam.core.ids import HypothesisId, RunId, new_id
from redteam.orchestration.candidates.models import ExecutableCandidate
from redteam.orchestration.work.models import TaskLease, WorkBudget, WorkItem
from redteam.orchestration.work.store import WorkStore


class WorkScheduler:
    def __init__(self, store: WorkStore) -> None:
        self._store = store

    async def enqueue(
        self,
        candidates: list[ExecutableCandidate],
        *,
        graph_revision: int,
        run_id: RunId,
        mission_task_id: str | None = None,
        now: datetime | None = None,
    ) -> list[WorkItem]:
        created_at = now or datetime.now(timezone.utc)
        enqueued: list[WorkItem] = []
        for candidate in candidates:
            draft = candidate.draft
            item = WorkItem(
                work_id=new_id("work"),
                run_id=run_id,
                mission_task_id=mission_task_id,
                delegation_id=draft.plan_step.delegation_id,
                candidate_key=candidate.candidate_key,
                execution_identity_version=draft.execution_identity_version,
                agent_id=draft.agent_id,
                graph_revision=graph_revision,
                path_id=draft.plan_step.path_id,
                path_version=draft.plan_step.path_version,
                path_step_id=draft.plan_step.path_step_id,
                target_node_ids=draft.target_node_ids,
                hypothesis_id=(
                    draft.plan_step.existing_hypothesis_id
                    or (
                        HypothesisId(new_id("hyp"))
                        if draft.plan_step.hypothesis is not None
                        else None
                    )
                ),
                prerequisite_ids=draft.prerequisite_ids,
                conflict_keys=draft.conflict_keys,
                priority=candidate.priority,
                budget=WorkBudget(),
                repeatable=candidate.plan_step.repeatable,
                created_at=created_at,
                updated_at=created_at,
            )
            if await self._store.add(item):
                enqueued.append(item)
        return enqueued

    async def lease_batch(
        self,
        *,
        run_id: RunId,
        owner: str,
        limit: int,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> list[TaskLease]:
        if limit < 1:
            raise ValueError("lease batch limit must be positive")
        timestamp = now or datetime.now(timezone.utc)
        leases: list[TaskLease] = []
        for item in await self._store.list_items(run_id=run_id):
            if len(leases) >= limit:
                break
            lease = await self._store.acquire(item.work_id, owner=owner, now=timestamp, ttl=ttl)
            if lease is not None:
                leases.append(lease)
        return leases
