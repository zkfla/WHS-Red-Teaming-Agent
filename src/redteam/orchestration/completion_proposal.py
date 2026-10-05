"""Public completion and unresolved-result contracts.

An unresolved result is readable evidence of a gap, never proof of success.
Detailed gap records live in the existing evidence store; callers keep refs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import new_id
from redteam.orchestration.diagnostics import DecisionDiagnostic


class InvestigationGap(BaseModel):
    code: Literal[
        "insufficient_evidence", "capability_unavailable", "scope_restricted",
        "approval_pending", "provider_failure", "execution_failure",
        "assessment_failure", "budget_exhausted", "stalled", "unknown",
    ] = "unknown"
    description: str = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    required_conditions: list[str] = Field(default_factory=list)
    next_suggestions: list[str] = Field(default_factory=list)


class CompletionProposal(BaseModel):
    # Existing result-claim details remain owned by their existing verifier.
    model_config = ConfigDict(extra="allow")
    outcome: Literal["success", "refuted", "inconclusive", "blocked"]
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    criterion_results: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[InvestigationGap] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def _redacted_reason(cls, value: str) -> str:
        return redact_secret_material(value)

    @property
    def unresolved(self) -> bool:
        return self.outcome in {"inconclusive", "blocked"}


async def validate_unresolved(
    proposal: CompletionProposal,
    *,
    evidence_store: Any,
    runtime: Any,
    target_node_ids: list[str],
) -> list[str]:
    """Check references, without asking a blocked investigation for success proof."""
    if not proposal.reason.strip():
        return ["unresolved investigation requires a reason"]
    ids = list(dict.fromkeys([
        *proposal.evidence_ids,
        *(identity for gap in proposal.gaps for identity in gap.evidence_ids),
    ]))
    permitted_targets = set(target_node_ids)
    for gap in proposal.gaps:
        if permitted_targets and set(gap.target_node_ids) - permitted_targets:
            return ["gap refers to a target outside this investigation"]
    for identity in ids:
        record = await evidence_store.get(identity)
        if record is None:
            return ["unresolved result cites missing evidence"]
        if str(record.run_id) != str(runtime.run_id):
            return ["unresolved result cites another run's evidence"]
        if str(record.engagement_id) != str(runtime.engagement_id):
            return ["unresolved result cites another engagement's evidence"]
        metadata = record.metadata
        if metadata.get("mission_id") and str(metadata["mission_id"]) != str(runtime.mission_id):
            return ["unresolved result cites another mission's evidence"]
        target = metadata.get("target_node_id")
        if target and permitted_targets and str(target) not in permitted_targets:
            return ["unresolved result cites unrelated target evidence"]
    return []


async def record_unresolved(
    *, evidence_store: Any, runtime: Any, proposal: CompletionProposal,
    path: Any, step_id: str | None = None, task_id: str | None = None,
    diagnostic: DecisionDiagnostic | None = None,
) -> str:
    """Store one canonical system note, explicitly separate from completion receipts."""
    payload = redact_secret_material({
        "record_kind": "investigation_unresolved",
        "reported_by": "system" if diagnostic else "model",
        "diagnostic": diagnostic.model_dump(mode="json") if diagnostic else None,
        "mission_id": str(runtime.mission_id) if runtime.mission_id else None,
        "task_id": task_id,
        "path_id": path.path_id,
        "path_version": path.version,
        "path_step_id": step_id,
        "outcome": proposal.outcome,
        "reason": proposal.reason,
        "source_evidence_ids": proposal.evidence_ids,
        "gaps": [gap.model_dump(mode="json") for gap in proposal.gaps] or [{
            "code": "insufficient_evidence" if proposal.outcome == "inconclusive" else "unknown",
            "description": proposal.reason,
        }],
        "claim_supported": False,
    })
    identity = new_id("evidence")
    await evidence_store.append(EvidenceRecord(
        evidence_id=identity,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.investigation_result",
        content_type="application/json",
        digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        summary=proposal.reason[:280],
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    ))
    return str(identity)
