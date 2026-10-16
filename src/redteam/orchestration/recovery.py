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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypedDict

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import HYPOTHESIS_INCONCLUSIVE
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.tool import ToolResult, execution_outcome_uncertain
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType, Verdict
from redteam.core.ids import EvidenceId, new_id
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.analysis import (
    BudgetedAnalysisVerifier,
    VerificationRequest,
    append_review_receipt,
    hypothesis_status_for_assessment,
    resolve_claim_for_review,
    session_list_pairs,
)
from redteam.orchestration.completion import check_record_linkage, preserved_target_is_absent
from redteam.orchestration.evidence_access import locate_raw, raw_from_metadata
from redteam.orchestration.findings import (
    FindingPromoter,
    PromotionRequest,
    claim_keys_from_findings,
)
from redteam.orchestration.service_status import severity_for

__all__ = [
    "is_execution_ambiguous",
    "reprocess_evidence",
    "record_mapping_failure",
    "record_target_references",
    "mapping_attempt_key",
    "ProcessingAttempt",
    "record_processing_attempt",
    "record_recovery_note",
    "recovery_report",
]


def is_execution_ambiguous(result: ToolResult | None) -> bool:
    """True when the caller must check the ledger and tool state first.

    A missing result, or one stamped with an orchestration-side exception,
    says nothing about whether the tool ran -- reprocessing would be
    guessing. Returns False only when a real tool payload is in hand.
    """
    if result is None:
        return True
    return execution_outcome_uncertain(result.metadata)


def _clean_recovery_identity(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


async def _recovery_alias_grounded(
    *,
    evidence_store: Any,
    graph_service: Any,
    run_id: str,
    target_ref: str,
    session_name: str,
    principal_name: str,
    arguments_target_ref: str | None = None,
) -> bool:
    from redteam.orchestration.analysis import _session_alias_grounded

    return await _session_alias_grounded(
        evidence_store=evidence_store,
        graph_service=graph_service,
        run_id=run_id,
        target_ref=target_ref,
        session_name=session_name,
        principal_name=principal_name,
        arguments_target_ref=arguments_target_ref,
    )


def _has_exact_recovery_proof(
    meta: dict[str, Any],
    raw: dict[str, Any],
    claimed_accounts: set[str],
    stored_proposal: dict[str, Any] | None,
) -> bool:
    """Exact structural identity equality only; never bare or substring."""
    cleaned_claimed = {
        str(item).strip() for item in (claimed_accounts or set()) if str(item).strip()
    }
    if not cleaned_claimed:
        return False
    args = stored_proposal.get("arguments") if isinstance(stored_proposal, dict) else {}
    if not isinstance(args, dict):
        args = {}
    expected_principals = set(cleaned_claimed)
    for key in ("principal_ref", "principal", "first_principal_ref", "second_principal_ref"):
        cleaned = _clean_recovery_identity(args.get(key))
        if cleaned is not None:
            expected_principals.add(cleaned)
    expected_sessions: set[str] = set()
    for key in ("session_ref", "first_session_ref", "second_session_ref"):
        cleaned = _clean_recovery_identity(args.get(key))
        if cleaned is not None:
            expected_sessions.add(cleaned)
    raw_principals: set[str] = set()
    raw_sessions: set[str] = set()
    http_obs = meta.get("http_observation")
    if isinstance(http_obs, dict):
        cleaned = _clean_recovery_identity(http_obs.get("session_ref"))
        if cleaned is not None:
            raw_sessions.add(cleaned)
    comparison = meta.get("session_comparison")
    comparison_dict: dict[str, Any] | None = comparison if isinstance(comparison, dict) else None
    if comparison_dict is not None:
        for key in ("first_session_ref", "second_session_ref"):
            cleaned = _clean_recovery_identity(comparison_dict.get(key))
            if cleaned is not None:
                raw_sessions.add(cleaned)
        for key in ("first_principal_ref", "second_principal_ref"):
            cleaned = _clean_recovery_identity(comparison_dict.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    if isinstance(output, dict):
        for key in ("session_ref", "first_session_ref", "second_session_ref"):
            cleaned = _clean_recovery_identity(output.get(key))
            if cleaned is not None:
                raw_sessions.add(cleaned)
        for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
            cleaned = _clean_recovery_identity(output.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
        for nested_key in ("first", "second", "authenticated", "anonymous"):
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                for key in ("session_ref", "session"):
                    cleaned = _clean_recovery_identity(nested.get(key))
                    if cleaned is not None:
                        raw_sessions.add(cleaned)
                cleaned = _clean_recovery_identity(nested.get("principal_ref"))
                if cleaned is not None:
                    raw_principals.add(cleaned)
    if comparison_dict is not None:
        first_principal = _clean_recovery_identity(comparison_dict.get("first_principal_ref"))
        second_principal = _clean_recovery_identity(comparison_dict.get("second_principal_ref"))
        if first_principal is not None or second_principal is not None:
            if not expected_principals:
                return False
            for observed in (first_principal, second_principal):
                if observed is not None and observed not in expected_principals:
                    return False
            if first_principal is None or second_principal is None:
                return False
        first_session = _clean_recovery_identity(comparison_dict.get("first_session_ref"))
        second_session = _clean_recovery_identity(comparison_dict.get("second_session_ref"))
        if (first_session is not None or second_session is not None) and expected_sessions:
            for observed in (first_session, second_session):
                if observed is not None and observed not in expected_sessions:
                    return False
    args_target = args.get("target_ref") if isinstance(args, dict) else None
    row_sessions, row_principals, _ = session_list_pairs(
        output, args_target if isinstance(args_target, str) else None
    )
    raw_sessions |= row_sessions
    raw_principals |= row_principals
    return cleaned_claimed <= (raw_principals | (raw_sessions & expected_sessions))


class _RecoveryAttempt(TypedDict):
    attempt: int
    supersedes_attempt: int


async def _next_recovery_attempt(*, evidence_store: Any, source: EvidenceRecord) -> tuple[int, int]:
    """Read this origin's previous attempt once; initial application is 1.

    Store errors propagate before replay starts. Legacy companions without
    a number belong to the initial attempt and never reset a newer one.
    """
    previous = 1
    for companion in await evidence_store.list_for_run(source.run_id):
        if (
            companion.run_id != source.run_id
            or companion.engagement_id != source.engagement_id
            or companion.action_id != source.action_id
            or companion.invocation_id != source.invocation_id
            or companion.metadata.get("source_evidence_id") != str(source.evidence_id)
        ):
            continue
        attempt = companion.metadata.get("attempt", 1)
        if type(attempt) is int and attempt > 0:
            previous = max(previous, attempt)
    return previous + 1, previous


async def reprocess_evidence(
    *,
    evidence_id: str,
    run_id: str,
    evidence_store: Any,
    graph_service: Any,
    verifier: Any,
    result_mappers: Any | None = None,
    budget: Any | None = None,
    before_call: Any | None = None,
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
    "invalid_raw" / "no_proposal" / "invalid_proposal" / "no_canonical" /
    "link_mismatch" / "anonymous_blocked" / "no_account_proof". Lookup
    failures ("not_found", "run_mismatch", "no_raw") write nothing;
    everything from "invalid_raw" on leaves a companion note behind.
    The tool gateway is never called here; only the stored payload, the
    passed verifier, the graph, and the mappers are touched.
    Initial application is attempt 1; each explicit recovery reads the
    origin's previous maximum once and carries its new attempt plus
    supersedes_attempt on every companion in this request.
    """
    record = await evidence_store.get(evidence_id)
    status, raw = locate_raw(record, run_id)
    if status == "missing":
        return {
            "status": "not_found",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
        }
    if status == "other_run":
        return {
            "status": "run_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "actual_run_id": str(record.run_id),
        }
    if status == "no_raw" or raw is None:
        return {
            "status": "no_raw",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
        }
    attempt, supersedes_attempt = await _next_recovery_attempt(
        evidence_store=evidence_store, source=record
    )
    recovery_attempt: _RecoveryAttempt = {
        "attempt": attempt,
        "supersedes_attempt": supersedes_attempt,
    }
    await record_recovery_note(
        evidence_store=evidence_store,
        run_id=record.run_id,
        engagement_id=record.engagement_id,
        source_evidence_id=str(record.evidence_id),
        note="explicit recovery request started",
        action_id=record.action_id,
        invocation_id=record.invocation_id,
        hypothesis_id=record.hypothesis_id,
        recovery_status="attempted",
        **recovery_attempt,
    )
    try:
        result = ToolResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a corrupt payload is data, not a crash
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing failed: stored raw result unreadable (" + type(exc).__name__ + ")",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
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
            **recovery_attempt,
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
            note="reprocessing failed: stored proposal unreadable (" + type(exc).__name__ + ")",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
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
    meta = record.metadata if isinstance(record.metadata, dict) else {}
    canonical_value = str(
        meta.get("canonical_operation") or meta.get("operation_key") or ""
    ).strip()
    if not canonical_value:
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note=(
                "reprocessing blocked: no canonical operation proof "
                "(bare operation alone proves nothing)"
            ),
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "no_canonical",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    if str(raw.get("action_id", "")).strip() != str(record.action_id or "").strip():
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing blocked: preserved raw result answers another action",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "link_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    if str(raw.get("invocation_id", "")).strip() != str(record.invocation_id or "").strip():
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing blocked: preserved raw result answers another invocation",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "link_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    if str(stored_proposal.get("action_id", "")).strip() != str(record.action_id or "").strip():
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note=(
                "reprocessing blocked: stored proposal answers another action; "
                "identifier alone is not proof"
            ),
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "link_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    target_ref = str(meta.get("target_node_id", "") or "").strip()
    if preserved_target_is_absent(meta) and (
        target_ref or check_record_linkage(record, meta, raw, str(evidence_id))
    ):
        return {"status": "link_mismatch", "evidence_id": str(evidence_id)}
    if not target_ref and not preserved_target_is_absent(meta):
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing blocked: stored evidence names no target",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "link_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    try:
        expected_target = (
            str(node_id_for_target(proposal.target)) if proposal.target is not None else ""
        )
    except Exception:
        expected_target = ""
    if expected_target and target_ref != expected_target:
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing blocked: stored evidence observed another target",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "link_mismatch",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    record_principal = meta.get("principal_ref")
    anonymous_record = meta.get("anonymous") is True or (
        isinstance(record_principal, str) and record_principal.strip().lower() == "anonymous"
    )
    step_principal = getattr(step, "principal_ref", None) if step is not None else None
    account_scoped = (
        isinstance(step_principal, str)
        and step_principal.strip()
        and step_principal.strip().lower() != "anonymous"
    )
    if not account_scoped and isinstance(record_principal, str) and record_principal.strip():
        account_scoped = record_principal.strip().lower() != "anonymous"
    if anonymous_record and account_scoped:
        companion = await record_recovery_note(
            evidence_store=evidence_store,
            run_id=record.run_id,
            engagement_id=record.engagement_id,
            source_evidence_id=str(record.evidence_id),
            note="reprocessing blocked: anonymous evidence cannot satisfy an account-scoped replay",
            action_id=record.action_id,
            invocation_id=record.invocation_id,
            hypothesis_id=record.hypothesis_id,
            **recovery_attempt,
        )
        return {
            "status": "anonymous_blocked",
            "evidence_id": str(evidence_id),
            "requested_run_id": str(run_id),
            "companion_evidence_id": str(companion.evidence_id),
        }
    if account_scoped:
        claimed_set: set[str] = set()
        if isinstance(record_principal, str) and record_principal.strip():
            cleaned_principal = record_principal.strip()
            if cleaned_principal.lower() != "anonymous":
                claimed_set.add(cleaned_principal)
        if isinstance(step_principal, str) and step_principal.strip():
            cleaned_step = step_principal.strip()
            if cleaned_step.lower() != "anonymous":
                claimed_set.add(cleaned_step)
        exact_ok = _has_exact_recovery_proof(meta, raw, claimed_set, stored_proposal)
        if not exact_ok:
            stored_args_map = (
                stored_proposal.get("arguments") if isinstance(stored_proposal, dict) else {}
            )
            if not isinstance(stored_args_map, dict):
                stored_args_map = {}
            bound_session = stored_args_map.get("session_ref")
            if not isinstance(bound_session, str) or not bound_session.strip():
                bound_session = None
            else:
                bound_session = bound_session.strip()
            alias_verified: set[str] = set()
            if bound_session is not None:
                try:
                    for claimed_name in sorted(claimed_set):
                        if await _recovery_alias_grounded(
                            evidence_store=evidence_store,
                            graph_service=graph_service,
                            run_id=str(record.run_id),
                            target_ref=target_ref,
                            session_name=bound_session,
                            principal_name=claimed_name,
                            arguments_target_ref=stored_args_map.get("target_ref")
                            if isinstance(stored_args_map, dict)
                            else None,
                        ):
                            alias_verified.add(claimed_name)
                except Exception:
                    companion = await record_recovery_note(
                        evidence_store=evidence_store,
                        run_id=record.run_id,
                        engagement_id=record.engagement_id,
                        source_evidence_id=str(record.evidence_id),
                        note=(
                            "reprocessing blocked: session binding lookup failed; "
                            "treating as ungrounded"
                        ),
                        action_id=record.action_id,
                        invocation_id=record.invocation_id,
                        hypothesis_id=record.hypothesis_id,
                        **recovery_attempt,
                    )
                    return {
                        "status": "no_account_proof",
                        "evidence_id": str(evidence_id),
                        "requested_run_id": str(run_id),
                        "companion_evidence_id": str(companion.evidence_id),
                    }
            if not claimed_set <= alias_verified:
                companion = await record_recovery_note(
                    evidence_store=evidence_store,
                    run_id=record.run_id,
                    engagement_id=record.engagement_id,
                    source_evidence_id=str(record.evidence_id),
                    note=(
                        "reprocessing blocked: account has no exact raw session receipt or binding"
                    ),
                    action_id=record.action_id,
                    invocation_id=record.invocation_id,
                    hypothesis_id=record.hypothesis_id,
                    **recovery_attempt,
                )
                return {
                    "status": "no_account_proof",
                    "evidence_id": str(evidence_id),
                    "requested_run_id": str(run_id),
                    "companion_evidence_id": str(companion.evidence_id),
                }
    try:
        stored_claim, stored_flag = await resolve_claim_for_review(
            graph_service, proposal.hypothesis_id, step
        )
    except Exception:
        stored_claim, stored_flag = (None, None)
    try:
        request = VerificationRequest(
            proposal=proposal,
            result=result,
            expectation=expectation,
            source_refs=[str(record.evidence_id), str(result.invocation_id)],
            hypothesis_claim=stored_claim,
            violation_if_confirmed=stored_flag,
            principal_ref=getattr(step, "principal_ref", None) if step is not None else None,
        )
        if (
            step is not None and step.assessment_kind == "observation"
            and proposal.hypothesis_id is None and step.authentication_check is None
        ):
            from redteam.orchestration.analysis import VerificationAssessment

            assessment = VerificationAssessment(
                verdict=Verdict.UNKNOWN, reason="observation only; no security claim to review",
                reviewer_id="analysis:observation_skip", source_refs=request.source_refs,
                outcome="unknown", claim_supported=False,
            )
        elif budget is not None and isinstance(verifier, BudgetedAnalysisVerifier):
            assessment = await verifier.verify_with_budget(
                request,
                budget=budget,
                before_call=before_call,
            )
        elif budget is not None and getattr(step, "delegation_id", None) is not None:
            from redteam.orchestration.analysis import VerificationAssessment

            assessment = VerificationAssessment(
                verdict=Verdict.UNKNOWN,
                reason="analysis verifier does not support delegated request budgets",
                reviewer_id="analysis:budgeting_unsupported",
                source_refs=request.source_refs,
                outcome="unknown",
                claim_supported=False,
            )
        else:
            if before_call is not None:
                await before_call()
            assessment = await verifier.verify(request)
    except Exception:
        from redteam.orchestration.analysis import VerificationAssessment as _VA

        assessment = _VA(
            verdict=Verdict.UNKNOWN,
            reason="meaning review failed; evidence preserved, no verdict reached",
            reviewer_id="analysis:error:recovery",
            source_refs=[str(record.evidence_id), str(result.invocation_id)],
            outcome="unknown",
            claim_supported=False,
        )
    verdict, reason = assessment.verdict, assessment.reason
    # Observation steps never approve: downgrade before receipt/hypothesis/finding.
    if step is not None and step.assessment_kind != "security_check":
        from redteam.orchestration.analysis import VerificationAssessment as _VC

        _prior_stage = getattr(assessment, "last_record_stage", None)
        assessment = _VC(
            verdict=Verdict.UNKNOWN,
            reason="response recorded as an observation; no security claim was verified",
            reviewer_id=getattr(assessment, "reviewer_id", "analysis:recovery"),
            source_refs=list(getattr(assessment, "source_refs", [str(record.evidence_id)])),
            outcome="unknown",
            last_record_stage=None,
            claim_supported=False,
        )
        verdict, reason = assessment.verdict, assessment.reason
    receipt_stage: str | None = getattr(assessment, "last_record_stage", None)
    supersede_chain: list[str] = []
    if receipt_stage is not None:
        supersede_chain.append(str(receipt_stage))
    try:
        await append_review_receipt(
            evidence_store=evidence_store,
            proposal=proposal,
            result=result,
            assessment=assessment,
            admission="final",
            supersedes=supersede_chain,
        )
        receipt_status = "appended"
        try:
            assessment.last_record_stage = "recovery_final"
        except Exception:
            pass
    except Exception:
        from redteam.orchestration.analysis import VerificationAssessment as _VB

        assessment = _VB(
            verdict=Verdict.UNKNOWN,
            reason="review_receipt_write_failed; evidence preserved, no verdict reached",
            reviewer_id=getattr(assessment, "reviewer_id", "analysis:recovery"),
            source_refs=list(getattr(assessment, "source_refs", [str(record.evidence_id)])),
            outcome="unknown",
            claim_supported=False,
        )
        verdict, reason = assessment.verdict, assessment.reason
        receipt_status = "failed"
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
            hypothesis_status = hypothesis_status_for_assessment(assessment)
        await graph_service.resolve_hypothesis(
            str(proposal.hypothesis_id), status=hypothesis_status
        )
    support_audit_note: str | None = None
    if proposal.hypothesis_id is not None:
        try:
            outcome_name = getattr(assessment, "outcome", None)
            supporting_ids: list[str] = []
            refuting_ids: list[str] = []
            if outcome_name == "success":
                supporting_ids = [str(record.evidence_id)]
            elif outcome_name == "refuted":
                refuting_ids = [str(record.evidence_id)]
            from redteam.attack_graph.identity import hypothesis_node_id as _hid
            from redteam.core.enums import GraphNodeType as _GNT

            await graph_service.upsert_node(
                _GNT.HYPOTHESIS,
                node_id=_hid(str(proposal.hypothesis_id)),
                facts={
                    "supporting_evidence_ids": supporting_ids,
                    "refuting_evidence_ids": refuting_ids,
                    "verification_outcome": outcome_name,
                    "verification_verdict": verdict.value
                    if hasattr(verdict, "value")
                    else str(verdict),
                },
                provenance=provenance,
            )
        except Exception as exc:
            try:
                audit_note = await record_recovery_note(
                    evidence_store=evidence_store,
                    run_id=record.run_id,
                    engagement_id=record.engagement_id,
                    source_evidence_id=str(record.evidence_id),
                    note=(
                        "hypothesis support assignment failed ("
                        + type(exc).__name__
                        + ") for "
                        + str(proposal.hypothesis_id)
                        + ": outcome="
                        + str(getattr(assessment, "outcome", None) or "unknown")
                    ),
                    record_kind="hypothesis_support_audit",
                    action_id=proposal.action_id,
                    invocation_id=result.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    **recovery_attempt,
                )
                support_audit_note = "support_audit=" + str(audit_note.evidence_id)
            except Exception as exc2:
                support_audit_note = (
                    "support_audit_note_failed:"
                    + type(exc2).__name__
                    + "(upsert:"
                    + type(exc).__name__
                    + ")"
                )
    finding_id = None
    if (
        verdict is Verdict.VIOLATION and proposal.hypothesis_id is not None
        and proposal.target is not None
    ):
        existing_claims = claim_keys_from_findings(await graph_service.list_findings())
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
            reason = "violation was not promoted to a Finding: " + promotion.refused_reason
    # Pre-seeded so the companion note below never KeyErrors when mapping
    # is skipped (no mappers configured): the note reports zeros, honestly.
    mutation_summary = {"nodes": 0, "edges": 0, "target_references": 0}
    mapper_failure = None
    mapping_status = "skipped"
    replay_writes_planned = False
    replay_processing_status = "unknown"
    replay_processing_stage = None
    # The outcome is computed for every replayed result so failures stay
    # visible; graph writes below stay success-gated, mirroring UPDATE.
    if result_mappers is not None:
        map_with_outcome = getattr(result_mappers, "map_result_with_outcome", None)
        if map_with_outcome is not None:
            outcome = map_with_outcome(
                tool_id=result.tool_id,
                target=proposal.target,
                arguments=dict(proposal.arguments),
                result=result,
            )
            mutation = outcome.mutation
            mapping_status = outcome.status
        else:
            mutation = result_mappers.map_result(
                tool_id=result.tool_id,
                target=proposal.target,
                arguments=dict(proposal.arguments),
                result=result,
            )
        # Call-local failure read: the shared last_failure slot stays only
        # for the legacy map_result path above, the outcome carries the rest.
        if map_with_outcome is not None:
            if outcome.status == "failed":
                mapper_failure = {
                    "tool_id": outcome.tool_id,
                    "mapper": outcome.mapper or "unknown",
                    "reason": outcome.reason or "unknown",
                }
        else:
            failure = result_mappers.last_failure
            mapping_status = "failed" if failure is not None else "mapped"
            if failure is not None:
                mapper_failure = {
                    "tool_id": failure.tool_id,
                    "mapper": failure.mapper,
                    "reason": failure.reason,
                }
        mutation_summary = {
            "nodes": len(mutation.nodes),
            "edges": len(mutation.edges),
            "target_references": len(getattr(mutation, "target_references", [])),
        }
        # Graph writes stay success-gated: a failed replay still reports its
        # mapping outcome above, but never promotes output into the graph.
        nodes_to_apply = mutation.nodes if result.success else []
        edges_to_apply = mutation.edges if result.success else []
        # References ride the same companion channel as UPDATE: only entries
        # grounded against the preserved raw payload are stored, never nodes.
        if getattr(mutation, "target_references", []):
            try:
                from redteam.orchestration.observations.reference_scan import (
                    is_reference_grounded,
                )

                grounded = []
                for entry in mutation.target_references:
                    draft = entry
                    if draft.source_evidence_id is None:
                        draft = draft.model_copy(
                            update={"source_evidence_id": str(record.evidence_id)}
                        )
                    if is_reference_grounded(
                        draft,
                        tool_result=raw if isinstance(raw, dict) else {},
                        action_id=str(proposal.action_id),
                        invocation_id=str(result.invocation_id),
                        evidence_id=str(record.evidence_id),
                    ):
                        grounded.append(draft)
                if grounded:
                    await record_target_references(
                        evidence_store=evidence_store,
                        run_id=record.run_id,
                        engagement_id=record.engagement_id,
                        source_evidence_id=str(record.evidence_id),
                        tool_id=str(result.tool_id),
                        references=[item.model_dump(mode="json") for item in grounded],
                        action_id=proposal.action_id,
                        invocation_id=result.invocation_id,
                        hypothesis_id=proposal.hypothesis_id,
                        **recovery_attempt,
                    )
            except Exception:  # noqa: BLE001 - references must not break recovery
                pass
        # Same processing channel as UPDATE, same helper: before-attempt
        # intent, confirmed counters per phase, and a partial/failed
        # companion on exception. No tool rerun, no ToolResult rewrite.
        replay_writes_planned = bool(nodes_to_apply or edges_to_apply)
        replay_confirmed_nodes = 0
        replay_confirmed_edges = 0
        replay_stage: str | None = None
        replay_failed_at: str | None = None
        if replay_writes_planned:
            try:
                await record_processing_attempt(
                    evidence_store=evidence_store,
                    run_id=record.run_id,
                    engagement_id=record.engagement_id,
                    source_evidence_id=str(record.evidence_id),
                    tool_id=str(result.tool_id),
                    processing=ProcessingAttempt(
                        status="attempted",
                        attempted_nodes=len(nodes_to_apply),
                        attempted_edges=len(edges_to_apply),
                    ),
                    action_id=proposal.action_id,
                    invocation_id=result.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    **recovery_attempt,
                )
            except Exception:  # noqa: BLE001 - the attempt note must not break recovery
                pass
        replay_failed_at = "nodes"
        try:
            for node_mutation in nodes_to_apply:
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
                replay_confirmed_nodes += 1
            replay_stage = "nodes"
            replay_failed_at = "edges"
            for edge_mutation in edges_to_apply:
                await graph_service.link(
                    edge_mutation.edge_type,
                    edge_mutation.source_id,
                    edge_mutation.target_id,
                    edge_mutation.properties,
                    provenance=provenance,
                )
                replay_confirmed_edges += 1
            replay_stage = "done"
            if replay_writes_planned:
                try:
                    await record_processing_attempt(
                        evidence_store=evidence_store,
                        run_id=record.run_id,
                        engagement_id=record.engagement_id,
                        source_evidence_id=str(record.evidence_id),
                        tool_id=str(result.tool_id),
                        processing=ProcessingAttempt(
                            status="confirmed",
                            stage="done",
                            attempted_nodes=len(nodes_to_apply),
                            attempted_edges=len(edges_to_apply),
                            confirmed_nodes=replay_confirmed_nodes,
                            confirmed_edges=replay_confirmed_edges,
                        ),
                        action_id=proposal.action_id,
                        invocation_id=result.invocation_id,
                        hypothesis_id=proposal.hypothesis_id,
                        **recovery_attempt,
                    )
                except Exception:  # noqa: BLE001 - the companion must not break recovery
                    pass
        except Exception as exc:  # noqa: BLE001 - partial state is recorded on the same channel
            if replay_writes_planned:
                try:
                    await record_processing_attempt(
                        evidence_store=evidence_store,
                        run_id=record.run_id,
                        engagement_id=record.engagement_id,
                        source_evidence_id=str(record.evidence_id),
                        tool_id=str(result.tool_id),
                        processing=ProcessingAttempt(
                            status=(
                                "partial"
                                if (replay_confirmed_nodes or replay_confirmed_edges)
                                else "failed"
                            ),
                            stage=replay_stage,
                            attempted_nodes=len(nodes_to_apply),
                            attempted_edges=len(edges_to_apply),
                            confirmed_nodes=replay_confirmed_nodes,
                            confirmed_edges=replay_confirmed_edges,
                            failed_at=replay_failed_at,
                            reason=type(exc).__name__,
                        ),
                        action_id=proposal.action_id,
                        invocation_id=result.invocation_id,
                        hypothesis_id=proposal.hypothesis_id,
                        **recovery_attempt,
                    )
                except Exception:  # noqa: BLE001 - the companion must not mask the failure
                    pass
            raise
    # The write try above re-raises, so reaching here means the replayed
    # writes (if any) are confirmed; the summary carries the last state.
    if replay_writes_planned:
        replay_processing_status = "confirmed"
        replay_processing_stage = "done"
    parts = [
        "reprocessed " + str(record.evidence_id) + ": verdict=" + verdict.value,
        "hypothesis="
        + (
            str(proposal.hypothesis_id) + "->" + hypothesis_status
            if proposal.hypothesis_id is not None
            else "none"
        ),
        "finding=" + (finding_id or "none"),
        "mutation nodes="
        + str(mutation_summary["nodes"])
        + " edges="
        + str(mutation_summary["edges"]),
        " references="
        + str(mutation_summary["target_references"])
        + " mapping="
        + str(mapping_status),
    ]
    if step_degraded:
        parts.append("step context unreadable, replayed degraded")
    if mapper_failure is not None:
        parts.append("mapper_failure=" + str(mapper_failure["reason"]))
    parts.append("receipt=" + str(receipt_status))
    parts.append("outcome=" + str(getattr(assessment, "outcome", None) or "unknown"))
    if support_audit_note is not None:
        parts.append(support_audit_note)
    companion = await record_recovery_note(
        evidence_store=evidence_store,
        run_id=record.run_id,
        engagement_id=record.engagement_id,
        source_evidence_id=str(record.evidence_id),
        note="; ".join(parts),
        record_kind="recovery_result",
        mapping_status=mapping_status,
        processing_status=replay_processing_status,
        processing_stage=replay_processing_stage,
        recovery_status="ok",
        action_id=proposal.action_id,
        invocation_id=result.invocation_id,
        hypothesis_id=proposal.hypothesis_id,
        **recovery_attempt,
    )
    return {
        "status": "ok",
        **recovery_attempt,
        "evidence_id": str(evidence_id),
        "requested_run_id": str(run_id),
        "verdict": verdict.value,
        "reason": reason,
        "outcome": getattr(assessment, "outcome", None) or "unknown",
        "hypothesis_id": str(proposal.hypothesis_id)
        if proposal.hypothesis_id is not None
        else None,
        "hypothesis_status": hypothesis_status,
        "finding_id": finding_id,
        "mutation": mutation_summary,
        "mapper_failure": mapper_failure,
        "mapping_status": mapping_status,
        "processing_status": replay_processing_status,
        "processing_stage": replay_processing_stage,
        "companion_evidence_id": str(companion.evidence_id),
        "review_receipt": receipt_status,
    }


def mapping_attempt_key(
    source_evidence_id: str | None,
    action_id: Any | None,
    invocation_id: Any | None,
    attempt: int,
) -> str:
    """Dedup key for one processing attempt over existing identifiers.

    Built only from the call and evidence identifiers plus the processing
    attempt: no new record identifier ever enters the execution meaning key.
    """
    return "|".join(
        [
            str(source_evidence_id or ""),
            str(action_id or ""),
            str(invocation_id or ""),
            str(attempt),
        ]
    )


@dataclass(frozen=True)
class ProcessingAttempt:
    """One apply pass over a mapped mutation: tried versus confirmed.

    status is attempted (writes starting), confirmed (all applied),
    partial (some applied, then failed) or failed (none applied, error).
    stage is the last completed write phase: None, nodes, edges,
    findings or done. Counts are confirmed writes, except attempted_*
    which state intent. The original record is never touched: this
    travels on an immutable companion next to it.
    """

    status: str
    stage: str | None = None
    attempted_nodes: int = 0
    attempted_edges: int = 0
    attempted_findings: int = 0
    confirmed_nodes: int = 0
    confirmed_edges: int = 0
    confirmed_findings: int = 0
    failed_at: str | None = None
    reason: str | None = None


async def record_processing_attempt(
    *,
    evidence_store: Any,
    run_id: Any,
    engagement_id: Any,
    source_evidence_id: str | None,
    tool_id: str,
    processing: ProcessingAttempt,
    action_id: Any | None = None,
    invocation_id: Any | None = None,
    hypothesis_id: Any | None = None,
    attempt: int = 1,
    supersedes_attempt: int | None = None,
) -> EvidenceRecord:
    """Append an immutable companion stating one processing attempt.

    Same channel as the mapping-failure and reference companions: the
    original evidence record is never rewritten. Root reads
    processing_status / processing_stage off this companion metadata.
    """
    payload = {
        "record_kind": "processing_attempt",
        "source_evidence_id": source_evidence_id,
        "tool_id": tool_id,
        "processing_status": processing.status,
        "processing_stage": processing.stage,
        "processing_outcome": processing.status,
        "attempted": {
            "nodes": processing.attempted_nodes,
            "edges": processing.attempted_edges,
            "findings": processing.attempted_findings,
        },
        "confirmed": {
            "nodes": processing.confirmed_nodes,
            "edges": processing.confirmed_edges,
            "findings": processing.confirmed_findings,
        },
        "failed_at": processing.failed_at,
        "reason": processing.reason,
        "attempt": attempt,
        "mapping_attempt_key": mapping_attempt_key(
            source_evidence_id, action_id, invocation_id, attempt
        ),
    }
    if supersedes_attempt is not None:
        payload["supersedes_attempt"] = supersedes_attempt
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
        summary=(
            "processing "
            + processing.status
            + " for "
            + str(tool_id)
            + " ("
            + str(processing.stage or "start")
            + ")"
        )[:280],
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await evidence_store.append(note)
    return note


async def record_target_references(
    *,
    evidence_store: Any,
    run_id: Any,
    engagement_id: Any,
    source_evidence_id: str,
    tool_id: str,
    references: list[Any],
    action_id: Any | None = None,
    invocation_id: Any | None = None,
    hypothesis_id: Any | None = None,
    attempt: int = 1,
    supersedes_attempt: int | None = None,
) -> EvidenceRecord:
    """Append a companion record carrying reference candidates.

    The original evidence record is never touched: entries travel on this
    SYSTEM companion through source_evidence_id, each already echoing the
    origin identifiers. No graph node or fact is created here or implied.
    """
    payload = {
        "record_kind": "target_references",
        "source_evidence_id": source_evidence_id,
        "tool_id": tool_id,
        "target_references": list(references),
        "attempt": attempt,
        "mapping_attempt_key": mapping_attempt_key(
            source_evidence_id, action_id, invocation_id, attempt
        ),
    }
    if supersedes_attempt is not None:
        payload["supersedes_attempt"] = supersedes_attempt
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
        summary=("target references for " + str(tool_id) + " (" + str(len(list(references))) + ")")[
            :280
        ],
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await evidence_store.append(note)
    return note


async def record_mapping_failure(
    *,
    evidence_store: Any,
    run_id: Any,
    engagement_id: Any,
    source_evidence_id: str | None,
    tool_id: str,
    mapper: str,
    reason: str,
    attempt: int = 1,
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
        "attempt": attempt,
        "mapping_attempt_key": mapping_attempt_key(
            source_evidence_id, action_id, invocation_id, attempt
        ),
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
    mapping_status: str | None = None,
    processing_status: str | None = None,
    processing_stage: str | None = None,
    recovery_status: str | None = None,
    attempt: int | None = None,
    supersedes_attempt: int | None = None,
) -> EvidenceRecord:
    """Append a free-form companion note next to an existing record."""
    payload: dict[str, Any] = {
        "record_kind": record_kind,
        "source_evidence_id": source_evidence_id,
        "note": note,
    }
    if attempt is not None:
        payload["attempt"] = attempt
        payload["mapping_attempt_key"] = mapping_attempt_key(
            source_evidence_id, action_id, invocation_id, attempt
        )
    if supersedes_attempt is not None:
        payload["supersedes_attempt"] = supersedes_attempt
    # Structured last-state mirrors so root projects without parsing note.
    if mapping_status is not None:
        payload["mapping_status"] = mapping_status
        payload["mapping_outcome"] = mapping_status
    if processing_status is not None:
        payload["processing_status"] = processing_status
        payload["processing_outcome"] = processing_status
    if processing_stage is not None:
        payload["processing_stage"] = processing_stage
    if recovery_status is not None:
        payload["recovery_status"] = recovery_status
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
        raw = raw_from_metadata(getattr(record, "metadata", None))
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
        "target_reference_companions": kinds.get("target_references", 0),
    }
