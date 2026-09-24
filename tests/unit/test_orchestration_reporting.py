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
