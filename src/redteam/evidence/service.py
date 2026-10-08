"""Evidence convenience API. Owned by Control Team.

Builds on ``redteam.core.convert.tool_result_to_evidence`` (the
mechanical ToolResult -> EvidenceRecord conversion PM defines once so
it isn't duplicated between this service and orchestration's
``verify_node``).

``record_blocked_action`` covers the other half of the picture: a
DENY/REQUIRE_APPROVAL verdict never reaches ``verify_node`` (there's no
``ToolResult`` -- nothing executed), so without this, a report built
purely from Evidence would only ever show successful attempts, never
what was tried and blocked. Track A wires this service into
``DefaultControlPlane`` and the application bootstrap; Orchestration
does not import this concrete Control-team service.

Retention (``FilesystemEvidenceStore.purge_older_than``) and integrity
verification (``FilesystemEvidenceStore.verify_run_integrity``) are
deliberately *not* wrapped here -- they aren't part of the frozen
``EvidenceStore`` Protocol this service is typed against (PLAN.md PM
Issue candidate #4: added to the concrete store only, not the port).
Call them directly against a ``FilesystemEvidenceStore`` instance (see
``redteam.app.cli evidence verify``/``evidence purge``) instead of
through this service.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import digest_payload, tool_result_to_evidence
from redteam.core.enums import EvidenceSourceType, PolicyVerdict
from redteam.core.ids import ActionId, EngagementId, EvidenceId, HypothesisId, RunId, new_id
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.redaction import redact_arguments, redact_sensitive_text


class EvidenceService:
    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    async def record_tool_result(
        self,
        run_id: RunId,
        engagement_id: EngagementId,
        result: ToolResult,
        *,
        hypothesis_id: HypothesisId | None = None,
        summary: str | None = None,
    ) -> EvidenceRecord:
        evidence = tool_result_to_evidence(
            result,
            run_id=run_id,
            engagement_id=engagement_id,
            hypothesis_id=hypothesis_id,
            summary=summary,
        )
        await self._store.append(evidence)
        return evidence

    async def record_blocked_action(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
    ) -> EvidenceRecord:
        """Record a non-executed DENY/REQUIRE_APPROVAL action as Evidence."""
        if decision.action_id != proposal.action_id:
            raise ValueError("decision.action_id does not match proposal.action_id")
        if decision.verdict not in {
            PolicyVerdict.DENY,
            PolicyVerdict.REQUIRE_APPROVAL,
        }:
            raise ValueError(
                "record_blocked_action only accepts DENY or REQUIRE_APPROVAL decisions"
            )

        action_key = f"{proposal.capability}.{proposal.operation}"
        redacted_reasons = [redact_sensitive_text(reason) for reason in decision.reasons]
        reason_text = "; ".join(redacted_reasons) or "no policy reason provided"
        if decision.verdict == PolicyVerdict.DENY:
            summary = f"Blocked action {action_key}: {reason_text}"
        else:
            summary = f"Approval required for action {action_key}: {reason_text}"

        proposal_payload = proposal.model_dump(mode="json")
        proposal_payload["reason"] = redact_sensitive_text(proposal.reason)
        if proposal.expected_observation is not None:
            proposal_payload["expected_observation"] = redact_sensitive_text(
                proposal.expected_observation
            )
        decision_payload = decision.model_dump(mode="json")
        decision_payload["reasons"] = redacted_reasons
        redacted_payload = redact_arguments(
            {
                "proposal": proposal_payload,
                "decision": decision_payload,
            }
        )
        evidence = EvidenceRecord(
            evidence_id=EvidenceId(new_id("ev")),
            run_id=proposal.run_id,
            engagement_id=proposal.engagement_id,
            source_type=EvidenceSourceType.SYSTEM,
            source_id="control_plane",
            action_id=proposal.action_id,
            invocation_id=None,
            hypothesis_id=proposal.hypothesis_id,
            content_type="application/json",
            artifact_ref=None,
            digest=digest_payload(redacted_payload),
            summary=summary,
            created_at=datetime.now(timezone.utc),
            metadata={
                "record_kind": "blocked_action",
                "action_key": action_key,
                "verdict": decision.verdict.value,
                "reasons": redacted_reasons,
                "matched_rules": list(decision.matched_rules),
                "approval_id": (
                    str(decision.approval_id)
                    if decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
                    and decision.approval_id is not None
                    else None
                ),
            },
        )
        await self._store.append(evidence)
        return evidence

    async def list_for_run(self, run_id: RunId) -> list[EvidenceRecord]:
        return await self._store.list_for_run(run_id)

    async def list_for_action(self, action_id: ActionId) -> list[EvidenceRecord]:
        return await self._store.list_for_action(action_id)
