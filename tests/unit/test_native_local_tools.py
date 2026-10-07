"""Native-loop runtime-local helpers: registry wiring and bounded behavior.

Uses local fakes plus the real in-memory mission/task/path stores; no
existing fakes or production code are touched. The unresolved settlement
paths run the real canonical worker without any model call.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.local_tools import LocalToolRegistry, LocalToolResult
from redteam.orchestration.native_local_tools import (
    EVIDENCE_READ_TOOL_ID,
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
    goal_settle_handler,
    native_local_tool_contributions,
    task_settle_handler,
)
from redteam.orchestration.paths import InMemoryPathStore
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

RUN = "run-native-local"
ENGAGEMENT = "eng-native-local"
MISSION = "mission-native-local"
TASK = "task-native-local"
NOW = datetime.now(timezone.utc)


class _EvidenceStore:
    def __init__(self, records: tuple[Any, ...] = ()) -> None:
        self.records: list[Any] = list(records)
        self.appended: list[Any] = []

    async def get(self, evidence_id: Any) -> Any | None:
        wanted = str(evidence_id)
        for record in (*self.records, *self.appended):
            if str(getattr(record, "evidence_id", "")) == wanted:
                return record
        return None

    async def append(self, record: Any) -> Any:
        self.appended.append(record)
        return record

    async def list_for_run(self, run_id: Any) -> list[Any]:
        return []


class _WorkStore:
    async def list_items(self, **kwargs: Any) -> list[Any]:
        return []


def _record(evidence_id: str, *, run_id: str = RUN, raw: Any | None = None) -> Any:
    return SimpleNamespace(
        evidence_id=evidence_id,
        run_id=run_id,
        engagement_id=ENGAGEMENT,
        summary="stored tool output",
        metadata={} if raw is None else {
            "tool_result": {"output": raw},
            "mission_id": MISSION,
        },
    )


def _runtime(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": RUN,
        "engagement_id": ENGAGEMENT,
        "mission_id": MISSION,
        "active_task_id": TASK,
        "evidence_reads": [],
        "unresolved_evidence_ids": [],
        "delegations": {},
        "mission_status": None,
        "pending_approval_id": None,
        "step_count": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(runtime: Any, **overrides: Any) -> Any:
    from redteam.orchestration.local_tools import LocalToolContext

    base: dict[str, Any] = {"deps": None, "runtime": runtime, "purpose": "test"}
    base.update(overrides)
    return LocalToolContext(**base)


def _mission() -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId(MISSION),
        engagement_id=EngagementId(ENGAGEMENT),
        objective="confirm the login session behavior",
        status=MissionStatus.ACTIVE,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test:native-local"),
    )


def _task(**overrides: Any) -> MissionTask:
    base: dict[str, Any] = {
        "task_id": TaskId(TASK),
        "mission_id": MissionId(MISSION),
        "title": "confirm the login session",
        "objective": "confirm the login session behavior",
        "status": TaskStatus.RUNNING,
        "required": True,
        "provenance": Provenance(source="test:native-local"),
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return MissionTask(**base)


def test_runtime_outcome_defaults_to_none_and_survives_asdict() -> None:
    result = LocalToolResult(ok=True, payload={"a": 1})
    assert result.runtime_outcome is None
    assert asdict(result)["runtime_outcome"] is None


def test_runtime_outcome_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        LocalToolResult(ok=True, runtime_outcome="task_done")


def test_runtime_outcome_needs_success() -> None:
    with pytest.raises(ValueError):
        LocalToolResult(ok=False, error="nope", runtime_outcome="task_completed")


async def test_contributions_register_without_tool_name_branches() -> None:
    contributions = native_local_tool_contributions()
    assert len(contributions) == 3
    assert len({item.contribution_id for item in contributions}) == 3
    assert all(item.kind == "runtime_tool" for item in contributions)
    registry = LocalToolRegistry()
    for item in contributions:
        descriptor, handler = item.factory(None)
        registry.register(descriptor, handler)
    assert {item.tool_id for item in registry.descriptors()} == {
        EVIDENCE_READ_TOOL_ID,
        TASK_SETTLE_TOOL_ID,
        GOAL_SETTLE_TOOL_ID,
    }
    assert registry.get("no.such.tool") is None
    from redteam.orchestration.local_tools import LocalToolContext as Ctx

    runtime = _runtime()
    store = _EvidenceStore()
    deps = SimpleNamespace(evidence_store=store)
    unknown = await registry.call(
        "no.such.tool", {}, Ctx(deps=deps, runtime=runtime)
    )
    assert unknown.ok is False
    missing = await registry.call(
        EVIDENCE_READ_TOOL_ID, {}, Ctx(deps=deps, runtime=runtime)
    )
    assert missing.ok is False
    assert missing.runtime_outcome is None


async def test_evidence_read_returns_bounded_window_and_retains() -> None:
    raw = {"lines": ["login ok"] * 2000}
    store = _EvidenceStore((_record("ev-1", raw=raw),))
    runtime = _runtime()
    deps = SimpleNamespace(evidence_store=store)
    from redteam.orchestration.native_local_tools import evidence_read_handler

    result = await evidence_read_handler(
        {"evidence_id": "ev-1", "limit": 200}, _context(runtime, deps=deps)
    )
    assert result.ok is True
    assert result.runtime_outcome is None
    assert result.refs == ("ev-1",)
    assert result.payload["status"] == "ok"
    assert result.payload["truncated"] is True
    assert len(result.payload["text"]) <= 200
    assert len(runtime.evidence_reads) == 1
    for _ in range(12):
        await evidence_read_handler(
            {"evidence_id": "ev-1", "limit": 10}, _context(runtime, deps=deps)
        )
    assert len(runtime.evidence_reads) <= 10


async def test_evidence_read_masks_foreign_and_missing_records() -> None:
    store = _EvidenceStore((_record("ev-9", run_id="other-run"),))
    runtime = _runtime()
    deps = SimpleNamespace(evidence_store=store)
    from redteam.orchestration.native_local_tools import evidence_read_handler

    foreign = await evidence_read_handler(
        {"evidence_id": "ev-9"}, _context(runtime, deps=deps)
    )
    assert foreign.ok is False
    assert foreign.runtime_outcome is None
    assert "another run" in str(foreign.error)
    missing = await evidence_read_handler(
        {"evidence_id": "ev-missing"}, _context(runtime, deps=deps)
    )
    assert missing.ok is False
    oversized = await evidence_read_handler(
        {"evidence_id": "ev-9", "limit": 10**9}, _context(runtime, deps=deps)
    )
    assert oversized.ok is False


async def test_task_settle_unresolved_records_canonical_gap() -> None:
    tasks = InMemoryTaskGraphStore()
    await tasks.create_mission(_mission())
    await tasks.create_task(_task())
    paths = InMemoryPathStore()
    await coordinator_for(tasks, paths).bind(RUN, MISSION, ENGAGEMENT)
    store = _EvidenceStore((_record("ev-1", raw={"output": "timeout"}),))
    runtime = _runtime()
    deps = SimpleNamespace(
        evidence_store=store,
        task_graph_store=tasks,
        path_store=paths,
        work_store=_WorkStore(),
        graph_store=None,
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    result = await task_settle_handler(
        {
            "outcome": "inconclusive",
            "reason": "login flow timed out; need another window",
            "evidence_ids": ["ev-1"],
        },
        _context(runtime, deps=deps),
    )
    assert result.ok is True
    assert result.runtime_outcome == "task_blocked"
    assert result.refs[0] == "ev-1"
    assert len(result.payload["unresolved_evidence_ids"]) == 1
    assert len(store.appended) == 1
    assert store.appended[0].metadata["record_kind"] == "investigation_unresolved"
    assert store.appended[0].metadata["path_id"] is None
    settled = await tasks.get_task(TaskId(TASK))
    assert settled is not None and settled.status is TaskStatus.BLOCKED


async def test_goal_settle_unresolved_finishes_investigation() -> None:
    tasks = InMemoryTaskGraphStore()
    await tasks.create_mission(_mission())
    await tasks.create_task(_task())
    paths = InMemoryPathStore()
    await coordinator_for(tasks, paths).bind(RUN, MISSION, ENGAGEMENT)
    store = _EvidenceStore((_record("ev-1", raw={"output": "timeout"}),))
    runtime = _runtime()
    deps = SimpleNamespace(
        evidence_store=store,
        task_graph_store=tasks,
        path_store=paths,
        work_store=_WorkStore(),
        graph_store=None,
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    result = await goal_settle_handler(
        {
            "outcome": "blocked",
            "reason": "scope window closed with the login flow unproven",
            "evidence_ids": ["ev-1"],
        },
        _context(runtime, deps=deps),
    )
    assert result.ok is True
    assert result.runtime_outcome == "investigation_finished"
    mission = await tasks.get_mission(MissionId(MISSION))
    assert mission is not None and mission.status is MissionStatus.BLOCKED


async def test_settle_rejects_without_active_task_or_mission() -> None:
    deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    idle = await task_settle_handler(
        {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
        _context(_runtime(active_task_id=""), deps=deps),
    )
    assert idle.ok is False
    assert idle.runtime_outcome is None
    nomission = await goal_settle_handler(
        {"outcome": "success", "reason": "done"},
        _context(_runtime(mission_id=None), deps=deps),
    )
    assert nomission.ok is False
    assert nomission.runtime_outcome is None
    bad = await task_settle_handler(
        {"outcome": "success"},
        _context(_runtime(), deps=deps),
    )
    assert bad.ok is False
    assert bad.runtime_outcome is None


async def test_settle_maps_worker_errors_without_outcome(monkeypatch: Any) -> None:
    import redteam.orchestration.semantic_completion as worker

    async def failing(*args: Any, **kwargs: Any) -> list[str]:
        return ["task completion needs related stored evidence"]

    monkeypatch.setattr(worker, "complete_task", failing)
    deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    result = await task_settle_handler(
        {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
        _context(_runtime(), deps=deps),
    )
    assert result.ok is False
    assert result.runtime_outcome is None
    assert "stored evidence" in str(result.error)


async def test_settle_maps_worker_success_to_canonical_outcome(
    monkeypatch: Any,
) -> None:
    import redteam.orchestration.semantic_completion as worker

    seen: dict[str, Any] = {}

    async def succeeding(*args: Any, **kwargs: Any) -> list[str]:
        seen["path"] = args[1]
        seen["budget"] = kwargs.get("budget")
        return []

    monkeypatch.setattr(worker, "complete_task", succeeding)
    monkeypatch.setattr(worker, "complete_goal", succeeding)
    budget = SimpleNamespace(used=0, tokens_used=0, unknown_usage_count=0)
    deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    task_ok = await task_settle_handler(
        {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
        _context(_runtime(), deps=deps, budget=budget),
    )
    assert task_ok.ok is True
    assert task_ok.runtime_outcome == "task_completed"
    assert task_ok.refs == ("ev-1",)
    assert seen["path"] is None
    assert seen["budget"] is budget
    goal_ok = await goal_settle_handler(
        {"outcome": "refuted", "reason": "clean"},
        _context(_runtime(), deps=deps, budget=budget),
    )
    assert goal_ok.ok is True
    assert goal_ok.runtime_outcome == "goal_achieved"


async def test_evidence_read_runtime_failure_keeps_safe_structured_diagnostic() -> None:
    from redteam.orchestration.native_local_tools import evidence_read_handler

    class _FailingStore(_EvidenceStore):
        async def get(self, evidence_id: Any) -> Any | None:
            raise RuntimeError("store-secret-boom")

    runtime = _runtime()
    deps = SimpleNamespace(evidence_store=_FailingStore((_record("ev-1"),)))
    result = await evidence_read_handler(
        {"evidence_id": "ev-1"}, _context(runtime, deps=deps)
    )
    assert result.ok is False
    assert result.runtime_outcome is None
    assert "evidence_read_failed" in str(result.error)
    assert "store-secret-boom" not in str(result.error)
    diagnostic = result.payload.get("diagnostic", {})
    assert diagnostic.get("code") == "evidence_read_failed"
    assert "store-secret-boom" not in str(result.payload)


async def test_settle_runtime_failure_keeps_safe_structured_diagnostic(
    monkeypatch: Any,
) -> None:
    import redteam.orchestration.semantic_completion as worker

    async def exploding(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("settle-secret-boom")

    monkeypatch.setattr(worker, "complete_task", exploding)
    deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    result = await task_settle_handler(
        {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
        _context(_runtime(), deps=deps),
    )
    assert result.ok is False
    assert result.runtime_outcome is None
    assert "settlement_failed" in str(result.error)
    assert "settle-secret-boom" not in str(result.error)
    assert result.payload.get("outcome") == "success"
    diagnostic = result.payload.get("diagnostic", {})
    assert diagnostic.get("code") == "settlement_failed"
    assert "settle-secret-boom" not in str(result.payload)


async def test_evidence_read_validation_keeps_safe_field_diagnostics() -> None:
    from redteam.orchestration.native_local_tools import evidence_read_handler

    runtime = _runtime()
    deps = SimpleNamespace(evidence_store=_EvidenceStore())
    missing = await evidence_read_handler({}, _context(runtime, deps=deps))
    assert missing.ok is False
    assert missing.runtime_outcome is None
    assert "evidence_id" in str(missing.error)
    bad_type = await evidence_read_handler(
        {"evidence_id": "ev-1", "limit": "not-a-number-secret-value"},
        _context(runtime, deps=deps),
    )
    assert bad_type.ok is False
    assert "limit" in str(bad_type.error)
    assert "not-a-number-secret-value" not in str(bad_type.error)
    out_of_range = await evidence_read_handler(
        {"evidence_id": "ev-1", "limit": 10**9},
        _context(runtime, deps=deps),
    )
    assert out_of_range.ok is False
    assert "limit" in str(out_of_range.error)
    assert "1000000000" not in str(out_of_range.error)
    unknown_keys_stay_unnamed = await evidence_read_handler(
        {"mystery_field_name": "mystery-value"},
        _context(runtime, deps=deps),
    )
    assert unknown_keys_stay_unnamed.ok is False
    assert "mystery_field_name" not in str(unknown_keys_stay_unnamed.error)
    assert "mystery-value" not in str(unknown_keys_stay_unnamed.error)


async def test_settle_validation_keeps_safe_field_diagnostics() -> None:
    deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    result = await task_settle_handler(
        {"outcome": "bogus-outcome-secret-value"},
        _context(_runtime(), deps=deps),
    )
    assert result.ok is False
    assert result.runtime_outcome is None
    assert "literal_error" in str(result.error)
    assert "bogus-outcome-secret-value" not in str(result.error)


async def test_native_handlers_propagate_reasoning_terminal_signals(
    monkeypatch: Any,
) -> None:
    import redteam.orchestration.semantic_completion as worker
    from redteam.orchestration import native_local_tools as native
    from redteam.orchestration.cognition import DecisionFailure, ReasoningHalted

    async def halted_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ReasoningHalted("analysis_run_stopped_or_paused")

    monkeypatch.setattr(native, "read_for_runtime", halted_read)
    runtime = _runtime()
    deps = SimpleNamespace(evidence_store=_EvidenceStore())
    with pytest.raises(ReasoningHalted):
        await native.evidence_read_handler(
            {"evidence_id": "ev-1"}, _context(runtime, deps=deps)
        )

    async def failed_settle(*args: Any, **kwargs: Any) -> list[str]:
        raise DecisionFailure("native", "budget", 3, "spent")

    monkeypatch.setattr(worker, "complete_task", failed_settle)
    settle_deps = SimpleNamespace(
        evidence_store=_EvidenceStore(),
        task_graph_store=InMemoryTaskGraphStore(),
        path_store=InMemoryPathStore(),
        work_store=_WorkStore(),
        llm_client=object(),
        settings=SimpleNamespace(decision_max_attempts=2),
        planning_provider=None,
        coordinator=None,
    )
    with pytest.raises(DecisionFailure):
        await task_settle_handler(
            {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
            _context(_runtime(), deps=settle_deps),
        )

    async def halted_settle(*args: Any, **kwargs: Any) -> list[str]:
        raise ReasoningHalted("analysis_run_stopped_or_paused")

    monkeypatch.setattr(worker, "complete_task", halted_settle)
    with pytest.raises(ReasoningHalted):
        await task_settle_handler(
            {"outcome": "success", "reason": "done", "evidence_ids": ["ev-1"]},
            _context(_runtime(), deps=settle_deps),
        )


def test_builtin_registration_includes_native_local_tools() -> None:
    from redteam.app.runtime_components import _BuiltinRuntimeExtension

    declarations = _BuiltinRuntimeExtension().redteam_extensions()
    table = {item.extension_id: item for item in declarations}
    assert "builtin.native-local-tools" in table
    assert "builtin.graph-recording" in table
    assert len(table["builtin.native-local-tools"].contributions) == 3
    assert {item.contribution_id for item in table["builtin.native-local-tools"].contributions} == {
        "evidence-read",
        "task-settle",
        "goal-settle",
    }
