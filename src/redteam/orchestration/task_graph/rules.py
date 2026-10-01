"""Task Graph lifecycle and dependency invariants."""

from __future__ import annotations

from redteam.core.contracts.mission import MissionTask
from redteam.core.enums import TaskStatus
from redteam.core.ids import TaskId


class TaskGraphError(ValueError):
    pass


class TaskNotFoundError(TaskGraphError):
    pass


class DuplicateTaskError(TaskGraphError):
    pass


class InvalidTaskTransition(TaskGraphError):
    pass


class InvalidDependency(TaskGraphError):
    pass


TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTaskTransition(f"invalid task transition: {current.value} -> {target.value}")


def validate_dependencies(task: MissionTask, tasks: dict[TaskId, MissionTask]) -> None:
    for dependency_id in task.dependency_task_ids:
        dependency = tasks.get(dependency_id)
        if dependency is None:
            raise InvalidDependency(f"dependency does not exist: {dependency_id}")
        if dependency.mission_id != task.mission_id:
            raise InvalidDependency("dependencies must belong to the same mission")
    cursor = list(task.dependency_task_ids)
    seen: set[TaskId] = set()
    while cursor:
        dependency_id = cursor.pop()
        if dependency_id == task.task_id:
            raise InvalidDependency("dependency would create a cycle")
        if dependency_id in seen:
            continue
        seen.add(dependency_id)
        dependency = tasks.get(dependency_id)
        if dependency is not None:
            cursor.extend(dependency.dependency_task_ids)


def dependencies_completed(task: MissionTask, tasks: dict[TaskId, MissionTask]) -> bool:
    return all(tasks[item].status is TaskStatus.COMPLETED for item in task.dependency_task_ids)
