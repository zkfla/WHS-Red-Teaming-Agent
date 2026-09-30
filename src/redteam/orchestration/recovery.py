"""Recovery from stored raw results -- never by re-running a tool.

VERIFY or UPDATE can fail *after* the raw ToolResult was preserved on the
evidence record (a crashing verifier, a mapper meeting an output shape it
was not written for). The tool already ran -- its side effects happened --
so recovery replays the stored payload through verification and
normalization again instead of invoking anything.

Whether the tool ran at all is answered from the work ledger and the tool
state first (see is_execution_ambiguous); a missing or stamped result says
nothing by itself. Mapping failures leave an append-only companion record
next to the original through record_mapping_failure -- the original record
is never rewritten.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import HYPOTHESIS_INCONCLUSIVE
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType, Verdict
from redteam.core.ids import EvidenceId, new_id
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.analysis import VerificationRequest
from redteam.orchestration.findings import FindingPromoter, PromotionRequest
from redteam.orchestration.service_status import HYPOTHESIS_STATUS_BY_VERDICT, severity_for

__all__ = [
    "is_execution_ambiguous",
    "reprocess_evidence",
    "record_mapping_failure",
    "record_recovery_note",
    "recovery_report",
]

# Raw payload keys, newest first. Mirrors the reader so the two agree.
_RAW_KEYS = ("tool_result", "raw_tool_result")


def is_execution_ambiguous(result: ToolResult | None) -> bool:
    """True when the caller must check the ledger and tool state first.

    A missing result, or one stamped with an orchestration-side exception,
    says nothing about whether the tool ran -- reprocessing would be
    guessing. Returns False only when a real tool payload is in hand.
    """
    if result is None:
        return True
    return bool(
        result.metadata.get("orchestration_exception")
        or result.metadata.get("verification_exception")
    )


def _stored_raw(record: EvidenceRecord) -> Any | None:
    for key in _RAW_KEYS:
        raw = record.metadata.get(key)
        if raw is not None:
            return raw
    return None


async def reprocess_evidence(
    *,
    evidence_id: str,
    run_id: str,
    evidence_store: Any,
    graph_service: Any,
    verifier: Any,
    result_mappers: Any | None = None,
) -> dict:
    """Replay one preserved result through meaning review and the graph.

    This is what the root engine's TurnDecision mode "recover_evidence"
    calls after a verification or normalization failure: the tool already
    ran, so the only inputs are the raw ToolResult dict and the original
    proposal and plan step, all preserved on the evidence record itself.
    The tool gateway is never touched.

    The verdict comes only from the passed verifier (the structured LLM
    reviewer): no deterministic verdict is produced here, and a negative
    result is refuted only through that meaning review with the raw
    payload intact. The outcome lands back in the graph -- hypothesis
    resolution, an optional promoted finding, replayed mapper mutations --
    and as an append-only companion note ("recovery_result") next to the
    original record, which is never rewritten.

    Status is one of "ok" / "not_found" / "run_mismatch" / "no_raw" /
    "invalid_raw" / "no_proposal" / "invalid_proposal". Lookup failures
    ("not_found", "run_mismatch", "no_raw") write nothing; everything
    from "invalid_raw" on leaves a companion note behind.
    """
    record = await evidence_store.get(evidence_id)
    if record is None:
        return {
            "status": "not_found",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
        }
    if str(record.run_id) != str(run_id):
        return {
            "status": "run_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "actual_run_id": str(record.run_id),
        }
    raw = _stored_raw(record)
    if raw is None:
        return {
            "status": "no_raw",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
        }
    try:
        result = ToolResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a corrupt payload is data, not a crash
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing failed: stored raw result unreadable ("
            + type(exc).__name__
            + ")",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
        )
        return {
            "status": "invalid_raw",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "error": type(exc).__name__,
            "companion_evidence_id": str(companion.evidence_id),
        }
    stored_proposal = record.metadata.get("proposal")
    if not isinstance(stored_proposal, dict):
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing failed: no stored proposal to replay from",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
        )
        return {
            "status": "no_proposal",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    try:
        proposal = ActionProposal.model_validate(stored_proposal)
    except Exception as exc:  # noqa: BLE001 - a corrupt proposal is data, not a crash
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing failed: stored proposal unreadable ("
            + type(exc).__name__
            + ")",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
        )
        return {
            "status": "invalid_proposal",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "error": type(exc).__name__,
            "companion_evidence_id": str(companion.evidence_id),
        }
    step = None
    step_dump = record.metadata.get("plan_step")
    step_degraded = False
    if isinstance(step_dump, dict):
        try:
            step = PlanStep.model_validate(step_dump)
        except Exception:  # noqa: BLE001 - replay continues without step context
            step_degraded = True
    expectation = step.expectation if step is not None else None
    assessment = await verifier.verify(
        VerificationRequest(
            proposal=proposal,
            result=result,
            expectation=expectation,
            source_refs=[str(record.evidence_id), str(result.invocation_id)],
        )
    )
    verdict, reason = assessment.verdict, assessment.reason
    provenance = Provenance(
        step=0,
        source="recovery:tool:" + str(result.tool_id),
        evidence_id=str(record.evidence_id),
        run_id=str(record.run_id),
        confidence=Confidence.MEASURED,
    )
    hypothesis_status = ""
    if proposal.hypothesis_id is not None:
        if step is not None and step.assessment_kind != "security_check":
            hypothesis_status = HYPOTHESIS_INCONCLUSIVE
        else:
            hypothesis_status = HYPOTHESIS_STATUS_BY_VERDICT[verdict]
        await graph_service.resolve_hypothesis(
            str(proposal.hypothesis_id), status=hypothesis_status
        )
    finding_id = None
    if verdict is Verdict.VIOLATION and proposal.hypothesis_id is not None:
        existing_claims = {
            str(item.metadata.get("claim_key"))
            for item in await graph_service.list_findings()
            if item.metadata.get("claim_key")
        }
        assessment_kind = step.assessment_kind if step is not None else "observation"
        severity = severity_for(step) if step is not None else "medium"
        promotion = FindingPromoter().promote(
            PromotionRequest(
                verdict=verdict,
                reason=reason,
                evidence_ids=[str(record.evidence_id)],
                hypothesis_id=str(proposal.hypothesis_id),
                hypothesis_status=hypothesis_status,
                target_node_id=str(node_id_for_target(proposal.target)),
                capability=proposal.capability,
                operation=proposal.operation,
                agent_id=str(proposal.agent_id),
                action_id=str(proposal.action_id),
                reviewer_id=assessment.reviewer_id,
                source_refs=list(assessment.source_refs),
                severity=severity,
                assessment_kind=assessment_kind,
            ),
            existing_claims=existing_claims,
        )
        if promotion.finding is not None:
            await graph_service.record_finding(
                promotion.finding,
                hypothesis_id=str(proposal.hypothesis_id),
                about=node_id_for_target(proposal.target),
                provenance=provenance,
            )
            finding_id = str(promotion.finding.finding_id)
        else:
            verdict = Verdict.UNKNOWN
            reason = (
                "violation was not promoted to a Finding: " + promotion.refused_reason
            )
    mutation_summary = {"nodes": 0, "edges": 0}
    mapper_failure = None
    if result_mappers is not None and result.success:
        mutation = result_mappers.map_result(
            tool_id=result.tool_id,
            target=proposal.target,
            arguments=dict(proposal.arguments),
            result=result,
        )
        failure = result_mappers.last_failure
        mutation_summary = {"nodes": len(mutation.nodes), "edges": len(mutation.edges)}
        if failure is not None:
            mapper_failure = {
                "tool_id": failure.tool_id,
                "mapper": failure.mapper,
                "reason": failure.reason,
            }
        for node_mutation in mutation.nodes:
            observed_facts = dict(node_mutation.facts)
            if node_mutation.observations:
                observed_facts["observations"] = [
                    {**item, "evidence_id": str(record.evidence_id)}
                    for item in node_mutation.observations
                ]
            await graph_service.upsert_node(
                node_mutation.node_type,
                node_id=node_mutation.node_id,
                attrs=node_mutation.attrs,
                facts={
                    **observed_facts,
                    "evidence_ids": [str(record.evidence_id)],
                },
                labels=node_mutation.labels,
                provenance=provenance,
            )
        for edge_mutation in mutation.edges:
            await graph_service.link(
                edge_mutation.edge_type,
                edge_mutation.source_id,
                edge_mutation.target_id,
                edge_mutation.properties,
                provenance=provenance,
            )
    parts = [
        "reprocessed " + str(record.evidence_id) + ": verdict=" + verdict.value,
        "hypothesis="
        + (
            str(proposal.hypothesis_id) + "->" + hypothesis_status
            if proposal.hypothesis_id is not None
            else "none"
        ),
        "finding=" + (finding_id or "none"),
        "mutation nodes=" + str(mutation_summary["nodes"])
        + " edges="
        + str(mutation_summary["edges"]),
    ]
    if step_degraded:
        parts.append("step context unreadable, replayed degraded")
    if mapper_failure is not None:
        parts.append("mapper_failure=" + str(mapper_failure["reason"]))
    companion = await record_recovery_note(
        evidence_store=evidence_store,
        run_id=record.run_id,
        engagement_id=record.engagement_id,
        source_evidence_id=str(record.evidence_id),
        note="; ".join(parts),
        record_kind="recovery_result",
        action_id=proposal.action_id,
        invocation_id=result.invocation_id,
        hypothesis_id=proposal.hypothesis_id,
    )
    return {
        "status": "ok",
        "evidence_id": str(evidence_id),
        "requested_run_id": str(run_id),
        "verdict": verdict.value,
        "reason": reason,
        "hypothesis_id": str(proposal.hypothesis_id)
        if proposal.hypothesis_id is not None
        else None,
        "hypothesis_status": hypothesis_status,
        "finding_id": finding_id,
        "mutation": mutation_summary,
        "mapper_failure": mapper_failure,
        "companion_evidence_id": str(companion.evidence_id),
    }
async def record_mapping_failure(
    *,
    evidence_store: Any,
    run_id: Any,
    engagement_id: Any,
    source_evidence_id: str | None,
    tool_id: str,
    mapper: str,
    reason: str,
    action_id: Any | None = None,
    invocation_id: Any | None = None,
    hypothesis_id: Any | None = None,
) -> EvidenceRecord:
    """Append a companion record for a mapper that choked on real output.

    The original evidence record is never touched: this is a new SYSTEM
    record pointing back at it through "source_evidence_id", so the
    failure stays traceable without rewriting history.
    """
    payload = {
        "record_kind": "mapper_failure",
        "source_evidence_id": source_evidence_id,
        "tool_id": tool_id,
        "mapper": mapper,
        "reason": reason,
    }
    note = EvidenceRecord(
        evidence_id=EvidenceId(new_id("ev")),
        run_id=run_id,
        engagement_id=engagement_id,
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.recovery",
        action_id=action_id,
        invocation_id=invocation_id,
        hypothesis_id=hypothesis_id,
        content_type="application/json",
        artifact_ref=None,
        digest=digest_payload(payload),
        summary="result mapper " + mapper + " failed for " + tool_id + " (" + reason + ")",
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await evidence_store.append(note)
    return note


async def record_recovery_note(
    *,
    evidence_store: Any,
    run_id: Any,
    engagement_id: Any,
    source_evidence_id: str | None,
    note: str,
    record_kind: str = "recovery_note",
    action_id: Any | None = None,
    invocation_id: Any | None = None,
    hypothesis_id: Any | None = None,
) -> EvidenceRecord:
    """Append a free-form companion note next to an existing record."""
    payload = {
        "record_kind": record_kind,
        "source_evidence_id": source_evidence_id,
        "note": note,
    }
    record = EvidenceRecord(
        evidence_id=EvidenceId(new_id("ev")),
        run_id=run_id,
        engagement_id=engagement_id,
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.recovery",
        action_id=action_id,
        invocation_id=invocation_id,
        hypothesis_id=hypothesis_id,
        content_type="application/json",
        artifact_ref=None,
        digest=digest_payload(payload),
        summary=note[:280],
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await evidence_store.append(record)
    return record


async def recovery_report(*, evidence_store: Any, run_id: str) -> dict:
    """Summarize how recoverable this run's evidence is.

    Counts what a later reprocessing pass would meet: records carrying a
    raw payload, records with none, mapper-failure companions, and results
    stamped with a verification exception. Read-only; changes nothing.
    """
    records = await evidence_store.list_for_run(run_id)
    kinds: dict[str, int] = {}
    with_raw = 0
    verification_failures = 0
    for record in records:
        kind = record.metadata.get("record_kind")
        if isinstance(kind, str) and kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        raw = record.metadata.get("tool_result") or record.metadata.get("raw_tool_result")
        if raw is None:
            continue
        with_raw += 1
        if isinstance(raw, dict):
            nested = raw.get("metadata") or {}
            if isinstance(nested, dict) and nested.get("verification_exception"):
                verification_failures += 1
    return {
        "run_id": str(run_id),
        "total_evidence": len(records),
        "with_raw": with_raw,
        "missing_raw": len(records) - with_raw,
        "record_kinds": kinds,
        "mapping_failures": kinds.get("mapper_failure", 0),
        "verification_failures": verification_failures,
    }
