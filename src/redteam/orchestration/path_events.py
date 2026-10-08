"""Path/work handoff: state + event commit, idempotent work invalidation.

Uses the task/path coordinator's existing outbox, not a second queue or DB.
The work ledger lives in its own transaction: a cursor is acknowledged only
after its safe invalidation effect, and recovery repeats that effect if needed.
"""

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ids import RunId
from redteam.orchestration.coordination import ChangeSet, Coordinator
from redteam.orchestration.outbox import consume_pending
from redteam.orchestration.paths.models import PathPlan
from redteam.orchestration.work.store import WorkStore

PATH_ACCEPTED = "PathAccepted"
PATH_STEP_SETTLED = "PathStepSettled"


def affected_steps(current: PathPlan, previous: PathPlan | None) -> list[str]:
    return (
        [s.step_id for s in previous.steps]
        if previous and previous.path_id != current.path_id
        else current.changed_step_ids
    )


def queue_path_accepted(
    change: ChangeSet,
    publisher,
    current: PathPlan,
    previous: PathPlan | None,
) -> None:
    previous_id = previous.path_id if previous else None
    change.queue(
        publisher.build(
            PATH_ACCEPTED,
            {
                "schema_version": "1",
                "path_id": current.path_id,
                "version": current.version,
                "previous_path_id": previous_id,
                "affected_step_ids": affected_steps(current, previous),
                "dedup_key": f"path:{current.path_id}:{current.version}",
            },
        )
    )


def queue_step_settled(change: ChangeSet, publisher, path: PathPlan, step_id: str) -> None:
    step = next(s for s in path.steps if s.step_id == step_id)
    change.queue(
        publisher.build(
            PATH_STEP_SETTLED,
            {
                "schema_version": "1",
                "path_id": path.path_id,
                "version": path.version,
                "step_id": step_id,
                "status": step.status,
                "outcome": step.outcome,
                "evidence_ids": step.evidence_ids,
                "dedup_key": f"step:{path.path_id}:{path.version}:{step_id}:{step.status}",
            },
        )
    )


async def reconcile_path_events(
    coordinator: Coordinator,
    work_store: WorkStore,
    *,
    run_id: str,
    engagement_id: str,
) -> None:
    async def apply(event: EventEnvelope) -> None:
        payload = event.payload
        if payload.get("schema_version") != "1":
            raise ValueError("path_event_schema_unsupported")
        previous = payload.get("previous_path_id")
        affected = payload.get("affected_step_ids")
        if not isinstance(affected, list) or any(not isinstance(s, str) for s in affected):
            raise ValueError("path_event_invalid_steps")
        if previous is not None:
            if not isinstance(previous, str) or not previous:
                raise ValueError("path_event_invalid_previous_path")
            await work_store.invalidate_pending(
                run_id=RunId(run_id),
                path_id=previous,
                step_ids=set(affected),
                now=datetime.now(timezone.utc),
            )

    await consume_pending(
        coordinator,
        run_id=run_id,
        engagement_id=engagement_id,
        consumer_id="path-work-invalidation:v1",
        event_types=frozenset({PATH_ACCEPTED}),
        handler=apply,
    )
