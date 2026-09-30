"""Independent verification seam used by the Analysis role."""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import Verdict
from redteam.attack_graph.identity import node_id_for_target
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.action_naming import tool_matches_action_namespace
from redteam.orchestration.cognition import StructuredDecisionClient
from redteam.orchestration.expectations import Expectation, ExpectationOutcome, verdict_for


class VerificationRequest(BaseModel):
    proposal: ActionProposal
    result: ToolResult
    expectation: Expectation | None = None
    source_refs: list[str] = Field(default_factory=list)


class VerificationAssessment(BaseModel):
    verdict: Verdict
    reason: str
    reviewer_id: str
    source_refs: list[str] = Field(default_factory=list)


class AnalysisVerifier(Protocol):
    async def verify(self, request: VerificationRequest) -> VerificationAssessment: ...


class DeterministicAnalysisVerifier:
    """Default reviewer: reproducible and independent from the executor."""

    reviewer_id = "analysis:deterministic-expectation-v1"

    async def verify(self, request: VerificationRequest) -> VerificationAssessment:
        verdict, reason = verdict_for(request.expectation, request.result)
        return VerificationAssessment(
            verdict=verdict,
            reason=reason,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
        )


_ANALYSIS_SYSTEM = (
    "You judge what a tool result means against the expectation recorded "
    "before the action ran. Answer only what the output shows; a bare "
    "success or a normal response is not proof of a security violation, "
    "and an unknown is an unknown, never a success."
)


def _truncate_for_prompt(output: Any, *, budget: int = 2000) -> Any:
    """Cap the payload sent out for a meaning judgment."""
    if output is None:
        return None
    if isinstance(output, str):
        text = output
    else:
        text = json.dumps(output, ensure_ascii=False, default=str)
    return text if len(text) <= budget else text[:budget]
    return text if len(text) <= budget else text[:budget]


def _claimed_principal(proposal: ActionProposal) -> str | None:
    """The account the proposal said it would act as, if it named one."""
    for key in ("principal_ref", "session_ref", "principal"):
        value = proposal.arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class StructuredLLMAnalysisVerifier:
    """The model judges meaning; code verifies grounding -- grounding wins.

    There is no deterministic substitute anywhere on this path: a missing
    model, a failed model call, or an uninterpretable answer ends in an
    explicit UNKNOWN with the evidence left intact, never in a borrowed
    PASS or VIOLATION. A bare tool success or a normal response is not
    proof on its own, and UNKNOWN stays "no proof", never a success.

    Once the model speaks, deterministic checks confirm the load-bearing
    facts -- the linked evidence exists in this run, the run, invocation,
    action, target, and behavior line up, and a named account (when both
    sides name one) is the same. Any gap forces UNKNOWN. The model
    outcome then maps onto the pre-recorded expectation: "success" on an
    expectation whose meeting is the violation is a VIOLATION, "success"
    otherwise is a PASS, and "refuted" is a PASS. "success" with no
    expectation under test is UNKNOWN, not a finding.
    """

    reviewer_id = "analysis:structured-llm-v1"

    _OUTCOMES = ("success", "refuted", "inconclusive", "blocked", "unknown")

    def __init__(self, llm: Any, evidence_store: EvidenceStore) -> None:
        self._llm = llm
        self._store = evidence_store
        self._decider = StructuredDecisionClient(llm)

    async def verify(self, request: VerificationRequest) -> VerificationAssessment:
        if self._llm is None:
            return self._unknown(
                request,
                "no model configured for analysis; evidence preserved, no verdict reached",
            )
        try:
            meaning = await self._decider.ask(
                stage="analysis_verify",
                payload=self._meaning_payload(request),
                response_schema=self._meaning_schema(),
                system=_ANALYSIS_SYSTEM,
            )
        except Exception as exc:  # noqa: BLE001 - model failure is UNKNOWN, explicitly
            kind = getattr(exc, "kind", None)
            suffix = " (" + str(kind) + ")" if kind else ""
            return self._unknown(
                request,
                "model decision failed" + suffix + ": " + type(exc).__name__
                + "; evidence preserved, no verdict reached",
            )
        return await self._decide(request, meaning)

    def _unknown(self, request: VerificationRequest, reason: str) -> VerificationAssessment:
        return VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason=reason,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
        )

    def _meaning_payload(self, request: VerificationRequest) -> dict[str, Any]:
        return {
            "capability": request.proposal.capability,
            "operation": request.proposal.operation,
            "target": str(request.proposal.target),
            "tool_id": str(request.result.tool_id),
            "success": request.result.success,
            "output": _truncate_for_prompt(request.result.output),
            "error_code": request.result.error.code if request.result.error else None,
            "expectation": (
                request.expectation.model_dump(mode="json")
                if request.expectation is not None
                else None
            ),
        }

    @staticmethod
    def _meaning_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "meaning": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["success", "refuted", "inconclusive", "blocked", "unknown"],
                },
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["meaning", "outcome", "reason"],
            "additionalProperties": False,
        }

    async def _decide(self, request: VerificationRequest, meaning: Any) -> VerificationAssessment:
        if not isinstance(meaning, dict):
            return self._unknown(
                request,
                "model returned no decision; evidence preserved, no verdict reached",
            )
        outcome = meaning.get("outcome")
        if outcome not in self._OUTCOMES:
            return self._unknown(
                request,
                "model outcome uninterpretable (" + str(outcome)
                + "); evidence preserved, no verdict reached",
            )
        if not request.result.success:
            code = request.result.error.code if request.result.error else "unknown_error"
            return self._unknown(
                request,
                "tool execution failed (" + code + "); no verdict can be reached "
                "regardless of meaning",
            )
        gaps = await self._grounding_gaps(request)
        if gaps:
            return self._unknown(request, "no proof: " + "; ".join(gaps))
        model_reason = str(meaning.get("reason") or "").strip()[:500]
        suffix = ": " + model_reason if model_reason else ""
        if outcome in ("unknown", "inconclusive", "blocked"):
            return self._unknown(request, "model reached no conclusion" + suffix)
        expectation = request.expectation
        if expectation is None:
            return self._unknown(
                request,
                "no expectation under test; a bare success proves nothing" + suffix,
            )
        assessment = expectation.evaluate(request.result)
        if outcome == "success":
            if assessment is not ExpectationOutcome.MET:
                return self._unknown(
                    request,
                    "model claims success but the recorded expectation did not hold ("
                    + assessment.value + ")" + suffix,
                )
            if expectation.violation_if_met:
                return VerificationAssessment(
                    verdict=Verdict.VIOLATION,
                    reason="model confirms the violation-marked expectation held" + suffix,
                    reviewer_id=self.reviewer_id,
                    source_refs=request.source_refs,
                )
            return VerificationAssessment(
                verdict=Verdict.PASS,
                reason="model confirms the benign expectation held" + suffix,
                reviewer_id=self.reviewer_id,
                source_refs=request.source_refs,
            )
        if assessment is not ExpectationOutcome.NOT_MET:
            return self._unknown(
                request,
                "model claims refutation but the recorded expectation was not cleanly "
                "unmet (" + assessment.value + ")" + suffix,
            )
        if expectation.anomaly_if_unmet:
            return VerificationAssessment(
                verdict=Verdict.ANOMALY,
                reason="model refuted the expectation, which is itself unexpected" + suffix,
                reviewer_id=self.reviewer_id,
                source_refs=request.source_refs,
            )
        return VerificationAssessment(
            verdict=Verdict.PASS,
            reason="model refuted the tested expectation" + suffix,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
        )
    async def _grounding_gaps(self, request: VerificationRequest) -> list[str]:
        gaps: list[str] = []
        proposal = request.proposal
        result = request.result
        if str(result.action_id) != str(proposal.action_id):
            gaps.append("result action does not match the proposal")
        if not result.invocation_id:
            gaps.append("result carries no invocation")
        if not proposal.capability or not proposal.operation:
            gaps.append("proposal names no behavior")
        else:
            if not tool_matches_action_namespace(result.tool_id, proposal.capability):
                gaps.append("tool sits outside the proposed capability")
        gap = await self._evidence_gap(request)
        if gap is not None:
            gaps.append(gap)
        return gaps

    async def _evidence_gap(self, request: VerificationRequest) -> str | None:
        refs = [str(ref) for ref in (request.source_refs or []) if ref]
        if not refs:
            return "no evidence reference to check"
        record = None
        for ref in refs:
            try:
                candidate = await self._store.get(ref)
            except Exception:  # noqa: BLE001 - an unreadable store proves nothing
                return "evidence store unreadable"
            if candidate is not None:
                record = candidate
                break
        if record is None:
            return "linked evidence is missing"
        if str(record.run_id) != str(request.proposal.run_id):
            return "linked evidence belongs to another run"
        if record.action_id is None or str(record.action_id) != str(
            request.proposal.action_id
        ):
            return "linked evidence does not answer this action"
        if record.invocation_id is None or str(record.invocation_id) != str(
            request.result.invocation_id
        ):
            return "linked evidence does not answer this invocation"
        raw = record.metadata.get("tool_result") or record.metadata.get("raw_tool_result")
        if not isinstance(raw, dict):
            return "no preserved raw result to check against"
        if str(raw.get("action_id")) != str(request.proposal.action_id):
            return "preserved raw result answers another action"
        if str(raw.get("invocation_id")) != str(request.result.invocation_id):
            return "preserved raw result answers another invocation"
        if str(raw.get("tool_id")) != str(request.result.tool_id):
            return "preserved raw result came from another tool"
        metadata = getattr(record, "metadata", None) or {}
        if metadata.get("capability") != request.proposal.capability:
            return "linked evidence describes another capability"
        if metadata.get("operation") != request.proposal.operation:
            return "linked evidence describes another behavior"
        claimed = _claimed_principal(request.proposal)
        recorded = metadata.get("principal_ref")
        if claimed is not None and recorded != claimed:
            if recorded is None:
                return "an account was used but the evidence names none"
            return "linked evidence names another account"
        expected_target = metadata.get("target_node_id")
        if expected_target is None:
            return "linked evidence names no target"
        try:
            actual_target = str(node_id_for_target(request.proposal.target))
        except Exception:  # noqa: BLE001 - an unresolvable target is not a match
            return "proposed target cannot be resolved"
        if expected_target != actual_target:
            return "linked evidence observed another target"
        return None
