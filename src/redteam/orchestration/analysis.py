"""Independent verification seam used by the Analysis role."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType, Verdict
from redteam.attack_graph.identity import node_id_for_target
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.action_naming import tool_matches_action_namespace
from redteam.orchestration.cognition import StructuredDecisionClient
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.convert import digest_payload
from redteam.core.ids import EvidenceId, new_id
from datetime import datetime, timezone
from redteam.orchestration.expectations import Expectation, ExpectationOutcome, verdict_for

VerificationOutcome = Literal["success", "refuted", "inconclusive", "blocked", "unknown"]


class VerificationRequest(BaseModel):
    proposal: ActionProposal
    result: ToolResult
    expectation: Expectation | None = None
    source_refs: list[str] = Field(default_factory=list)
    hypothesis_claim: str | None = None
    violation_if_confirmed: bool | None = None


class VerificationAssessment(BaseModel):
    verdict: Verdict
    reason: str
    reviewer_id: str
    source_refs: list[str] = Field(default_factory=list)
    outcome: VerificationOutcome | None = None
    last_record_stage: str | None = None
    claim_supported: bool | None = None


class AnalysisVerifier(Protocol):
    async def verify(self, request: VerificationRequest) -> VerificationAssessment: ...


class DeterministicAnalysisVerifier:
    """Default reviewer: reproducible and independent from the executor."""

    reviewer_id = "analysis:deterministic-expectation-v1"

    async def verify(self, request: VerificationRequest) -> VerificationAssessment:
        verdict, reason = verdict_for(request.expectation, request.result)
        outcome: VerificationOutcome | None = _outcome_for_deterministic(
            request, verdict
        )
        claim_text = (request.hypothesis_claim or "").strip()
        if outcome == "success" and claim_text:
            claim_flag: bool | None = True
        else:
            claim_flag = False
        return VerificationAssessment(
            verdict=verdict,
            reason=reason,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
            outcome=outcome,
            claim_supported=claim_flag,
        )


_ANALYSIS_SYSTEM = (
    "Judge the tested hypothesis claim against the tool output and the "
    "pre-recorded expectation. Confirm only what the output shows for "
    "that claim; expectation alone is never enough and a bare success "
    "proves nothing. Reply with JSON only."
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


def _outcome_for_deterministic(
    request: VerificationRequest, verdict: Verdict
) -> VerificationOutcome | None:
    """Map a code-only verdict onto the shared outcome vocabulary."""
    if not request.result.success:
        return "unknown"
    if request.expectation is None:
        return "unknown"
    assessed = request.expectation.evaluate(request.result)
    if assessed is ExpectationOutcome.MET:
        return "success"
    if assessed is ExpectationOutcome.NOT_MET:
        return "refuted"
    return "unknown"


def _is_anonymous_metadata(metadata: dict[str, Any]) -> bool:
    """True when the stored evidence claims no account."""
    if not isinstance(metadata, dict):
        return False
    if metadata.get("anonymous") is True:
        return True
    single = metadata.get("principal_ref")
    if isinstance(single, str) and single.strip().lower() == "anonymous":
        return True
    multi = metadata.get("principal_refs")
    if isinstance(multi, (list, tuple, set)):
        return any(
            isinstance(item, str) and item.strip().lower() == "anonymous"
            for item in multi
        )
    return False


def _expected_canonical(capability: str, operation: str) -> str:
    """The canonical behavior key the evidence must carry."""
    try:
        from redteam.orchestration.action_naming import canonical_action_key

        return canonical_action_key(capability, operation)
    except Exception:
        return str(capability) + "." + str(operation)


async def resolve_claim_for_review(
    graph_service: Any,
    hypothesis_id: Any | None,
    step: Any | None,
) -> tuple[str | None, bool | None]:
    """Load the real hypothesis claim for the review payload; never invent."""
    draft_claim: str | None = None
    draft_flag: bool | None = None
    try:
        draft = getattr(step, "hypothesis", None)
        desc = getattr(draft, "description", None)
        if isinstance(desc, str) and desc.strip():
            draft_claim = desc.strip()[:2000]
        flag = getattr(draft, "violation_if_confirmed", None)
        if isinstance(flag, bool):
            draft_flag = flag
    except Exception:
        pass
    stored_claim: str | None = None
    stored_flag: bool | None = None
    if hypothesis_id is not None and str(hypothesis_id).strip():
        try:
            getter = getattr(graph_service, "get_hypothesis", None)
            stored = await getter(str(hypothesis_id)) if callable(getter) else None
        except Exception:
            stored = None
        try:
            desc = getattr(stored, "description", None)
            if isinstance(desc, str) and desc.strip():
                stored_claim = desc.strip()[:2000]
            meta = getattr(stored, "metadata", None)
            if isinstance(meta, dict):
                for key in ("violation_if_confirmed", "violation_if_met"):
                    value = meta.get(key)
                    if isinstance(value, bool):
                        stored_flag = value
                        break
        except Exception:
            pass
    claim = stored_claim if stored_claim is not None else draft_claim
    flag = stored_flag if stored_flag is not None else draft_flag
    return (claim, flag)


def _clean_identity(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _has_raw_account_artifact(
    metadata: dict[str, Any],
    raw: dict[str, Any] | Any,
    claimed: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    """True only on exact structural identity equality; never bare or substring."""
    args = arguments if isinstance(arguments, dict) else {}
    expected_principals: set[str] = set()
    cleaned_claim = _clean_identity(claimed)
    if cleaned_claim is not None:
        expected_principals.add(cleaned_claim)
    for key in ("principal_ref", "principal", "first_principal_ref", "second_principal_ref"):
        cleaned = _clean_identity(args.get(key))
        if cleaned is not None:
            expected_principals.add(cleaned)
    expected_sessions: set[str] = set()
    for key in ("session_ref", "first_session_ref", "second_session_ref"):
        cleaned = _clean_identity(args.get(key))
        if cleaned is not None:
            expected_sessions.add(cleaned)
    raw_principals: set[str] = set()
    raw_sessions: set[str] = set()
    http_obs = metadata.get("http_observation")
    if isinstance(http_obs, dict):
        cleaned = _clean_identity(http_obs.get("session_ref"))
        if cleaned is not None:
            raw_sessions.add(cleaned)
    comparison = metadata.get("session_comparison")
    comparison_dict: dict[str, Any] | None = comparison if isinstance(comparison, dict) else None
    if comparison_dict is not None:
        for key in ("first_session_ref", "second_session_ref"):
            cleaned = _clean_identity(comparison_dict.get(key))
            if cleaned is not None:
                raw_sessions.add(cleaned)
        for key in ("first_principal_ref", "second_principal_ref"):
            cleaned = _clean_identity(comparison_dict.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    if isinstance(output, dict):
        for key in ("session_ref", "first_session_ref", "second_session_ref"):
            cleaned = _clean_identity(output.get(key))
            if cleaned is not None:
                raw_sessions.add(cleaned)
        for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
            cleaned = _clean_identity(output.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
        for nested_key in ("first", "second", "authenticated", "anonymous"):
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                for key in ("session_ref", "session"):
                    cleaned = _clean_identity(nested.get(key))
                    if cleaned is not None:
                        raw_sessions.add(cleaned)
                cleaned = _clean_identity(nested.get("principal_ref"))
                if cleaned is not None:
                    raw_principals.add(cleaned)
    if comparison_dict is not None:
        first_principal = _clean_identity(comparison_dict.get("first_principal_ref"))
        second_principal = _clean_identity(comparison_dict.get("second_principal_ref"))
        if first_principal is not None or second_principal is not None:
            if not expected_principals:
                return False
            for observed in (first_principal, second_principal):
                if observed is not None and observed not in expected_principals:
                    return False
            if first_principal is None or second_principal is None:
                return False
        first_session = _clean_identity(comparison_dict.get("first_session_ref"))
        second_session = _clean_identity(comparison_dict.get("second_session_ref"))
        if (first_session is not None or second_session is not None) and expected_sessions:
            for observed in (first_session, second_session):
                if observed is not None and observed not in expected_sessions:
                    return False
    if expected_sessions and (raw_sessions & expected_sessions):
        return True
    if expected_principals and (raw_principals & expected_principals):
        return True
    return False


def _graph_node_type_name(node: Any) -> str:
    node_type = getattr(node, "type", None)
    try:
        value = node_type.value if hasattr(node_type, "value") else node_type
    except Exception:
        value = node_type
    return str(value or "").strip().lower()


def _node_pair_facts(node: Any) -> tuple[dict[str, Any], dict[str, Any], Any]:
    props = getattr(node, "properties", {}) or {}
    if not isinstance(props, dict):
        return ({}, {}, props)
    facts = props.get("facts", {})
    attrs = props.get("attrs", {})
    if not isinstance(facts, dict):
        facts = {}
    if not isinstance(attrs, dict):
        attrs = {}
    return (facts, attrs, props)


async def _session_alias_grounded(
    *,
    evidence_store: Any,
    graph_service: Any | None,
    run_id: str,
    target_ref: str,
    session_name: str,
    principal_name: str,
) -> bool:
    """True only with a measured same-run same-target session->principal mapping."""
    cleaned_session = session_name.strip() if isinstance(session_name, str) else ""
    cleaned_principal = principal_name.strip() if isinstance(principal_name, str) else ""
    cleaned_target = target_ref.strip() if isinstance(target_ref, str) else ""
    if not cleaned_session or not cleaned_principal or not cleaned_target:
        return False
    if graph_service is not None:
        lister = getattr(graph_service, "list_nodes", None)
        nodes = await lister() if callable(lister) else []
        for node in list(nodes or []):
            if _graph_node_type_name(node) not in ("principal", "credential"):
                continue
            facts, _attrs, props = _node_pair_facts(node)
            node_target = facts.get("target_ref")
            if not isinstance(node_target, str) or node_target.strip() != cleaned_target:
                continue
            sessions: set[str] = set()
            for key in ("session_ref", "session_refs"):
                value = facts.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            sessions.add(item.strip())
                elif isinstance(value, str) and value.strip():
                    sessions.add(value.strip())
            node_principal = facts.get("principal_ref")
            if not isinstance(node_principal, str) or node_principal.strip() != cleaned_principal:
                continue
            if cleaned_session not in sessions:
                continue
            measured = False
            provenance = props.get("provenance", []) if isinstance(props, dict) else []
            if isinstance(provenance, list):
                for entry in provenance:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("confidence", "")) != "measured":
                        continue
                    entry_run = entry.get("run_id")
                    if entry_run is not None and str(entry_run) != str(run_id):
                        continue
                    measured = True
                    break
            if not measured:
                continue
            return True
    records = await evidence_store.list_for_run(run_id)
    for rec in list(records or []):
        try:
            meta = getattr(rec, "metadata", None) or {}
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        if str(meta.get("target_node_id", "") or "").strip() != cleaned_target:
            continue
        comparison = meta.get("session_comparison")
        if isinstance(comparison, dict):
            pairs = [
                (comparison.get("first_session_ref"), comparison.get("first_principal_ref")),
                (comparison.get("second_session_ref"), comparison.get("second_principal_ref")),
            ]
            for raw_session, raw_principal in pairs:
                if (
                    isinstance(raw_session, str)
                    and isinstance(raw_principal, str)
                    and raw_session.strip() == cleaned_session
                    and raw_principal.strip() == cleaned_principal
                ):
                    return True
        raw = meta.get("tool_result")
        if not isinstance(raw, dict):
            raw = meta.get("raw_tool_result")
        try:
            output = raw.get("output") if isinstance(raw, dict) else None
        except Exception:
            output = None
        if isinstance(output, dict):
            for nested_key in ("first", "second", "authenticated"):
                nested = output.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                nested_session = nested.get("session_ref", nested.get("session"))
                nested_principal = nested.get("principal_ref")
                if (
                    isinstance(nested_session, str)
                    and isinstance(nested_principal, str)
                    and nested_session.strip() == cleaned_session
                    and nested_principal.strip() == cleaned_principal
                ):
                    return True
    return False


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
    outcome then maps onto the tested claim plus the pre-recorded
    expectation: "success" needs the claim text, a MET violation-marked
    output-grounded expectation, and a grounded security effect -- only
    then a VIOLATION. "success" otherwise is a benign PASS, and
    "refuted" is a PASS. "success" with no claim or no expectation
    under test is UNKNOWN, not a finding. Bare tool status alone never
    proves a violation.
    """

    reviewer_id = "analysis:structured-llm-v1"

    _OUTCOMES = ("success", "refuted", "inconclusive", "blocked", "unknown")

    def __init__(
        self, llm: Any, evidence_store: EvidenceStore, graph_service: Any | None = None
    ) -> None:
        self._llm = llm
        self._store = evidence_store
        self._graph = graph_service
        self._decider = StructuredDecisionClient(llm)

    async def verify(self, request: VerificationRequest) -> VerificationAssessment:
        if self._llm is None:
            assessment = self._unknown(
                request,
                "no model configured for analysis; evidence preserved, no verdict reached",
            )
        else:
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
                assessment = self._unknown(
                    request,
                    "model decision failed" + suffix + ": " + type(exc).__name__
                    + "; evidence preserved, no verdict reached",
                )
            else:
                assessment = await self._decide(request, meaning)
        try:
            receipt = await append_review_receipt(
                evidence_store=self._store,
                proposal=request.proposal,
                result=request.result,
                assessment=assessment,
            )
        except Exception:
            return VerificationAssessment(
                verdict=Verdict.UNKNOWN,
                reason="review_receipt_write_failed; evidence preserved, no verdict reached",
                reviewer_id=self.reviewer_id,
                source_refs=request.source_refs,
                outcome="unknown",
                last_record_stage=None,
                claim_supported=False,
            )
        try:
            assessment.last_record_stage = "analysis_verify"
        except Exception:
            pass
        return assessment

    def _unknown(
        self,
        request: VerificationRequest,
        reason: str,
        outcome: VerificationOutcome = "unknown",
    ) -> VerificationAssessment:
        return VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason=reason,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
            outcome=outcome,
            claim_supported=False,
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
            "hypothesis_claim": request.hypothesis_claim,
            "violation_if_confirmed": request.violation_if_confirmed,
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
                "claim_supported": {"type": "boolean"},
            },
            "required": ["meaning", "outcome", "reason", "claim_supported"],
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
        claim_supported = meaning.get("claim_supported")
        if not isinstance(claim_supported, bool):
            return self._unknown(
                request,
                "model omitted claim_supported; evidence preserved, no verdict reached",
            )
        if outcome in ("unknown", "inconclusive", "blocked"):
            if claim_supported is not False:
                return self._unknown(
                    request,
                    "model reached no conclusion" + suffix,
                    outcome=outcome,  # type: ignore[assignment]
                )
            preserved: VerificationOutcome = outcome  # type: ignore[assignment]
            return self._unknown(
                request, "model reached no conclusion" + suffix, outcome=preserved
            )
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
            claim_text = (request.hypothesis_claim or "").strip()
            if not claim_text:
                return self._unknown(
                    request,
                    "no hypothesis claim under test; an unrelated expectation "
                    "cannot confirm a claim" + suffix,
                )
            if claim_supported is not True:
                return self._unknown(
                    request,
                    "tested claim not supported; evidence preserved, no verdict reached" + suffix,
                )
            if expectation.violation_if_met:
                if request.violation_if_confirmed is False:
                    return self._unknown(
                        request,
                        "claim is benign but the expectation is violation-marked; "
                        "refusing to confirm" + suffix,
                    )
                if getattr(expectation, "kind", "") == "tool_succeeded":
                    return self._unknown(
                        request,
                        "bare tool status alone cannot prove a violation; "
                        "output-grounded expectation required" + suffix,
                    )
                return VerificationAssessment(
                    verdict=Verdict.VIOLATION,
                    reason="model confirms the tested claim and the violation-marked expectation held" + suffix,
                    reviewer_id=self.reviewer_id,
                    source_refs=request.source_refs,
                    outcome="success",
                    claim_supported=True,
                )
            return VerificationAssessment(
                verdict=Verdict.PASS,
                reason="model confirms the benign expectation held" + suffix,
                reviewer_id=self.reviewer_id,
                source_refs=request.source_refs,
                outcome="success",
                claim_supported=True,
            )
        if assessment is not ExpectationOutcome.NOT_MET:
            return self._unknown(
                request,
                "model claims refutation but the recorded expectation was not cleanly "
                "unmet (" + assessment.value + ")" + suffix,
            )
        if claim_supported is not False:
            return self._unknown(
                request,
                "refuted needs an unsupported claim with a cleanly unmet expectation; "
                "evidence preserved, no verdict reached" + suffix,
            )
        if expectation.anomaly_if_unmet:
            return VerificationAssessment(
                verdict=Verdict.ANOMALY,
                reason="model refuted the expectation, which is itself unexpected" + suffix,
                reviewer_id=self.reviewer_id,
                source_refs=request.source_refs,
                outcome="refuted",
                claim_supported=False,
            )
        return VerificationAssessment(
            verdict=Verdict.PASS,
            reason="model refuted the tested expectation" + suffix,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
            outcome="refuted",
            claim_supported=False,
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
        canonical = metadata.get("canonical_operation") or metadata.get("operation_key")
        if not isinstance(canonical, str) or not canonical.strip():
            return "no canonical operation proof; bare operation alone proves nothing"
        expected_canonical = _expected_canonical(
            request.proposal.capability, request.proposal.operation
        )
        if str(canonical).strip().lower() != expected_canonical.strip().lower():
            return "linked evidence describes another behavior"
        meta_action = metadata.get("action_id")
        if meta_action is not None and str(meta_action) != str(request.proposal.action_id):
            return "linked evidence disagrees with its original action"
        meta_invocation = metadata.get("invocation_id")
        if meta_invocation is not None and str(meta_invocation) != str(
            request.result.invocation_id
        ):
            return "linked evidence disagrees with its original invocation"
        stored_proposal = metadata.get("proposal")
        if isinstance(stored_proposal, dict):
            stored_action = stored_proposal.get("action_id")
            if stored_action is not None and str(stored_action) != str(
                request.proposal.action_id
            ):
                return "stored proposal answers another action"
        claimed = _claimed_principal(request.proposal)
        recorded = metadata.get("principal_ref")
        recorded_refs = metadata.get("principal_refs")
        if claimed is not None:
            if _is_anonymous_metadata(metadata):
                return "anonymous evidence cannot satisfy an account-scoped action"
            pool: set[str] = set()
            if isinstance(recorded, str) and recorded.strip():
                pool.add(recorded.strip())
            if isinstance(recorded_refs, (list, tuple, set)):
                for item in recorded_refs:
                    if isinstance(item, str) and item.strip():
                        pool.add(item.strip())
            if claimed not in pool:
                if not pool:
                    return "an account was used but the evidence names none"
                return "linked evidence names another account"
            args = request.proposal.arguments if isinstance(request.proposal.arguments, dict) else {}
            if _has_raw_account_artifact(metadata, raw, claimed, args):
                pass
            else:
                bound_session = _clean_identity(args.get("session_ref"))
                raw_session_hit = False
                if bound_session is not None:
                    http_obs = metadata.get("http_observation")
                    if isinstance(http_obs, dict) and _clean_identity(http_obs.get("session_ref")) == bound_session:
                        raw_session_hit = True
                    if not raw_session_hit:
                        try:
                            output = raw.get("output") if isinstance(raw, dict) else None
                        except Exception:
                            output = None
                        if isinstance(output, dict):
                            for key in ("session_ref", "first_session_ref", "second_session_ref"):
                                if _clean_identity(output.get(key)) == bound_session:
                                    raw_session_hit = True
                                    break
                            if not raw_session_hit:
                                for nested_key in ("first", "second", "authenticated"):
                                    nested = output.get(nested_key)
                                    if isinstance(nested, dict) and (
                                        _clean_identity(nested.get("session_ref")) == bound_session
                                        or _clean_identity(nested.get("session")) == bound_session
                                    ):
                                        raw_session_hit = True
                                        break
                if bound_session is None or not raw_session_hit:
                    return (
                        "account claim has no exact raw session receipt; "
                        "declaration alone is not proof"
                    )
                try:
                    alias_ok = await _session_alias_grounded(
                        evidence_store=self._store,
                        graph_service=getattr(self, "_graph", None),
                        run_id=str(request.proposal.run_id),
                        target_ref=str(metadata.get("target_node_id", "") or ""),
                        session_name=bound_session,
                        principal_name=claimed,
                    )
                except Exception:
                    return "session binding lookup failed; treating as ungrounded"
                if not alias_ok:
                    return (
                        "account alias has no measured same-run same-target mapping; "
                        "declaration alone is not proof"
                    )
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


def _receipt_support_assignment(
    assessment: VerificationAssessment, source_refs: list[str]
) -> tuple[list[str], list[str]]:
    """Assign supporting vs refuting evidence from the outcome, not bare status."""
    refs = [str(item) for item in (source_refs or []) if str(item).strip()]
    outcome = getattr(assessment, "outcome", None)
    if outcome == "success":
        return (refs, [])
    if outcome == "refuted":
        return ([], refs)
    return ([], [])


def _clean_receipt_identity(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def _covered_principals_from_sources(store: Any, source_refs: Any) -> list[str]:
    """Derive account coverage only from comparison raw principal fields."""
    seen: dict[str, None] = {}
    refs = [str(item) for item in (source_refs or []) if str(item).strip()][:8]
    for ref in refs:
        try:
            record = await store.get(ref)
        except Exception:
            continue
        try:
            metadata = getattr(record, "metadata", None) or {}
        except Exception:
            continue
        if not isinstance(metadata, dict):
            continue
        comparison = metadata.get("session_comparison")
        if isinstance(comparison, dict):
            for key in ("first_principal_ref", "second_principal_ref"):
                cleaned = _clean_receipt_identity(comparison.get(key))
                if cleaned is not None and cleaned not in seen:
                    seen[cleaned] = None
        raw: Any = metadata.get("tool_result")
        if not isinstance(raw, dict):
            raw = metadata.get("raw_tool_result")
        try:
            output = raw.get("output") if isinstance(raw, dict) else None
        except Exception:
            output = None
        if isinstance(output, dict):
            for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
                cleaned = _clean_receipt_identity(output.get(key))
                if cleaned is not None and cleaned not in seen:
                    seen[cleaned] = None
            for nested_key in ("first", "second", "authenticated", "anonymous"):
                nested = output.get(nested_key)
                if isinstance(nested, dict):
                    cleaned = _clean_receipt_identity(nested.get("principal_ref"))
                    if cleaned is not None and cleaned not in seen:
                        seen[cleaned] = None
    return sorted(seen)


async def append_review_receipt(
    *,
    evidence_store: Any,
    proposal: ActionProposal,
    result: ToolResult,
    assessment: VerificationAssessment,
    admission: str = "interim",
    supersedes: list[str] | None = None,
) -> Any | None:
    """Append the public review receipt for one meaning judgment.

    Supporting vs refuting assignment follows the outcome: success supports,
    refuted refutes, anything else assigns neither. A bare tool success or
    status alone never wins; only a grounded outcome with its expectation
    check assigns support.

    Only admission == "final" counts as approval. Interim receipts record
    the raw meaning judgment before code downgrades; the final admission
    supersedes them so a stale positive can never be consumed as approval.
    """
    try:
        target_node: str | None = str(node_id_for_target(proposal.target))
    except Exception:
        target_node = None
    supporting, refuting = _receipt_support_assignment(assessment, assessment.source_refs)
    outcome = getattr(assessment, "outcome", None)
    superseded_stages = [str(item) for item in (supersedes or []) if str(item).strip()]
    try:
        covered_principals = await _covered_principals_from_sources(
            evidence_store, assessment.source_refs
        )
    except Exception:
        covered_principals = []
    payload: dict[str, Any] = {
        "record_kind": "review_receipt",
        "public": True,
        "admission": admission,
        "supersedes": superseded_stages,
        "reviewer_id": assessment.reviewer_id,
        "verdict": assessment.verdict.value if hasattr(assessment.verdict, "value") else str(assessment.verdict),
        "outcome": outcome,
        "reason": assessment.reason,
        "claim_supported": getattr(assessment, "claim_supported", None),
        "action_id": str(proposal.action_id),
        "invocation_id": str(result.invocation_id),
        "capability": proposal.capability,
        "operation": proposal.operation,
        "target_node_id": target_node,
        "hypothesis_id": str(proposal.hypothesis_id) if proposal.hypothesis_id is not None else None,
        "source_evidence_ids": [str(item) for item in (assessment.source_refs or [])],
        "supporting_evidence_ids": supporting,
        "refuting_evidence_ids": refuting,
        "covered_principals": covered_principals,
    }
    receipt = EvidenceRecord(
        evidence_id=EvidenceId(new_id("ev")),
        run_id=proposal.run_id,
        engagement_id=proposal.engagement_id,
        source_type=EvidenceSourceType.SYSTEM,
        source_id=str(assessment.reviewer_id),
        action_id=proposal.action_id,
        invocation_id=result.invocation_id,
        hypothesis_id=proposal.hypothesis_id,
        content_type="application/json",
        artifact_ref=None,
        digest=digest_payload(payload),
        summary=("review " + str(outcome or "unknown") + "/" + str(payload["verdict"]) + " for " + str(proposal.action_id))[:280],
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await evidence_store.append(receipt)
    return receipt


def is_final_admission_receipt(record: Any) -> bool:
    """True only for a final-admission public review receipt."""
    try:
        metadata = getattr(record, "metadata", None)
        if metadata is None and isinstance(record, dict):
            metadata = record.get("metadata", None)
    except Exception:
        return False
    if not isinstance(metadata, dict):
        return False
    if metadata.get("record_kind") != "review_receipt":
        return False
    if metadata.get("public") is not True:
        return False
    return metadata.get("admission") == "final"


def hypothesis_status_for_assessment(assessment: VerificationAssessment) -> str:
    """Map the grounded outcome onto a hypothesis state.

    Grounded success always confirms the tested hypothesis, including a
    benign PASS. Grounded refuted always rejects it, including ANOMALY
    (anomaly is a separate signal from claim truth). UNKNOWN or legacy
    None never auto-confirms or auto-rejects. Finding promotion still
    needs VIOLATION plus a violation-marked hypothesis, so benign
    confirmation never becomes a finding.
    """
    from redteam.attack_graph.service import (
        HYPOTHESIS_CONFIRMED,
        HYPOTHESIS_INCONCLUSIVE,
        HYPOTHESIS_REJECTED,
    )

    outcome = getattr(assessment, "outcome", None)
    if outcome == "success":
        return HYPOTHESIS_CONFIRMED
    if outcome == "refuted":
        return HYPOTHESIS_REJECTED
    return HYPOTHESIS_INCONCLUSIVE
