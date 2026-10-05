from datetime import datetime, timezone

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding
from redteam.core.enums import AgentPhase, EvidenceSourceType
from redteam.core.ids import AgentId, EngagementId, EvidenceId, GraphNodeId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from tests.fakes.evidence_store import FakeEvidenceStore


async def test_reporting_refuses_a_nonterminal_runtime() -> None:
    with pytest.raises(ValueError, match="terminal runtime"):
        await ReportingAgent().project(
            RuntimeState(run_id=RunId("run"), engagement_id=EngagementId("eng")),
            graph=AttackGraphService(InMemoryAttackGraphStore()),
            evidence_store=FakeEvidenceStore(),
            work_store=InMemoryWorkStore(),
        )


async def test_empty_terminal_projection_is_releaseable_and_read_only() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
            termination_reason="no_actionable_plan",
        ),
        graph=graph,
        evidence_store=FakeEvidenceStore(),
        work_store=InMemoryWorkStore(),
    )

    assert projection.ready_for_release is True
    assert projection.finding_count == 0
    assert await graph.list_findings() == []


async def test_reporting_projects_ledger_derived_effectiveness() -> None:
    store = InMemoryWorkStore()
    now = datetime.now(timezone.utc)
    for index, status in enumerate((WorkStatus.SUCCEEDED, WorkStatus.FAILED)):
        item = WorkItem(
            work_id=f"work-{index}",
            run_id=RunId("run"),
            candidate_key=f"candidate-{index}",
            agent_id=AgentId("agent"),
            target_node_ids=[GraphNodeId("host:a.local")],
            priority=PriorityScore(total=1),
            status=status,
            created_at=now,
            updated_at=now,
        )
        await store.add(item)

    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
            actions_executed=2,
        ),
        graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(),
        work_store=store,
    )

    assert projection.effectiveness.successful_work_per_action == 0.5
    assert projection.effectiveness.dead_end_work_ratio == 0.5


async def test_reporting_excludes_findings_supported_only_by_another_run() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    await graph.record_finding(
        Finding(
            finding_id="find-current",
            title="current",
            description="current run",
            severity="medium",
            verified=True,
            evidence_ids=["ev-current", "ev-other"],
        )
    )
    await graph.record_finding(
        Finding(
            finding_id="find-other",
            title="other",
            description="other run",
            severity="medium",
            verified=True,
            evidence_ids=["ev-other"],
        )
    )
    evidence_store = FakeEvidenceStore()
    await evidence_store.append(
        EvidenceRecord(
            evidence_id=EvidenceId("ev-current"),
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="tool",
            content_type="application/json",
            digest="digest",
            created_at=datetime.now(timezone.utc),
        )
    )

    projection = await ReportingAgent().project(
        RuntimeState(
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            phase=AgentPhase.FINISHED,
        ),
        graph=graph,
        evidence_store=evidence_store,
        work_store=InMemoryWorkStore(),
    )

    assert [item.finding_id for item in projection.findings] == ["find-current"]
    assert projection.findings[0].evidence_ids == ["ev-current"]


async def test_unresolved_store_detail_and_read_status_survive_saved_report(tmp_path):
    from types import SimpleNamespace

    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.orchestration.completion_proposal import (
        CompletionProposal,
        InvestigationGap,
        record_unresolved,
    )
    from redteam.orchestration.evidence_access import EvidenceReadRequest, read_for_runtime

    runtime = RuntimeState(
        run_id="run", engagement_id="eng", mission_id="mission",
        phase=AgentPhase.FINISHED, termination_reason="investigation_held",
        model_requests_used=5, model_tokens_used=19, model_usage_unknown=2,
    )
    store = FakeEvidenceStore()
    identity = await record_unresolved(
        evidence_store=store, runtime=runtime,
        proposal=CompletionProposal(
            outcome="inconclusive", reason="Need another account; token=private-value",
            gaps=[InvestigationGap(
                code="insufficient_evidence", description="Comparison remains unverified",
                unresolved_questions=["Does the second account see the same record?"],
                required_conditions=["Obtain authorized second account"],
                next_suggestions=["Compare authorized session results"],
            )],
        ), path=SimpleNamespace(path_id="path", version=2),
        step_id="step", task_id="task",
    )
    runtime.unresolved_evidence_ids = [identity]
    await store.append(EvidenceRecord(
        evidence_id="raw-evidence", run_id="run", engagement_id="eng",
        source_type=EvidenceSourceType.TOOL, source_id="tool",
        content_type="application/json", digest="digest",
        created_at=datetime.now(timezone.utc),
        metadata={"tool_result": {"output": {"body": "opaque-raw-not-for-report"}}},
    ))
    await read_for_runtime(store, runtime, EvidenceReadRequest(
        evidence_id="raw-evidence", offset=3, limit=8, query="opaque",
    ), delegation_id="delegation")
    await read_for_runtime(store, runtime, EvidenceReadRequest(evidence_id="missing"))
    # A foreign engagement's note must not supply detail to this report.
    note = await store.get(identity)
    await store.append(note.model_copy(update={
        "evidence_id": "foreign-note", "engagement_id": "other-engagement",
    }))
    runtime = RuntimeState.model_validate_json(runtime.model_dump_json())
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=store, work_store=InMemoryWorkStore(),
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored.run_outcome == "unresolved" and not restored.ready_for_release
    assert restored.unresolved_evidence_ids == [identity]
    assert [item.evidence_id for item in restored.unresolved_details] == [identity]
    detail = restored.unresolved_details[0]
    assert detail.reported_by == "model" and not detail.claim_supported
    assert (detail.mission_id, detail.task_id, detail.path_id, detail.path_step_id) == (
        "mission", "task", "path", "step",
    )
    assert detail.gaps[0].required_conditions == ["Obtain authorized second account"]
    assert [(entry.status, entry.delegation_id) for entry in restored.evidence_reads] == [
        ("ok", "delegation"), ("not_found", None),
    ]
    assert restored.evidence_reads[0].offset == 3 and restored.evidence_reads[0].query == "opaque"
    assert (restored.model_requests_used, restored.model_tokens_used,
            restored.model_usage_unknown) == (5, 19, 2)
    rendered = restored.model_dump_json() + _render_markdown(restored)
    assert "private-value" not in rendered and "opaque-raw-not-for-report" not in rendered
    assert "Obtain authorized second account" in rendered and "not_found" in rendered


async def test_recovered_planning_failure_history_remains_in_persisted_report(tmp_path):
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.orchestration.diagnostics import DecisionDiagnostic

    diagnostic = DecisionDiagnostic(
        code="llm_provider_rate_limited", stage="path_review", kind="transport",
        summary="Provider request failed", attempts=1, retryable=True,
        provider_status=429, provider_code="rate_limit", retry_after=2.5,
    )
    runtime = RuntimeState(
        run_id="run", engagement_id="eng", phase=AgentPhase.FINISHED,
        termination_reason="investigation_held", planning_diagnostic=None,
        planning_diagnostic_history=[diagnostic],
    )
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(), work_store=InMemoryWorkStore(),
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored.model_dump(mode="json").get("planning_diagnostic_history") == [
        diagnostic.model_dump(mode="json"),
    ]
    text = _render_markdown(restored)
    assert "rate_limit" in text and "429" in text and "2.5" in text
