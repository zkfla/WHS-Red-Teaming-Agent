"""Atomic orchestration changes; external I/O is always outside this boundary.

Callbacks operate exclusively on detached memory stores. SQLite runs the same
calculation on its owning thread and commits task, path and internal records
with one connection. The intent history is a recoverable audit projection.
"""

from __future__ import annotations

import copy
import hashlib
import json
import string
from collections.abc import Callable, Coroutine, Hashable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.paths.models import PathPlan, PathVersionConflict

if TYPE_CHECKING:
    from redteam.orchestration.diagnostics import DecisionDiagnostic

T = TypeVar("T")
CLOSED = frozenset({MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED})
BLOCKING_INTENTS = frozenset({"stop", "pause", "resume", "focus", "reprioritize", "exclude"})


class CoordinationError(ValueError):
    """Safe, code-owned orchestration rejection."""


class StateConflict(PathVersionConflict):
    """A reviewed snapshot no longer describes the canonical state."""


class PlanningInputChangeCode(str, Enum):
    """Bounded causes emitted by the planning snapshot validators."""

    GOAL_OR_BINDING_CHANGED = "planning_goal_or_binding_changed"
    CANDIDATE_GRAPH_CHANGED = "candidate_related_graph_changed"
    PROBE_RESULT_CHANGED = "probe_related_result_changed"
    CONTEXT_EDGE_CHANGED = "planner_context_edge_changed"
    CONTEXT_EXECUTION_CHANGED = "planner_context_execution_changed"
    TERMINAL_INVENTORY_CHANGED = "planning_terminal_inventory_changed"
    EXISTING_WORK_CHANGED = "planner_existing_work_changed"


_PLANNING_INPUT_CHANGE_CODES = frozenset(item.value for item in PlanningInputChangeCode)
_UNKNOWN_PLANNING_INPUT_CHANGE = "planning_inputs_changed"


class PlanningInputsChanged(StateConflict):
    """A related planner input changed after its captured decision snapshot."""

    def __init__(self, code: str) -> None:
        # Preserve only owner-defined causes. Unknown strings may contain arbitrary data.
        self.code = (
            code if isinstance(code, str) and code in _PLANNING_INPUT_CHANGE_CODES
            else _UNKNOWN_PLANNING_INPUT_CHANGE
        )
        super().__init__(self.code)


class CandidateAdmissionConflict(StateConflict):
    """Candidate or probe admission rejected with bounded owner-produced diagnostics.

    The message stays the fixed branch code for existing callers. The first diagnostic is
    the primary owner diagnostic; later entries are secondary. At most eight are retained.
    """

    def __init__(self, code: str, diagnostics: Sequence[DecisionDiagnostic] = ()) -> None:
        self.diagnostics: tuple[DecisionDiagnostic, ...] = tuple(
            _identifier_projection(item) for item in tuple(diagnostics)[:8]
        )
        super().__init__(code)

    @property
    def diagnostic(self) -> DecisionDiagnostic | None:
        return self.diagnostics[0] if self.diagnostics else None


SAFE_IDENTIFIER_MAX = 100
SAFE_IDENTIFIER_LIMIT = 10
_SAFE_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "_-")


def safe_identifier(value: object) -> str | None:
    """Owner identifier shape: 1-100 ASCII letters, digits, underscore or hyphen.

    Anything else (free text, secrets, oversize values) is not an identifier and
    returns None; callers drop it rather than substituting a placeholder.
    """
    text = "" if value is None else str(value)
    if 0 < len(text) <= SAFE_IDENTIFIER_MAX and set(text) <= _SAFE_IDENTIFIER_CHARS:
        return text
    return None


def safe_identifiers(
    values: Iterable[object], *, limit: int = SAFE_IDENTIFIER_LIMIT,
) -> list[str]:
    """Order-preserving, deduplicated, bounded identifiers; unsafe entries are dropped."""
    kept: list[str] = []
    for value in values:
        text = safe_identifier(value)
        if text is not None and text not in kept:
            kept.append(text)
            if len(kept) >= limit:
                break
    return kept


def _identifier_projection(item: DecisionDiagnostic) -> DecisionDiagnostic:
    """Copy a diagnostic and keep only safe identifiers in its ID projections."""
    copied = copy.deepcopy(item)
    return copied.model_copy(update={
        "evidence_ids": safe_identifiers(copied.evidence_ids),
        "criterion_ids": safe_identifiers(copied.criterion_ids),
    })


def content_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AtomicStateBackend(Protocol):
    """Public change session owned by one composite state provider.

    The coordinator only consumes this surface; storage, concurrency and
    records persistence stay inside the provider.

    - ``change`` is the single atomic boundary: the provider stages
      Task/Path/records, runs ``callback`` on detached memory views, then
      commits everything together or rolls everything back. Partial
      application is a failure. The callback must not perform outside I/O
      (LLM, tools, publishing); only detached validation and mutation.
    - ``atomic_scope`` is a diagnostic identity (memory/SQLite), optional
      for custom providers.
    - ``attach_paths`` binds a path store once; rebinding or a mismatched
      store is rejected loudly.
    - ``check_history`` rejects an operator-intent history that does not
      pair with the provider (``None`` history always passes).
    """

    async def change(self, callback: Callable[[Any], Coroutine[Any, Any, T]]) -> T: ...

    def atomic_scope(self) -> Hashable: ...

    def attach_paths(self, paths: Any) -> None: ...

    def check_history(self, history: Any | None) -> None: ...


@runtime_checkable
class RunExecutionBackend(Protocol):
    """Optional provider-owned, nonblocking exclusive Run execution scope.

    This is process ownership, not a Task claim, tool-resource lease or
    database transaction. Release never authorizes checkpoint replay.
    Custom backends must provide equivalent ownership for specialist mode.
    """

    def exclusive_run(self, run_id: str) -> AbstractAsyncContextManager[None]: ...


class StagedTaskGraph(Protocol):
    """Staged task view a change callback observes.

    Reads are public; the only bounded write is the task snapshot/restore
    pair that rolls one intent application back to its pre-mutation
    state. There is no general staged rollback primitive.
    """

    def peek_mission(self, mission_id: MissionId) -> MissionPlan | None: ...
    def peek_tasks_of_mission(self, mission_id: MissionId) -> list[MissionTask]: ...
    def snapshot_tasks(
        self,
    ) -> tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]]: ...
    def restore_tasks(
        self,
        snapshot: tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]],
    ) -> None: ...
    async def create_mission(self, mission: MissionPlan) -> None: ...
    async def get_mission(self, mission_id: MissionId) -> MissionPlan | None: ...
    async def list_missions(
        self, *, engagement_id: EngagementId | None = None
    ) -> list[MissionPlan]: ...
    async def update_mission(self, mission: MissionPlan) -> None: ...
    async def create_task(self, task: MissionTask) -> None: ...
    async def get_task(self, task_id: TaskId) -> MissionTask | None: ...
    async def list_tasks(
        self, *, mission_id: MissionId | None = None, status: TaskStatus | None = None
    ) -> list[MissionTask]: ...
    async def update_task(self, task: MissionTask) -> None: ...
    async def transition_task(
        self, task_id: TaskId, status: TaskStatus, *, now: datetime, reason: str | None = None
    ) -> MissionTask: ...
    async def add_dependency(
        self, task_id: TaskId, dependency_id: TaskId, *, now: datetime
    ) -> MissionTask: ...
    async def update_priority(
        self, task_id: TaskId, priority: int, *, now: datetime
    ) -> MissionTask: ...
    async def block_task(self, task_id: TaskId, reason: str, *, now: datetime) -> MissionTask: ...
    async def unblock_task(self, task_id: TaskId, *, now: datetime) -> MissionTask: ...
    async def split_task(
        self, parent_id: TaskId, children: list[MissionTask]
    ) -> list[MissionTask]: ...


class StagedPaths(Protocol):
    """Staged path view a change callback observes (public reads only)."""

    def peek_active_pointer(self, run_id: str) -> tuple[str, int] | None: ...
    def peek_snapshot(self, run_id: str, path_id: str, version: int) -> PathPlan | None: ...
    def merged_view(self, snapshot: PathPlan) -> PathPlan: ...
    async def get_active(self, run_id: str) -> PathPlan | None: ...
    async def list_paths(self, run_id: str) -> list[PathPlan]: ...
    async def register(
        self,
        path: PathPlan,
        expected_version: int | None = None,
        *,
        expected_path_id: str | None = None,
    ) -> PathPlan: ...
    async def update_step(
        self,
        run_id: str,
        path_id: str,
        version: int,
        step_id: str,
        status: str,
        outcome: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> PathPlan | None: ...


@dataclass
class ChangeSet:
    tasks: StagedTaskGraph
    paths: StagedPaths
    records: dict[tuple[str, str], Any] = field(default_factory=dict)

    def binding(self, run_id: str, mission_id: str, engagement_id: str | None = None) -> dict:
        bound = self.records.get(("binding", run_id))
        if bound is None or bound["mission_id"] != mission_id:
            raise CoordinationError("run_binding_missing_or_mismatched")
        if engagement_id is not None and bound["engagement_id"] != engagement_id:
            raise CoordinationError("engagement_binding_mismatched")
        return bound

    def legacy_scope(self, run_id: str) -> None:
        if ("binding", run_id) in self.records:
            raise CoordinationError("bound_run_requires_mission_identity")

    def generation(self, run_id: str, mission_id: str) -> int:
        return self.records.get(("generation", f"{run_id}:{mission_id}"), 0)

    def pending(self, run_id: str, mission_id: str) -> bool:
        return any(
            kind == "intent"
            and item["run_id"] == run_id
            and item["intent"]["mission_id"] == mission_id
            and item["status"] == "received"
            and item["intent"]["intent_type"] in BLOCKING_INTENTS
            for (kind, _), item in self.records.items()
        )

    def token(self, run_id: str, mission_id: str) -> str:
        mission = self.tasks.peek_mission(MissionId(mission_id))
        pointer = self.paths.peek_active_pointer(run_id)
        path = self.paths.peek_snapshot(run_id, *pointer) if pointer else None
        if path is not None:
            path = self.paths.merged_view(path)
        tasks = sorted(
            (
                item.model_dump(mode="json")
                for item in self.tasks.peek_tasks_of_mission(MissionId(mission_id))
            ),
            key=lambda item: item["task_id"],
        )
        return content_hash(
            {
                "mission": mission.model_dump(mode="json") if mission else None,
                "binding": self.records.get(("binding", run_id)),
                "path": path.model_dump(mode="json") if path else None,
                "tasks": tasks,
                "generation": self.generation(run_id, mission_id),
            }
        )

    def admit(self, run_id: str, mission_id: str, *, token: str | None = None) -> None:
        bound = self.binding(run_id, mission_id)
        mission = self.tasks.peek_mission(MissionId(mission_id))
        if bound["closed"] or mission is None or mission.status is not MissionStatus.ACTIVE:
            raise CoordinationError("mission_not_active")
        if self.pending(run_id, mission_id):
            raise StateConflict("pending_operator_mutation")
        if token is not None and self.token(run_id, mission_id) != token:
            raise StateConflict("reviewed_state_changed")

    def queue(self, envelope: Any) -> None:
        key = ("outbox", str(envelope.event_id))
        if key in self.records:
            return
        counter_key = ("outbox_sequence", str(envelope.run_id))
        sequence = self.records.get(counter_key, 0) + 1
        self.records[counter_key] = sequence
        self.records[key] = {
            "sequence": sequence,
            "envelope": envelope.model_dump(mode="json"),
            "delivered": False,
            "owner": None,
            "lease_until": 0.0,
        }


class Coordinator:
    """Thin facade: consumes only the backend change session plus bind/validate."""

    def __init__(self, backend: AtomicStateBackend) -> None:
        self.backend = backend

    def attach_paths(self, paths: Any) -> None:
        self.backend.attach_paths(paths)

    async def change(self, callback: Callable[[ChangeSet], Coroutine[Any, Any, T]]) -> T:
        return await self.backend.change(callback)

    def exclusive_run(self, run_id: str) -> AbstractAsyncContextManager[None]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise CoordinationError("run_execution_identity_missing")
        if not isinstance(self.backend, RunExecutionBackend):
            raise CoordinationError("run_execution_backend_unsupported")
        return self.backend.exclusive_run(run_id)

    async def bind(self, run_id: str, mission_id: str, engagement_id: str) -> None:
        async def register(change: ChangeSet) -> None:
            mission = change.tasks.peek_mission(MissionId(mission_id))
            if mission is None or str(mission.engagement_id) != engagement_id:
                raise CoordinationError("run_binding_mismatched")
            existing = change.records.get(("binding", run_id))
            expected = {
                "mission_id": mission_id,
                "engagement_id": engagement_id,
                "closed": mission.status in CLOSED,
            }
            if existing is not None:
                if (
                    existing["mission_id"] != mission_id
                    or existing["engagement_id"] != engagement_id
                ):
                    raise CoordinationError("run_binding_mismatched")
                return
            if any(
                kind == "binding" and bound["mission_id"] == mission_id
                for (kind, _), bound in change.records.items()
            ):
                raise CoordinationError("mission_already_bound_to_another_run")
            change.records[("binding", run_id)] = expected

        await self.change(register)

    async def review_token(self, run_id: str, mission_id: str) -> str:
        async def capture(change: ChangeSet) -> str:
            change.admit(run_id, mission_id)
            return change.token(run_id, mission_id)

        return await self.change(capture)


def coordinator_for(
    tasks: Any, paths: Any | None = None, *, backend: AtomicStateBackend | None = None
) -> Coordinator:
    """Reuse one coordinator per task store; the provider key lives in a registry.

    ``backend`` lets the assembler inject a ready provider (custom or
    builtin) instead of selecting one from ``tasks``/``paths``. The
    injected backend must own ``tasks`` (and ``paths`` when given); it is
    registered under ``tasks`` so later ``coordinator_for`` calls reuse the
    same coordinator. Injecting a *different* backend for an already
    registered store is rejected loudly.
    """
    # The compatibility owner follows the store's lifetime, without a global
    # strong root. Custom/slotted stores can instead own Coordinator in RuntimeDeps.
    coordinator = getattr(tasks, "_state_coordinator", None)
    if coordinator is None:
        from redteam.orchestration.state_backend import state_backend_for

        coordinator = Coordinator(
            backend if backend is not None else state_backend_for(tasks, paths)
        )
        tasks._state_coordinator = coordinator
    elif backend is not None and coordinator.backend is not backend:
        raise CoordinationError("atomic_state_backend_mismatched")
    if paths is not None:
        coordinator.attach_paths(paths)
    return coordinator


def validate_configuration(deps: Any) -> Coordinator:
    coordinator = getattr(deps, "coordinator", None) or coordinator_for(
        deps.task_graph_store, deps.path_store
    )
    coordinator.backend.check_history(getattr(deps, "operator_intent_store", None))
    return coordinator
