from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from graphlib import CycleError, TopologicalSorter


class ObjectiveStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Objective:
    id: str
    action: str
    description: str
    depends_on: tuple[str, ...] = ()
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    evidence_ids: tuple[str, ...] = ()


class ObjectiveGraphError(ValueError):
    pass


class ObjectiveGraph:
    """Small deterministic objective state machine for planner/worker handoffs."""

    def __init__(self, objectives: tuple[Objective, ...] = ()) -> None:
        self.objectives: dict[str, Objective] = {item.id: item for item in objectives}
        self.validate()

    @classmethod
    def from_steps(cls, steps) -> "ObjectiveGraph":
        objectives: list[Objective] = []
        previous: str | None = None
        for index, step in enumerate(steps):
            objective_id = f"objective-{index + 1}-{step.action}"
            dependencies = (previous,) if previous else ()
            objectives.append(Objective(objective_id, step.action, step.objective, dependencies))
            previous = objective_id
        return cls(tuple(objectives))

    def validate(self) -> None:
        if len(self.objectives) != len(set(self.objectives)):
            raise ObjectiveGraphError("objective IDs must be unique")
        for item in self.objectives.values():
            for dependency in item.depends_on:
                if dependency not in self.objectives:
                    raise ObjectiveGraphError(f"missing objective dependency: {dependency}")
                if dependency == item.id:
                    raise ObjectiveGraphError(f"objective cannot depend on itself: {item.id}")
        try:
            TopologicalSorter({item.id: set(item.depends_on) for item in self.objectives.values()}).prepare()
        except CycleError as exc:
            raise ObjectiveGraphError("objective dependency cycle") from exc

    def ready(self) -> tuple[Objective, ...]:
        return tuple(
            item for item in self.objectives.values()
            if item.status is ObjectiveStatus.PENDING
            and all(self.objectives[dependency].status is ObjectiveStatus.COMPLETED
                    for dependency in item.depends_on)
        )

    def transition(self, objective_id: str, status: ObjectiveStatus,
                   evidence_ids: tuple[str, ...] = ()) -> Objective:
        current = self.objectives.get(objective_id)
        if current is None:
            raise ObjectiveGraphError(f"unknown objective: {objective_id}")
        if status is ObjectiveStatus.IN_PROGRESS and current not in self.ready():
            raise ObjectiveGraphError(f"objective prerequisites are incomplete: {objective_id}")
        if status is ObjectiveStatus.COMPLETED and not evidence_ids and current.action != "reporting":
            raise ObjectiveGraphError("completed objective requires evidence references")
        if current.status in {ObjectiveStatus.COMPLETED, ObjectiveStatus.CANCELLED}:
            raise ObjectiveGraphError(f"objective is terminal: {objective_id}")
        updated = replace(current, status=status,
                          evidence_ids=evidence_ids or current.evidence_ids)
        self.objectives[objective_id] = updated
        return updated
