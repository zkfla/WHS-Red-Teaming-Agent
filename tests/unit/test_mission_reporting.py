"""Mission-aware terminal reporting projection and Markdown rendering.

The Mission view is an additive, optional projection over already-canonical
Mission/Task/Evidence records. These tests pin: it is absent by default, it
never guesses between ambiguous Missions, it never dumps raw Task JSON, and
stored reports written before the field existed still load.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from redteam.app.cli.commands.report import _render_markdown
from redteam.app.report_store import load_terminal_report, report_path, save_terminal_report
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding
from redteam.core.contracts.mission import (
    MissionCriterionResult,
    MissionPlan,
    MissionTask,
    Provenance,
)
from redteam.core.enums import AgentPhase, EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EvidenceId, MissionId, RunId, TaskId
from redteam.orchestration.reporting import ReportingAgent, TerminalReportProjection
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.fakes.evidence_store import FakeEvidenceStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mission(
    *,
    mission_id: str = "mission-1",
    engagement_id: str = "eng",
    objective: str = "Assess authorization on the storefront",
    workstreams: list[str] | None = None,
    success_criteria: list[str] | None = None,
    criterion_results: list[MissionCriterionResult] | None = None,
) -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId(mission_id),
        engagement_id=EngagementId(engagement_id),
        objective=objective,
        priorities=workstreams or ["authorization"],
        success_criteria=success_criteria or [],
        criterion_results=criterion_results or [],
        initial_workstreams=workstreams or ["authorization"],
        status=MissionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


def _task(
    task_id: str,
    *,
    capability: str,
    status: TaskStatus = TaskStatus.PENDING,
    title: str | None = None,
    required: bool = True,
    blocking_reason: str | None = None,
    priority: int = 0,
) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId("mission-1"),
        title=title or task_id,
        objective=f"Investigate {capability}",
        status=status,
        priority=priority,
        required=required,
        required_capability=capability,
        blocking_reason=blocking_reason,
        provenance=Provenance(source="test"),
        created_at=NOW,
        updated_at=NOW,
    )


def _runtime(*, mission_id: str | None = None, actions: int = 0) -> RuntimeState:
    return RuntimeState(
        run_id=RunId("run"),
        engagement_id=EngagementId("eng"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        mission_id=MissionId(mission_id) if mission_id else None,
        actions_executed=actions,
    )


async def _project(task_store, *, runtime: RuntimeState | None = None, evidence=None, graph=None):
    return await ReportingAgent().project(
        runtime or _runtime(mission_id="mission-1"),
        graph=graph or AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=evidence or FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
        task_store=task_store,
    )


async def test_mission_view_is_absent_without_a_task_store() -> None:
    projection = await ReportingAgent().project(
        _runtime(),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
    )

    assert projection.mission is None


async def test_release_requires_completed_mission_and_verified_criteria() -> None:
    store = InMemoryTaskGraphStore()
    plan = _mission(success_criteria=["verified evidence for authorization"])
    await store.create_mission(plan)
    await store.create_task(
        _task(
            "authz-1", capability="authorization", status=TaskStatus.BLOCKED,
            blocking_reason="missing second principal",
        )
    )

    blocked = await _project(store)
    assert blocked.termination_reason == "no_actionable_plan"
    assert blocked.ready_for_release is False

    await store.update_mission(
        plan.model_copy(
            update={
                "revision": plan.revision + 1,
                "status": MissionStatus.COMPLETED,
                "criterion_results": [
                    MissionCriterionResult(
                        criterion="verified evidence for authorization", met=True
                    )
                ],
            }
        )
    )
    # A stale or corrupt COMPLETED Mission label cannot hide a blocked
    # required Task from the release gate.
    assert (await _project(store)).ready_for_release is False
    await store.unblock_task(TaskId("authz-1"), now=NOW)
    await store.transition_task(TaskId("authz-1"), TaskStatus.RUNNING, now=NOW)
    await store.transition_task(TaskId("authz-1"), TaskStatus.COMPLETED, now=NOW)
    assert (await _project(store)).ready_for_release is True

    completed = await store.get_mission(plan.mission_id)
    await store.update_mission(
        completed.model_copy(
            update={
                "revision": completed.revision + 1,
                "criterion_results": [
                    MissionCriterionResult(
                        criterion="verified evidence for authorization", met=False
                    )
                ],
            }
        )
    )
    assert (await _project(store)).ready_for_release is False


async def test_report_never_attributes_an_explicit_missing_mission_to_another() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())
    projection = await _project(store, runtime=_runtime(mission_id="missing"))
    assert projection.mission is None
    assert projection.ready_for_release is False


async def test_report_never_attributes_a_foreign_engagements_explicit_mission() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(
        _mission().model_copy(update={"engagement_id": EngagementId("eng_foreign")})
    )
    projection = await _project(store, runtime=_runtime(mission_id="mission-1"))
    assert projection.mission is None
    assert projection.ready_for_release is False


async def test_mission_projection_redacts_free_text_before_report_persistence() -> None:
    store = InMemoryTaskGraphStore()
    plan = _mission().model_copy(update={"objective": "Review token=private-value"})
    await store.create_mission(plan)
    await store.create_task(
        _task(
            "authz-1", capability="authorization", status=TaskStatus.BLOCKED,
            title="Probe password=private-value",
            blocking_reason="missing Bearer private-value",
        )
    )
    projection = await _project(store)
    rendered = projection.model_dump_json()
    assert "private-value" not in rendered
    assert "REDACTED" in rendered


async def test_report_does_not_attribute_another_engagements_evidence() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())
    evidence = FakeEvidenceStore()
    await evidence.append(
        EvidenceRecord(
            evidence_id=EvidenceId("foreign-evidence"),
            run_id=RunId("run"), engagement_id=EngagementId("other-engagement"),
            source_type=EvidenceSourceType.TOOL, source_id="tool",
            content_type="application/json", digest="digest", created_at=NOW,
        )
    )

    report = await _project(store, evidence=evidence)

    assert report.evidence_count == 0
    assert report.evidence_ids == []
    assert report.mission is not None and report.mission.evidence_count == 0


async def test_mission_view_projects_objective_workstreams_and_criteria() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(
        _mission(
            workstreams=["authorization", "reconnaissance"],
            success_criteria=["verified evidence for authorization", "keep secrets out"],
            criterion_results=[
                MissionCriterionResult(
                    criterion="verified evidence for authorization",
                    met=True,
                    evidence_ids=["ev-1"],
                )
            ],
        )
    )
    await store.create_task(
        _task("recon-1", capability="recon", status=TaskStatus.COMPLETED, title="Recon Agent")
    )
    await store.create_task(
        _task(
            "authz-1",
            capability="authorization",
            status=TaskStatus.BLOCKED,
            title="Authorization Probe",
            blocking_reason="awaiting a second principal",
        )
    )
    await store.create_task(
        _task("mobile-1", capability="mobile", status=TaskStatus.RUNNING, required=False)
    )

    graph = AttackGraphService(InMemoryAttackGraphStore())
    await graph.record_finding(
        Finding(
            finding_id="find-1",
            title="IDOR on /orders",
            description="verified",
            severity="high",
            verified=True,
            evidence_ids=["ev-1"],
        )
    )
    evidence = FakeEvidenceStore()
    await evidence.append(
        EvidenceRecord(
            evidence_id=EvidenceId("ev-1"),
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="tool",
            content_type="application/json",
            digest="digest",
            created_at=NOW,
        )
    )

    projection = await _project(store, evidence=evidence, graph=graph)
    mission = projection.mission

    assert mission is not None
    assert mission.objective == "Assess authorization on the storefront"
    assert mission.status == "active"
    assert mission.verified_finding_count == 1
    assert mission.evidence_count == 1

    workstreams = {item.name: item for item in mission.workstreams}
    assert workstreams["authorization"].status == "blocked"
    assert workstreams["authorization"].reason == "awaiting a second principal"
    assert [str(v) for v in workstreams["authorization"].task_ids] == ["authz-1"]
    assert workstreams["reconnaissance"].status == "completed"
    assert workstreams["reconnaissance"].reason is None
    assert workstreams["Mobile"].status == "partial"
    assert workstreams["Mobile"].reason == "0 of 1 tasks completed"

    assert mission.completed_task_count == 1
    assert mission.blocked_task_count == 1
    assert mission.partial_task_count == 1
    markdown = _render_markdown(projection)
    assert "### Blocked tasks" in markdown
    assert "Authorization Probe [blocked]: awaiting a second principal" in markdown

    criteria = {item.criterion: item for item in mission.success_criteria}
    assert criteria["verified evidence for authorization"].met is True
    assert criteria["verified evidence for authorization"].evidence_count == 1
    assert criteria["keep secrets out"].met is False
    assert criteria["keep secrets out"].reason == "not yet evaluated"


async def test_mission_view_does_not_dump_raw_task_json_or_secrets() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())
    await store.create_task(_task("task-1", capability="authorization"))

    projection = await _project(store)
    assert projection.mission is not None
    rendered = projection.mission.model_dump_json()

    # Only the readable projection fields, never the full Task/Mission records.
    assert "provenance" not in rendered
    assert "operator_constraints" not in rendered
    assert "dependency_task_ids" not in rendered
    dumped = json.loads(rendered)
    assert set(dumped) == {
        "mission_id",
        "objective",
        "status",
        "workstreams",
        "tasks",
        "task_status_counts",
        "completed_task_count",
        "partial_task_count",
        "blocked_task_count",
        "success_criteria",
        "verified_finding_count",
        "evidence_count",
    }


async def test_mission_resolution_never_guesses_between_missions() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission(mission_id="mission-1"))
    await store.create_mission(_mission(mission_id="mission-2"))

    ambiguous = await ReportingAgent().project(
        _runtime(mission_id=None),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
        task_store=store,
    )
    assert ambiguous.mission is None

    resolved = await ReportingAgent().project(
        _runtime(mission_id="mission-1"),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
        task_store=store,
    )
    assert resolved.mission is not None
    assert resolved.mission.mission_id == "mission-1"


async def test_single_mission_is_resolved_without_a_runtime_link() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission())

    projection = await ReportingAgent().project(
        _runtime(mission_id=None),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
        task_store=store,
    )

    assert projection.mission is not None
    assert projection.mission.mission_id == "mission-1"


async def test_markdown_renders_the_mission_section() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(
        _mission(
            workstreams=["reconnaissance"],
            success_criteria=["verified evidence for reconnaissance"],
        )
    )
    await store.create_task(
        _task("recon-1", capability="recon", status=TaskStatus.COMPLETED, title="Recon Agent")
    )

    projection = await _project(store)
    markdown = _render_markdown(projection)

    assert "## Mission" in markdown
    assert "Assess authorization on the storefront" in markdown
    assert "[COMPLETED]" in markdown
    assert "### Success criteria" in markdown
    assert "[UNMET]" in markdown


def test_stored_report_written_before_mission_view_still_loads(tmp_path) -> None:
    report = TerminalReportProjection(
        run_id=RunId("run"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        ready_for_release=True,
        actions_executed=0,
        finding_count=0,
        evidence_count=0,
    )
    save_terminal_report(report, reports_dir=tmp_path)

    path = report_path(RunId("run"), reports_dir=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("mission", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_terminal_report(RunId("run"), reports_dir=tmp_path)
    assert loaded is not None
    assert loaded.mission is None
