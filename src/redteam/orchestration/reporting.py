"""Read-only terminal projection for the Reporting role."""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import AgentPhase
from redteam.core.ids import ApprovalId, EvidenceId, FindingId, RunId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.work.store import WorkStore


class FindingProjection(BaseModel):
    finding_id: FindingId
    title: str
    severity: str
    verified: bool
    evidence_ids: list[str] = Field(default_factory=list)


class RunEffectiveness(BaseModel):
    successful_work_per_action: float = 0.0
    evidence_per_action: float = 0.0
    verified_findings_per_action: float = 0.0
    dead_end_work_ratio: float = 0.0


class TerminalReportProjection(BaseModel):
    run_id: RunId
    phase: AgentPhase
    termination_reason: str | None
    pending_approval_id: ApprovalId | None = None
    ready_for_release: bool
    actions_executed: int
    finding_count: int
    evidence_count: int
    work_status_counts: dict[str, int] = Field(default_factory=dict)
    findings: list[FindingProjection] = Field(default_factory=list)
    evidence_ids: list[EvidenceId] = Field(default_factory=list)
    integrity_blockers: list[str] = Field(default_factory=list)
    unresolved_hypotheses: list[str] = Field(default_factory=list)
    outstanding_cleanup: list[str] = Field(default_factory=list)
    effectiveness: RunEffectiveness = Field(default_factory=RunEffectiveness)


class ReportingAgent:
    """Creates no facts; it only projects already canonical records."""

    async def project(
        self,
        runtime: RuntimeState,
        *,
        graph: AttackGraphService,
        evidence_store: EvidenceStore,
        work_store: WorkStore,
    ) -> TerminalReportProjection:
        if not runtime.is_terminal:
            raise ValueError("reporting projection requires a terminal runtime")

        integrity = await graph.integrity_report()
        cleanup = await graph.cleanup_report()
        evidence = await evidence_store.list_for_run(runtime.run_id)
        current_evidence_ids = {str(item.evidence_id) for item in evidence}
        findings = [
            item
            for item in await graph.list_findings()
            if not set(item.evidence_ids).isdisjoint(current_evidence_ids)
        ]
        work = await work_store.list_items(run_id=runtime.run_id)
        status_counts = Counter(item.status.value for item in work)
        projected_findings = [
            FindingProjection(
                finding_id=FindingId(item.finding_id),
                title=item.title,
                severity=item.severity,
                verified=item.verified,
                evidence_ids=sorted(set(item.evidence_ids) & current_evidence_ids),
            )
            for item in findings
        ]
        actions = runtime.actions_executed
        terminal_work = sum(status_counts.values())
        dead_ends = sum(
            status_counts.get(status, 0)
            for status in ("failed", "blocked", "stale", "cancelled")
        )
        effectiveness = RunEffectiveness(
            successful_work_per_action=(
                round(status_counts.get("succeeded", 0) / actions, 6) if actions else 0.0
            ),
            evidence_per_action=(round(len(evidence) / actions, 6) if actions else 0.0),
            verified_findings_per_action=(
                round(
                    sum(item.verified for item in projected_findings) / actions,
                    6,
                )
                if actions
                else 0.0
            ),
            dead_end_work_ratio=(round(dead_ends / terminal_work, 6) if terminal_work else 0.0),
        )
        return TerminalReportProjection(
            run_id=runtime.run_id,
            phase=runtime.phase,
            termination_reason=runtime.termination_reason,
            pending_approval_id=runtime.pending_approval_id,
            ready_for_release=(
                runtime.phase is AgentPhase.FINISHED
                and runtime.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
                and not integrity.blocking
                and not integrity.unresolved_hypotheses
                and cleanup.is_clear
            ),
            actions_executed=runtime.actions_executed,
            finding_count=len(projected_findings),
            evidence_count=len(evidence),
            work_status_counts=dict(sorted(status_counts.items())),
            findings=projected_findings,
            evidence_ids=[item.evidence_id for item in evidence],
            integrity_blockers=integrity.blocking,
            unresolved_hypotheses=integrity.unresolved_hypotheses,
            outstanding_cleanup=[str(item.node_id) for item in cleanup.outstanding],
            effectiveness=effectiveness,
        )
