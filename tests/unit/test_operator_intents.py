"""Runtime Operator Intent channel (Prompt 4).

Covers the piece contracts alone cannot: a received instruction becomes an
applied Mission/Task-Graph effect, with a walkable
``OperatorInstructionReceived -> OperatorIntentApplied -> Task*`` audit
chain, deterministic Korean/English interpretation, secret redaction, and
pause-before-execution that resumes the same Task/Mission.

No tool gateway is ever constructed here — the channel has no execution
path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import (
    AgentPhase,
    GraphNodeType,
    MissionStatus,
    OperatorIntentType,
    TaskStatus,
)
from redteam.core.ids import (
    AgentId,
    EngagementId,
    GraphNodeId,
    MissionId,
    RunId,
    TaskId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import CANDIDATE_REJECTED
from redteam.orchestration.nodes import observe_node
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    IntentInterpretationError,
    IntentRecord,
    IntentStatus,
    OperatorIntentService,
    SQLiteOperatorIntentStore,
    classify_instruction,
    interpret_instruction,
    redact_text,
    secret_parameters,
)
from redteam.orchestration.operator_intents.interpreter import (
    extract_endpoint_selectors,
    extract_subjects,
    extract_task_selectors,
)
from redteam.orchestration.operator_intents.models import (
    ActionExplanation,
    BlockedTaskExplanation,
    CandidateExplanation,
)
from redteam.orchestration.state import (
    RuntimeState,
    TerminationReason,
    initial_graph_state,
)
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes import build_test_application

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = RunId("run_intent")
ENG = EngagementId("eng_intent")
MISSION = MissionId("mission_intent")


def mission(status: MissionStatus = MissionStatus.ACTIVE) -> MissionPlan:
    return MissionPlan(
        mission_id=MISSION,
        engagement_id=ENG,
        objective="Assess the declared scope",
        success_criteria=["evidence-backed result"],
        status=status,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


def task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.READY,
    priority: int = 10,
    capability: str = "web",
    objective: str = "Investigate web surface",
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MISSION,
        title=task_id,
        objective=objective,
        status=status,
        priority=priority,
        required_capability=capability,
        assigned_agent=AgentId("web-api-agent"),
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


async def seeded_task_store(*tasks: MissionTask) -> InMemoryTaskGraphStore:
    store = InMemoryTaskGraphStore()
    await store.create_mission(mission())
    for item in tasks:
        await store.create_task(item)
    return store


def publisher(bus: InMemoryEventBus, source: str = "test.operator") -> EventPublisher:
    return EventPublisher(bus, run_id=RUN, engagement_id=ENG, source=source)


def service(
    *,
    task_store: InMemoryTaskGraphStore,
    intent_store: InMemoryOperatorIntentStore | SQLiteOperatorIntentStore,
    bus: InMemoryEventBus | None = None,
    graph_store: InMemoryAttackGraphStore | None = None,
    work_store: InMemoryWorkStore | None = None,
) -> OperatorIntentService:
    return OperatorIntentService(
        store=intent_store,
        task_graph_store=task_store,
        event_bus=bus,
        graph_store=graph_store,
        work_store=work_store,
    )


def blocked_task(
    task_id: str,
    *,
    depends_on: str | None = None,
    reason: str = "waiting on a prerequisite",
    capability: str = "api",
    objective: str = "Probe /api/docs",
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MISSION,
        title=task_id,
        objective=objective,
        status=TaskStatus.BLOCKED,
        priority=10,
        required_capability=capability,
        dependency_task_ids=[TaskId(depends_on)] if depends_on else [],
        blocking_reason=reason,
        assigned_agent=AgentId("web-api-agent"),
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


# --- store backends ------------------------------------------------------


def intent_record(instruction: str = "focus web") -> IntentRecord:
    intent = interpret_instruction(instruction, mission_id=MISSION, now=NOW)
    return IntentRecord(intent=intent, correlation_id=str(intent.intent_id))


@pytest.fixture(params=["memory", "sqlite"])
def intent_store(request, tmp_path: Path):
    if request.param == "memory":
        return InMemoryOperatorIntentStore()
    return SQLiteOperatorIntentStore(tmp_path / "intents.sqlite3")


async def test_store_round_trip_update_and_status_filter(intent_store) -> None:
    record = intent_record()
    await intent_store.append(record)

    stored = await intent_store.get(record.intent.intent_id)
    assert stored is not None and stored.status is IntentStatus.RECEIVED
    assert stored.intent.intent_type is OperatorIntentType.FOCUS

    await intent_store.update(record.model_copy(update={"status": IntentStatus.APPLIED}))
    assert (await intent_store.get(record.intent.intent_id)).status is IntentStatus.APPLIED
    assert await intent_store.list_for_mission(MISSION, status=IntentStatus.RECEIVED) == []
    assert len(await intent_store.list_for_mission(MISSION, status=IntentStatus.APPLIED)) == 1


async def test_sqlite_store_recovers_history_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable-intents.sqlite3"
    first = SQLiteOperatorIntentStore(path)
    record = intent_record("pause")
    await first.append(record)
    await first.update(record.model_copy(update={"status": IntentStatus.APPLIED}))

    second = SQLiteOperatorIntentStore(path)
    restored = await second.get(record.intent.intent_id)
    assert restored is not None
    assert restored.status is IntentStatus.APPLIED
    assert [item.intent.intent_id for item in await second.list_for_mission(MISSION)] == [
        record.intent.intent_id
    ]


async def test_sqlite_store_never_persists_spaced_password(tmp_path: Path) -> None:
    path = tmp_path / "redacted-intents.sqlite3"
    record = intent_record("pause password hunter2")
    store = SQLiteOperatorIntentStore(path)
    await store.append(record)
    assert "hunter2" not in path.read_bytes().decode("utf-8", errors="ignore")
    assert "hunter2" not in (await store.get(record.intent.intent_id)).model_dump_json()


# --- deterministic interpretation ---------------------------------------


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("pause the run", OperatorIntentType.PAUSE),
        ("일시정지해줘", OperatorIntentType.PAUSE),
        ("resume now", OperatorIntentType.RESUME),
        ("계속 진행해", OperatorIntentType.RESUME),
        ("stop everything", OperatorIntentType.STOP),
        ("중단해", OperatorIntentType.STOP),
        ("focus on web", OperatorIntentType.FOCUS),
        ("집중: api", OperatorIntentType.FOCUS),
        ("exclude mobile", OperatorIntentType.EXCLUDE),
        ("제외: 모바일", OperatorIntentType.EXCLUDE),
        ("reprioritize authorization 90", OperatorIntentType.REPRIORITIZE),
        ("우선순위: authentication", OperatorIntentType.REPRIORITIZE),
        ("why did you stop?", OperatorIntentType.EXPLANATION_QUERY),
        ("status please", OperatorIntentType.STATUS_QUERY),
        ("현황 알려줘", OperatorIntentType.STATUS_QUERY),
    ],
)
def test_classification_is_deterministic_for_korean_and_english(
    instruction: str, expected: OperatorIntentType
) -> None:
    assert classify_instruction(instruction).intent_type is expected


def test_query_wins_over_an_action_word() -> None:
    # "explain why you stopped" is a question, not a second STOP command.
    assert (
        classify_instruction("explain why you stopped").intent_type
        is OperatorIntentType.EXPLANATION_QUERY
    )


def test_unknown_and_empty_instructions_are_rejected() -> None:
    for instruction in ("", "   ", "do the thing"):
        with pytest.raises(IntentInterpretationError):
            classify_instruction(instruction)


def test_interpreter_extracts_areas_and_priority_and_redacts() -> None:
    focus = interpret_instruction(
        "focus on web and api with token=supersecret", mission_id=MISSION, now=NOW
    )
    assert focus.intent_type is OperatorIntentType.FOCUS
    assert focus.parameters["focus_areas"] == ["web", "api"]
    assert "supersecret" not in focus.instruction
    assert focus.instruction.lower().startswith("focus")

    reprioritize = interpret_instruction(
        "reprioritize authorization 90", mission_id=MISSION, now=NOW
    )
    assert reprioritize.parameters.get("priority") == 90


# --- redaction -----------------------------------------------------------


def test_redaction_removes_secret_values_but_keeps_opaque_refs() -> None:
    assert "abc123" not in redact_text("token=abc123 please")
    assert "***REDACTED***" in redact_text("token=abc123 please")
    assert "abc456" not in redact_text("Authorization: Bearer abc456")

    cleaned = secret_parameters(
        {
            "token": "abc123",
            "credential_ref": "vault://eng/tester",
            "note": "bearer abc999",
            "nested": {"api_key": "raw"},
        }
    )
    assert "token" not in cleaned
    assert cleaned["credential_ref"] == "vault://eng/tester"
    assert "abc999" not in cleaned["note"]
    assert cleaned["nested"]["api_key"] == "***REDACTED***"


async def test_receive_never_persists_a_raw_secret() -> None:
    store = InMemoryOperatorIntentStore()
    svc = service(task_store=await seeded_task_store(task("web")), intent_store=store)

    await svc.receive("focus web with password=hunter2", mission_id=MISSION, run_id=RUN)

    persisted = await store.list_for_mission(MISSION)
    assert len(persisted) == 1
    assert "hunter2" not in persisted[0].model_dump_json()


async def test_receive_rejects_executable_parameter_without_persisting() -> None:
    store = InMemoryOperatorIntentStore()
    svc = service(task_store=await seeded_task_store(task("web")), intent_store=store)

    result = await svc.receive(
        "focus web", mission_id=MISSION, run_id=RUN, parameters={"tool_id": "scanner"}
    )

    assert result.status is IntentStatus.REJECTED
    assert result.applied is False
    assert await store.list_for_mission(MISSION) == []


async def test_receive_rejects_unknown_instruction_without_persisting() -> None:
    store = InMemoryOperatorIntentStore()
    svc = service(task_store=await seeded_task_store(task("web")), intent_store=store)

    result = await svc.receive("please do the needful", mission_id=MISSION, run_id=RUN)

    assert result.status is IntentStatus.REJECTED
    assert await store.list_for_mission(MISSION) == []


# --- receive -> applied -> task events ----------------------------------


async def test_receive_apply_publishes_correlated_causation_chain() -> None:
    task_store = await seeded_task_store(
        task("web", priority=10),
        task("mobile", capability="mobile", objective="Investigate mobile surface"),
    )
    bus = InMemoryEventBus()
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    pub = publisher(bus)

    received = await svc.receive("focus web", mission_id=MISSION, run_id=RUN, publisher=pub)
    assert received.status is IntentStatus.RECEIVED
    assert received.received_event_id is not None

    applied = await svc.apply(received.intent_id, publisher=pub)
    assert applied.applied is True
    assert applied.applied_event_id is not None

    chain = bus.correlated(str(received.intent_id))
    assert [event.event_type for event in chain] == [
        EventType.OPERATOR_INSTRUCTION_RECEIVED,
        EventType.OPERATOR_INTENT_APPLIED,
        EventType.TASK_PRIORITY_CHANGED,
    ]
    assert chain[1].causation_id == chain[0].event_id
    assert chain[2].causation_id == chain[1].event_id

    # Only the web task moved; mobile is untouched and no task was created.
    assert (await task_store.get_task(TaskId("web"))).priority == 500
    assert (await task_store.get_task(TaskId("mobile"))).priority == 10
    assert len(await task_store.list_tasks(mission_id=MISSION)) == 2

    focus_mission = await task_store.get_mission(MISSION)
    assert focus_mission.operator_preferences["focus_areas"] == ["web"]


async def test_apply_is_idempotent() -> None:
    task_store = await seeded_task_store(task("web", priority=10))
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    received = await svc.receive("focus web", mission_id=MISSION, run_id=RUN)

    await svc.apply(received.intent_id)
    second = await svc.apply(received.intent_id)
    assert second.applied is True and second.outcome == "focused 1 task(s)"


async def test_exclude_cancels_pending_work_but_not_a_running_task() -> None:
    task_store = await seeded_task_store(
        task("web-ready", priority=10),
        task(
            "api-running",
            status=TaskStatus.RUNNING,
            priority=20,
            capability="api",
            objective="Investigate api surface",
        ),
        task("mobile", priority=30, capability="mobile", objective="Investigate mobile surface"),
    )
    bus = InMemoryEventBus()
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    pub = publisher(bus)

    await svc.receive_and_apply(
        "exclude web",
        mission_id=MISSION,
        run_id=RUN,
        publisher=pub,
    )

    assert (await task_store.get_task(TaskId("web-ready"))).status is TaskStatus.CANCELLED
    # web also matches the RUNNING api task? No: area "web" is not in its text.
    assert (await task_store.get_task(TaskId("api-running"))).status is TaskStatus.RUNNING
    assert (await task_store.get_task(TaskId("mobile"))).status is TaskStatus.READY
    assert bus.history(event_type=EventType.TASK_CANCELLED)


async def test_stop_cancels_work_and_marks_the_mission_cancelled() -> None:
    task_store = await seeded_task_store(
        task("web", priority=10), task("api", status=TaskStatus.RUNNING, priority=20)
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())

    result = await svc.receive_and_apply("stop", mission_id=MISSION, run_id=RUN)

    assert result.applied is True
    assert (await task_store.get_mission(MISSION)).status is MissionStatus.CANCELLED
    assert (await task_store.get_task(TaskId("web"))).status is TaskStatus.CANCELLED
    assert (await task_store.get_task(TaskId("api"))).status is TaskStatus.CANCELLED


async def test_rejected_intent_reason_never_echoes_a_caller_parameter() -> None:
    """A validation error from caller-supplied parameters must not persist or
    publish the raw value: keyword-based redaction cannot recognize an
    unlabelled credential inside an exception message."""
    task_store = await seeded_task_store(task("web", priority=10))
    intents = InMemoryOperatorIntentStore()
    bus = InMemoryEventBus()
    svc = service(task_store=task_store, intent_store=intents)

    result = await svc.receive_and_apply(
        "reprioritize task:web",
        mission_id=MISSION,
        run_id=RUN,
        parameters={"priorities": {"web": "hunter2"}},
        publisher=publisher(bus),
    )

    assert result.status is IntentStatus.REJECTED
    assert result.reason == "intent_validation_failed:ValueError"
    record = await intents.get(result.intent_id)
    assert record is not None and record.reason == "intent_validation_failed:ValueError"
    # The failure path must not add raw text anywhere new: the event carries
    # only the stable label, never the exception message.
    rejected = bus.history(event_type=EventType.OPERATOR_INTENT_REJECTED)
    assert rejected and rejected[0].payload["reason"] == "intent_validation_failed:ValueError"
    assert "hunter2" not in json.dumps(rejected[0].payload)


# --- read-only queries ---------------------------------------------------


async def test_status_and_explain_are_read_only() -> None:
    task_store = await seeded_task_store(task("web", priority=10))
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    await svc.receive_and_apply("focus web", mission_id=MISSION, run_id=RUN)

    before_mission = await task_store.get_mission(MISSION)
    before_task = await task_store.get_task(TaskId("web"))

    report = await svc.status(mission_id=MISSION)
    explanation = await svc.explain(mission_id=MISSION, run_id=RUN)

    assert report.focus_areas == ["web"]
    assert report.task_status_counts == {"ready": 1}
    assert "web" in explanation.task_priorities
    assert explanation.mission_status is MissionStatus.ACTIVE
    # Nothing about the canonical state changed by asking.
    assert (await task_store.get_mission(MISSION)) == before_mission
    assert (await task_store.get_task(TaskId("web"))) == before_task


async def test_query_intent_is_applied_without_touching_state() -> None:
    task_store = await seeded_task_store(task("web", priority=10))
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    before = await task_store.get_task(TaskId("web"))

    result = await svc.receive_and_apply("status please", mission_id=MISSION, run_id=RUN)

    assert result.applied is True
    assert result.intent_type is OperatorIntentType.STATUS_QUERY
    assert (await task_store.get_task(TaskId("web"))) == before


# --- observe integration: pause before execution, resume same task -------


@dataclass(frozen=True)
class _IntentDeps(RuntimeDeps):
    """Test-only shim: the composition root adds this field to RuntimeDeps."""

    operator_intent_store: object | None = None


def deps_with_intents(intent_store, task_store) -> RuntimeDeps:
    base = replace(build_test_application(), task_graph_store=task_store)
    # Forward-compatible: once the composition root adds the optional
    # ``operator_intent_store`` field to ``RuntimeDeps`` itself, use it
    # directly instead of the test-only subclass shim.
    if any(field.name == "operator_intent_store" for field in fields(RuntimeDeps)):
        return replace(base, operator_intent_store=intent_store)
    values = {field.name: getattr(base, field.name) for field in fields(RuntimeDeps)}
    values["operator_intent_store"] = intent_store
    return _IntentDeps(**values)


async def test_observe_pauses_before_execution_and_resumes_same_task() -> None:
    task_store = InMemoryTaskGraphStore()
    await task_store.create_mission(mission())
    await task_store.create_task(
        task(
            "recon-task",
            status=TaskStatus.RUNNING,
            priority=10,
            capability="recon",
            objective="Map the scope",
        ).model_copy(update={"assigned_agent": AgentId("recon-agent")})
    )
    intent_store = InMemoryOperatorIntentStore()
    deps = deps_with_intents(intent_store, task_store)
    # A HOST node makes the recon role schedulable, so resume has somewhere to go.
    await AttackGraphService(deps.graph_store).upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"in_scope": True}
    )

    runtime = RuntimeState(
        run_id=RUN,
        engagement_id=ENG,
        mission_id=MISSION,
        active_task_id=TaskId("recon-task"),
        current_agent=AgentId("recon-agent"),
    )

    # Out-of-band pause: no publisher at receive time; OBSERVE records it.
    await service(task_store=task_store, intent_store=intent_store).receive(
        "pause now", mission_id=MISSION, run_id=RUN
    )

    paused = await observe_node(initial_graph_state(runtime), deps=deps)
    assert paused["runtime"].phase is AgentPhase.REPLAN
    assert paused["runtime"].termination_reason == TerminationReason.OPERATOR_PAUSED
    assert paused["runtime"].active_task_id == TaskId("recon-task")
    assert paused["runtime"].current_agent is None
    assert (await task_store.get_mission(MISSION)).status is MissionStatus.PAUSED
    # The active Task is preserved, not completed.
    assert (await task_store.get_task(TaskId("recon-task"))).status is TaskStatus.RUNNING

    chain = deps.event_bus.correlated(  # type: ignore[attr-defined]
        (await intent_store.list_for_mission(MISSION))[0].correlation_id
    )
    assert [event.event_type for event in chain] == [
        EventType.OPERATOR_INSTRUCTION_RECEIVED,
        EventType.OPERATOR_INTENT_APPLIED,
    ]

    # Resume: same Mission, same Task, now schedulable again.
    await service(task_store=task_store, intent_store=intent_store).receive(
        "resume", mission_id=MISSION, run_id=RUN
    )
    resumed_runtime = paused["runtime"].model_copy(
        update={"phase": AgentPhase.OBSERVE, "termination_reason": None}
    )
    resumed = await observe_node(initial_graph_state(resumed_runtime), deps=deps)

    assert (await task_store.get_mission(MISSION)).status is MissionStatus.ACTIVE
    assert (await task_store.get_task(TaskId("recon-task"))).status is TaskStatus.RUNNING
    assert resumed["runtime"].active_task_id == TaskId("recon-task")
    assert resumed["runtime"].termination_reason is None


async def test_observe_without_an_intent_store_is_unchanged() -> None:
    # from_deps returns None for a deps bundle that never adopted the field.
    assert OperatorIntentService.from_deps(build_test_application()) is None


# --- structural boundary -------------------------------------------------


def test_channel_never_imports_capability_or_control_or_a_tool_gateway() -> None:
    import ast

    package = Path(__file__).parents[2] / "src" / "redteam" / "orchestration" / "operator_intents"
    offenders: list[str] = []
    for py_file in package.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            if module and module.split(".")[:2] in (
                ["redteam", "capability"],
                ["redteam", "control"],
            ):
                offenders.append(f"{py_file.name}: {module}")
            if module and "tool_gateway" in module:
                offenders.append(f"{py_file.name}: {module}")
    assert not offenders, f"operator-intent channel must not reach execution: {offenders}"


def test_no_scope_expansion_terms_in_service() -> None:
    # The service surface must not carry engagement/scope mutation hooks.
    from redteam.orchestration.operator_intents import OperatorIntentService

    for banned in ("expand_scope", "add_target", "engagement"):
        assert not hasattr(OperatorIntentService, banned)


# --- Prompt 4: exact selectors, suppression, structured explanation -------


def test_korean_suppression_phrase_is_exclude_not_unknown() -> None:
    # Literal Prompt 4: "이 endpoint는 더 이상 보지 마".
    classification = classify_instruction("이 endpoint는 더 이상 보지 마")
    assert classification.intent_type is OperatorIntentType.EXCLUDE
    assert "endpoint" in classification.subjects


@pytest.mark.parametrize(
    ("instruction", "expected_type", "subjects"),
    [
        ("why '/api/docs 안 봐?", OperatorIntentType.EXPLANATION_QUERY, []),
        ("why Candidate excluded", OperatorIntentType.EXPLANATION_QUERY, ["candidate"]),
        ("why was this action chosen", OperatorIntentType.EXPLANATION_QUERY, ["action"]),
        ("why is this Task blocked", OperatorIntentType.EXPLANATION_QUERY, ["blocked", "task"]),
        ("현황 알려줘", OperatorIntentType.STATUS_QUERY, []),
    ],
)
def test_prompt4_literal_phrases_classify_as_expected(
    instruction: str, expected_type: OperatorIntentType, subjects: list[str]
) -> None:
    classification = classify_instruction(instruction)
    assert classification.intent_type is expected_type
    assert classification.subjects == subjects


def test_endpoint_selector_is_bounded_and_validated() -> None:
    assert extract_endpoint_selectors("why '/api/docs 안 봐?") == ["/api/docs"]
    assert extract_endpoint_selectors("focus https://a.local:8443/api/v2") == [
        "https://a.local:8443/api/v2"
    ]
    # Not selectors: a bare slash, traversal, or text with no path at all.
    assert extract_endpoint_selectors("look at / only") == []
    assert extract_endpoint_selectors("check /../../etc/passwd") == []
    assert extract_endpoint_selectors("no path here") == []


def test_task_selector_uses_the_bounded_syntax() -> None:
    assert extract_task_selectors("focus task:web-ready") == ["web-ready"]
    assert extract_task_selectors("focus task#api-task") == ["api-task"]
    assert extract_task_selectors("focus web") == []


def test_endpoint_path_version_is_not_mistaken_for_a_priority() -> None:
    intent = interpret_instruction("reprioritize /api/v2 90", mission_id=MISSION, now=NOW)
    assert intent.parameters.get("priority") == 90
    # With no explicit number the path's "2" must not become the priority.
    bare = interpret_instruction("reprioritize /api/v2", mission_id=MISSION, now=NOW)
    assert "priority" not in bare.parameters


def test_query_instruction_records_selectors_for_explanation() -> None:
    intent = interpret_instruction("why /api/docs 안 봐?", mission_id=MISSION, now=NOW)
    assert intent.intent_type is OperatorIntentType.EXPLANATION_QUERY
    assert intent.parameters["endpoint_selectors"] == ["/api/docs"]


def test_endpoint_selector_does_not_widen_the_extracted_area() -> None:
    # ``/api/docs`` contains "api"; a selector must stay exact, not become
    # "exclude the whole api area".
    classification = classify_instruction("exclude /api/docs")
    assert classification.areas == []
    assert classification.endpoint_selectors == ["/api/docs"]
    # An explicit area alongside a selector is still honoured.
    both = classify_instruction("exclude api /api/docs")
    assert both.areas == ["api"]


async def test_excluding_an_endpoint_does_not_widen_to_the_whole_api_area() -> None:
    task_store = await seeded_task_store(
        task("docs", priority=10, capability="api", objective="Probe /api/docs"),
        task("api-other", priority=10, capability="api", objective="Probe /api/orders"),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())

    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    assert (await task_store.get_task(TaskId("docs"))).status is TaskStatus.CANCELLED
    assert (await task_store.get_task(TaskId("api-other"))).status is TaskStatus.READY


async def test_excluding_endpoint_does_not_cancel_a_prefix_neighbor() -> None:
    task_store = await seeded_task_store(
        task("docs", objective="Probe /api/docs"),
        task("docs-old", objective="Probe /api/docs-old"),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())

    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    assert (await task_store.get_task(TaskId("docs"))).status is TaskStatus.CANCELLED
    assert (await task_store.get_task(TaskId("docs-old"))).status is TaskStatus.READY


async def test_exclude_endpoint_cancels_matching_task_and_records_suppression() -> None:
    task_store = await seeded_task_store(
        task("docs", priority=10, capability="api", objective="Probe /api/docs"),
        task("other", priority=10, capability="web", objective="Investigate web surface"),
    )
    intent_store = InMemoryOperatorIntentStore()
    svc = service(task_store=task_store, intent_store=intent_store)

    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    assert (await task_store.get_task(TaskId("docs"))).status is TaskStatus.CANCELLED
    assert (await task_store.get_task(TaskId("other"))).status is TaskStatus.READY
    mission = await task_store.get_mission(MISSION)
    assert mission.operator_preferences["excluded_endpoints"] == ["/api/docs"]


async def test_suppression_prevents_refocusing_an_excluded_running_task() -> None:
    task_store = await seeded_task_store(
        task(
            "docs",
            status=TaskStatus.RUNNING,
            priority=10,
            capability="api",
            objective="Probe /api/docs",
        ),
        task("other", priority=10, capability="web", objective="Investigate web surface"),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())

    # A RUNNING task is not yanked, but the endpoint is recorded as excluded.
    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)
    assert (await task_store.get_task(TaskId("docs"))).status is TaskStatus.RUNNING

    result = await svc.receive_and_apply("focus /api/docs", mission_id=MISSION, run_id=RUN)

    assert "suppressed" in (result.outcome or "")
    assert (await task_store.get_task(TaskId("docs"))).priority == 10  # not re-raised
    assert (await task_store.get_task(TaskId("other"))).priority == 10


async def test_status_query_includes_blocked_tasks_and_excluded_endpoints() -> None:
    task_store = await seeded_task_store(
        task(
            "docs",
            status=TaskStatus.RUNNING,
            priority=10,
            capability="api",
            objective="Probe /api/docs",
        ),
        blocked_task(
            "blocked-docs",
            reason="waiting on a credential",
            objective="Probe /api/orders",
        ),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    report = await svc.status(mission_id=MISSION)

    assert report.excluded_endpoints == ["/api/docs"]
    assert report.blocked_tasks
    assert report.blocked_tasks[0].blocking_reason == "waiting on a credential"
    assert "blocked" in report.summary


async def test_explain_reports_blocked_task_reason_and_open_dependency() -> None:
    task_store = await seeded_task_store(
        task("prereq", priority=10, capability="web", objective="Investigate web surface"),
        blocked_task("blocked-docs", depends_on="prereq", reason="waiting on fingerprint"),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())

    explanation = await svc.explain(mission_id=MISSION)

    assert len(explanation.blocked_tasks) == 1
    blocked = explanation.blocked_tasks[0]
    assert isinstance(blocked, BlockedTaskExplanation)
    assert blocked.task_id == TaskId("blocked-docs")
    assert blocked.blocking_reason == "waiting on fingerprint"
    assert blocked.blocked_dependency_ids == [TaskId("prereq")]
    assert "blocked-docs" in explanation.summary
    assert "waiting on fingerprint" in explanation.summary


async def test_explain_matches_an_endpoint_from_the_attack_graph() -> None:
    graph_store = InMemoryAttackGraphStore()
    await AttackGraphService(graph_store).upsert_node(
        GraphNodeType.ENDPOINT,
        key="a.local GET /api/docs",
        attrs={"in_scope": True},
    )
    svc = service(
        task_store=await seeded_task_store(task("docs", capability="api")),
        intent_store=InMemoryOperatorIntentStore(),
        graph_store=graph_store,
    )

    explanation = await svc.explain(mission_id=MISSION, endpoint_selectors=["/api/docs"])

    assert explanation.endpoint_selectors == ["/api/docs"]
    assert explanation.endpoint_matches
    assert "/api/docs" in explanation.endpoint_matches[0]
    assert "/api/docs" in explanation.summary


async def test_explain_derives_selectors_from_the_latest_query_intent() -> None:
    task_store = await seeded_task_store(task("docs", capability="api"))
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    await svc.receive("why /api/docs 안 봐?", mission_id=MISSION, run_id=RUN)

    explanation = await svc.explain(mission_id=MISSION)

    assert explanation.endpoint_selectors == ["/api/docs"]
    # No graph store wired -> degrades honestly rather than failing.
    assert explanation.endpoint_matches == []
    assert "not recorded in the attack graph" in explanation.summary


async def test_explain_reports_suppressed_endpoint() -> None:
    task_store = await seeded_task_store(
        task(
            "docs",
            status=TaskStatus.RUNNING,
            priority=10,
            capability="api",
            objective="Probe /api/docs",
        ),
    )
    svc = service(task_store=task_store, intent_store=InMemoryOperatorIntentStore())
    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    explanation = await svc.explain(mission_id=MISSION, endpoint_selectors=["/api/docs"])

    assert explanation.suppressed_selectors == ["/api/docs"]
    assert explanation.excluded_endpoints == ["/api/docs"]
    assert "excluded by operator intent" in explanation.summary


async def test_explain_reports_rejected_candidate_and_chosen_action_from_events() -> None:
    bus = InMemoryEventBus()
    svc = service(
        task_store=await seeded_task_store(task("docs", capability="api")),
        intent_store=InMemoryOperatorIntentStore(),
        bus=bus,
    )
    pub = publisher(bus)
    await pub.publish(
        CANDIDATE_REJECTED,
        {
            "operation": "recon.probe",
            "reason": "prior_terminal_execution",
            "field": "",
            "candidate_key": "k1",
        },
    )
    await pub.publish(
        EventType.ACTION_PROPOSED,
        {"operation": "web.inspect", "candidate_key": "k2", "target": "a.local"},
    )

    explanation = await svc.explain(
        mission_id=MISSION, run_id=RUN, subjects=["candidate", "action"]
    )

    assert explanation.rejected_candidates
    assert isinstance(explanation.rejected_candidates[0], CandidateExplanation)
    assert explanation.rejected_candidates[0].operation == "recon.probe"
    assert explanation.rejected_candidates[0].reason == "prior_terminal_execution"
    assert explanation.chosen_actions
    assert isinstance(explanation.chosen_actions[0], ActionExplanation)
    assert explanation.chosen_actions[0].operation == "web.inspect"


async def test_explanation_does_not_mix_other_runs_on_shared_event_bus() -> None:
    bus = InMemoryEventBus()
    svc = service(
        task_store=await seeded_task_store(task("docs")),
        intent_store=InMemoryOperatorIntentStore(), bus=bus,
    )
    await publisher(bus).publish(
        CANDIDATE_REJECTED, {"operation": "this-run", "reason": "not viable"}
    )
    other = EventPublisher(bus, run_id=RunId("run_other"), engagement_id=ENG, source="test")
    await other.publish(CANDIDATE_REJECTED, {"operation": "other-run", "reason": "private"})
    await other.publish(EventType.ACTION_PROPOSED, {"operation": "other-action"})

    current = await svc.explain(mission_id=MISSION, run_id=RUN)
    assert [item.operation for item in current.rejected_candidates] == ["this-run"]
    assert current.chosen_actions == []
    unknown = await svc.explain(mission_id=MISSION)
    assert unknown.rejected_candidates == []
    assert unknown.chosen_actions == []


async def test_operator_renderer_shows_bounded_structured_reasons_without_secrets() -> None:
    from redteam.app.cli.renderer import operator_explanation_text, operator_status_text

    store = await seeded_task_store(
        task("missing", objective="Probe /api/other"),
        blocked_task(
            "blocked", reason="missing token=do-not-print",
            depends_on="missing", objective="Probe /api/other",
        )
    )
    svc = service(task_store=store, intent_store=InMemoryOperatorIntentStore())
    await svc.receive_and_apply("exclude /api/docs", mission_id=MISSION, run_id=RUN)

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN)
    status = await svc.status(mission_id=MISSION)
    shown = operator_explanation_text(explanation) + operator_status_text(status)
    assert "excluded endpoints: /api/docs" in shown
    assert "missing token=***REDACTED***" in shown
    assert "waiting on missing" in shown
    assert "do-not-print" not in shown


async def test_explain_falls_back_to_the_work_ledger_without_events() -> None:
    work_store = InMemoryWorkStore()
    common = dict(
        run_id=RUN,
        agent_id=AgentId("web-api-agent"),
        target_node_ids=[GraphNodeId("endpoint:a.local GET /api/docs")],
        priority=PriorityScore(total=1.0),
        created_at=NOW,
        updated_at=NOW,
    )
    await work_store.add(
        WorkItem(
            work_id="w-done",
            candidate_key="web.inspect",
            status=WorkStatus.SUCCEEDED,
            outcome_reason="done",
            **common,
        )
    )
    await work_store.add(
        WorkItem(
            work_id="w-blocked",
            candidate_key="api.probe",
            status=WorkStatus.BLOCKED,
            outcome_reason="prerequisite_not_succeeded:w0:failed",
            **common,
        )
    )
    svc = service(
        task_store=await seeded_task_store(task("docs", capability="api")),
        intent_store=InMemoryOperatorIntentStore(),
        bus=None,
        work_store=work_store,
    )

    explanation = await svc.explain(mission_id=MISSION, run_id=RUN)

    assert [item.operation for item in explanation.chosen_actions] == ["web.inspect"]
    assert [item.operation for item in explanation.rejected_candidates] == ["api.probe"]
    assert "prerequisite_not_succeeded" in (explanation.rejected_candidates[0].reason or "")


async def test_from_deps_wires_graph_and_work_evidence() -> None:
    graph_store = InMemoryAttackGraphStore()
    await AttackGraphService(graph_store).upsert_node(
        GraphNodeType.ENDPOINT, key="a.local GET /api/docs", attrs={"in_scope": True}
    )
    task_store = await seeded_task_store(task("docs", capability="api"))
    from types import SimpleNamespace

    deps = SimpleNamespace(
        operator_intent_store=InMemoryOperatorIntentStore(),
        task_graph_store=task_store,
        event_bus=InMemoryEventBus(),
        graph_store=graph_store,
        work_store=InMemoryWorkStore(),
    )
    svc = OperatorIntentService.from_deps(deps)
    assert svc is not None
    await svc.receive("why /api/docs 안 봐?", mission_id=MISSION, run_id=RUN)

    explanation = await svc.explain(mission_id=MISSION)

    assert explanation.endpoint_matches


def test_selector_extraction_helpers_are_exposed() -> None:
    # The subjects helper is deliberately a fixed vocabulary, not free text.
    assert extract_subjects("why candidate / action") == ["candidate", "action"]
