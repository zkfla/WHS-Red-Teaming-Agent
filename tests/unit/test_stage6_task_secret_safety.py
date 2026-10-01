"""Stage 6 — raw-secret safety for the Mission/Task Graph persistence boundary.

Scope (owned files only):

  - ``redteam.core.contracts.mission`` — Mission/Task/Intent contract free
    text and structured maps (provenance, preferences, constraints).
  - ``redteam.orchestration.task_graph.memory`` — the in-memory store.
  - ``redteam.orchestration.task_graph.sqlite`` — the durable store.

The threat this pins down is the ``model_copy(update=...)`` validator
bypass: Pydantic only runs field validators at *construction*, so
``update_mission`` / ``transition_task`` and any caller that mutates a model
first can otherwise carry a raw secret straight into a store. The boundary
therefore re-derives every value through ``core.redaction`` (core never
imports the Orchestrator) before validating.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import redteam.core.contracts.mission as mission_module
from redteam.core.contracts.mission import (
    MissionCriterionResult,
    MissionPlan,
    MissionTask,
    OperatorIntent,
    Provenance,
)
from redteam.core.enums import MissionStatus, OperatorIntentType, TaskStatus
from redteam.core.ids import EngagementId, MissionId, OperatorIntentId, TaskId
from redteam.orchestration.task_graph import (
    InMemoryTaskGraphStore,
    InvalidTaskTransition,
    SQLiteTaskGraphStore,
)

RAW = "hunter2"
SECRET_TEXT = f"password={RAW}"
REDACTED = "***REDACTED***"


def _now(offset: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)


def mission(revision: int = 1, **overrides: object) -> MissionPlan:
    base: dict[str, object] = {
        "mission_id": MissionId("mission-1"),
        "engagement_id": EngagementId("eng-1"),
        "objective": "Assess authorization",
        "priorities": ["authorization"],
        "success_criteria": ["evidence-backed result"],
        "operator_constraints": {"credential_ref": "vault://eng/tester"},
        "operator_preferences": {"focus": "API"},
        "status": MissionStatus.ACTIVE,
        "revision": revision,
        "created_at": _now(),
        "updated_at": _now(revision - 1),
        "provenance": Provenance(source="operator"),
    }
    base.update(overrides)
    return MissionPlan(**base)  # type: ignore[arg-type]


def task(task_id: str = "root", *, status: TaskStatus = TaskStatus.PENDING, **overrides: object):
    base: dict[str, object] = {
        "task_id": TaskId(task_id),
        "mission_id": MissionId("mission-1"),
        "title": task_id,
        "objective": f"perform {task_id}",
        "status": status,
        "blocking_reason": "waiting" if status is TaskStatus.BLOCKED else None,
        "provenance": Provenance(source="planner"),
        "created_at": _now(),
        "updated_at": _now(),
    }
    base.update(overrides)
    return MissionTask(**base)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryTaskGraphStore()
    return SQLiteTaskGraphStore(tmp_path / "task-graph.sqlite3")


# --- contract free-text surface -----------------------------------------


def test_contract_redacts_free_text_secret_surface() -> None:
    plan = mission(
        objective=SECRET_TEXT,
        priorities=[SECRET_TEXT, "recon"],
        success_criteria=[SECRET_TEXT],
        assumptions=[SECRET_TEXT],
        initial_workstreams=[SECRET_TEXT],
        operator_constraints={"credential_ref": "vault://x", "note": SECRET_TEXT},
        operator_preferences={"focus": [SECRET_TEXT]},
        provenance=Provenance(
            source=SECRET_TEXT,
            references=["token=abc"],
            metadata={"note": SECRET_TEXT},
        ),
    )

    dump = plan.model_dump_json()
    assert RAW not in dump
    assert REDACTED in plan.objective
    # Opaque references survive so suppression/preferences stay usable.
    assert plan.operator_constraints["credential_ref"] == "vault://x"
    assert plan.provenance.references == ["token=***REDACTED***"]


def test_criterion_result_and_operator_intent_are_redacted() -> None:
    criterion = MissionCriterionResult(criterion=SECRET_TEXT, met=True, reason=SECRET_TEXT)
    assert RAW not in criterion.model_dump_json()

    intent = OperatorIntent(
        intent_id=OperatorIntentId("intent-1"),
        mission_id=MissionId("mission-1"),
        intent_type=OperatorIntentType.REPRIORITIZE,
        instruction=SECRET_TEXT,
        task_ids=[TaskId("root")],
        parameters={"priority": 10, "note": SECRET_TEXT},
        provenance=Provenance(source="operator"),
        created_at=_now(),
    )
    assert RAW not in intent.model_dump_json()
    assert "***REDACTED***" in intent.instruction


def test_contract_still_rejects_secret_shaped_keys() -> None:
    for key in ("password", "api_key", "session_token", "authorization"):
        with pytest.raises(ValidationError):
            MissionPlan.model_validate(
                mission().model_dump() | {"operator_constraints": {key: RAW}}
            )

    # A rejection message names the field, never the value — at any nesting.
    with pytest.raises(ValidationError) as exc:
        MissionPlan.model_validate(
            mission().model_dump() | {"operator_constraints": {"password": RAW}}
        )
    assert RAW not in str(exc.value)

    with pytest.raises(ValidationError) as exc_provenance:
        Provenance(source="x", metadata={"token": RAW})
    assert RAW not in str(exc_provenance.value)

    # Opaque `*_id` / `*_ref` keys are references, not secrets.
    allowed = mission(operator_constraints={"credential_ref": "vault://x", "session_id": "s-1"})
    assert allowed.operator_constraints["session_id"] == "s-1"


def test_task_contract_redacts_title_objective_and_reason() -> None:
    blocked = task(
        status=TaskStatus.BLOCKED,
        title=SECRET_TEXT,
        objective=SECRET_TEXT,
        blocking_reason=SECRET_TEXT,
    )
    assert RAW not in blocked.model_dump_json()
    assert REDACTED in blocked.blocking_reason


# --- model_copy bypass ---------------------------------------------------


def test_redacted_copy_closes_the_model_copy_bypass() -> None:
    bypassed = mission().model_copy(update={"objective": SECRET_TEXT})
    assert RAW in bypassed.model_dump_json()  # the bypass leaves memory dirty
    safe = bypassed.redacted_copy()
    assert RAW not in safe.model_dump_json()
    assert bypassed.objective == SECRET_TEXT  # original is not mutated

    dirty_map = mission().model_copy(
        update={"operator_constraints": {"password": RAW, "credential_ref": "vault://x"}}
    )
    cleaned = dirty_map.redacted_copy()
    assert "password" not in cleaned.operator_constraints
    assert cleaned.operator_constraints["credential_ref"] == "vault://x"


def test_redacted_copy_never_echoes_a_raw_secret_in_its_error() -> None:
    # A bypass-injected reason on a non-BLOCKED Task is invalid; redaction runs
    # before validation so the raised error cannot carry the raw value.
    invalid = task().model_copy(update={"blocking_reason": SECRET_TEXT})
    with pytest.raises(ValidationError) as exc:
        invalid.redacted_copy()
    assert RAW not in str(exc.value)


# --- store persistence boundary -----------------------------------------


async def test_store_redacts_mission_and_task_free_text(store) -> None:
    await store.create_mission(mission(objective=SECRET_TEXT))
    await store.create_task(task(objective=SECRET_TEXT, title=SECRET_TEXT))

    got_mission = await store.get_mission(MissionId("mission-1"))
    got_task = await store.get_task(TaskId("root"))
    assert got_mission is not None and got_task is not None
    assert RAW not in got_mission.model_dump_json()
    assert RAW not in got_task.model_dump_json()
    assert REDACTED in got_mission.objective
    assert REDACTED in got_task.objective


async def test_update_mission_redacts_a_model_copy_bypass(store) -> None:
    await store.create_mission(mission())
    bypassed = mission(revision=2).model_copy(
        update={
            "objective": SECRET_TEXT,
            "operator_preferences": {"excluded_endpoints": [SECRET_TEXT]},
        }
    )
    await store.update_mission(bypassed)

    got = await store.get_mission(MissionId("mission-1"))
    assert got is not None
    assert got.revision == 2
    assert RAW not in got.model_dump_json()


async def test_update_task_redacts_a_model_copy_bypass(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task())
    bypassed = task().model_copy(update={"objective": SECRET_TEXT, "title": SECRET_TEXT})
    await store.update_task(bypassed)

    got = await store.get_task(TaskId("root"))
    assert got is not None
    assert RAW not in got.model_dump_json()


async def test_transition_reason_is_redacted_at_the_boundary(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task())
    await store.transition_task(TaskId("root"), TaskStatus.READY, now=_now(1))

    blocked = await store.block_task(TaskId("root"), SECRET_TEXT, now=_now(2))
    assert RAW not in blocked.model_dump_json()
    assert REDACTED in blocked.blocking_reason
    assert RAW not in (await store.get_task(TaskId("root"))).model_dump_json()


async def test_redaction_preserves_status_and_opaque_refs(store) -> None:
    await store.create_mission(
        mission(operator_constraints={"credential_ref": "vault://eng/tester"})
    )
    await store.create_task(task(status=TaskStatus.READY, priority=5))

    got_mission = await store.get_mission(MissionId("mission-1"))
    got_task = await store.get_task(TaskId("root"))
    assert got_mission is not None and got_task is not None
    assert got_mission.status is MissionStatus.ACTIVE
    assert got_mission.operator_constraints["credential_ref"] == "vault://eng/tester"
    assert got_task.status is TaskStatus.READY
    assert got_task.priority == 5


async def test_store_errors_do_not_carry_raw_secrets(store) -> None:
    await store.create_mission(mission())
    await store.create_task(task())

    invalid = task().model_copy(update={"blocking_reason": SECRET_TEXT})
    with pytest.raises(ValidationError) as exc:
        await store.update_task(invalid)
    assert RAW not in str(exc.value)

    with pytest.raises(InvalidTaskTransition) as exc2:
        await store.transition_task(
            TaskId("root"), TaskStatus.COMPLETED, now=_now(1), reason=SECRET_TEXT
        )
    assert RAW not in str(exc2.value)


# --- legacy durable payloads --------------------------------------------


async def test_sqlite_legacy_payload_is_redacted_and_reflushed(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    bootstrap = SQLiteTaskGraphStore(path)
    await bootstrap.setup()  # creates the schema only

    legacy_mission = mission().model_dump(mode="json") | {
        "objective": SECRET_TEXT,
        "operator_constraints": {"credential_ref": "vault://legacy", "password": RAW},
    }
    legacy_task = task().model_dump(mode="json") | {"objective": SECRET_TEXT}
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO missions VALUES(?,?,?)",
            ("mission-1", "eng-1", json.dumps(legacy_mission)),
        )
        db.execute(
            "INSERT INTO mission_tasks VALUES(?,?,?,?,?)",
            ("root", "mission-1", "pending", 0, json.dumps(legacy_task)),
        )

    store = SQLiteTaskGraphStore(path)
    got_mission = await store.get_mission(MissionId("mission-1"))
    got_task = await store.get_task(TaskId("root"))
    assert got_mission is not None and got_task is not None
    # Non-destructive: identity/status/refs survive, the secret does not.
    assert got_mission.status is MissionStatus.ACTIVE
    assert got_mission.operator_constraints["credential_ref"] == "vault://legacy"
    assert "password" not in got_mission.operator_constraints
    assert RAW not in got_mission.model_dump_json()
    assert RAW not in got_task.model_dump_json()

    # Any later mutation re-flushes the whole graph, scrubbing the raw bytes.
    await store.update_priority(TaskId("root"), 3, now=_now(1))
    with sqlite3.connect(path) as db:
        payloads = [row[0] for row in db.execute("SELECT payload FROM missions")]
        payloads += [row[0] for row in db.execute("SELECT payload FROM mission_tasks")]
    assert payloads
    assert all(RAW not in payload for payload in payloads)


# --- ownership / dependency direction -----------------------------------


def test_mission_contract_does_not_depend_on_the_orchestrator() -> None:
    tree = ast.parse(inspect.getsource(mission_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(module.startswith("redteam.orchestration") for module in imported)
    assert "redteam.core.redaction" in imported
