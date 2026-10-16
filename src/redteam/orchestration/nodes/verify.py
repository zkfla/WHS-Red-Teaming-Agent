"""VERIFY — append Evidence, then reach a ``Verdict`` against the
expectation recorded before the action ran. A VIOLATION produces a
verified ``Finding`` carrying the ``EvidenceId`` behind it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from redteam.attack_graph.identity import (
    hypothesis_node_id,
    node_id_for_target,
    session_credential_node_id,
    session_principal_node_id,
)
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import HYPOTHESIS_INCONCLUSIVE, HYPOTHESIS_OPEN
from redteam.core.contracts.events import EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.convert import digest_payload, tool_result_to_evidence
from redteam.core.enums import (
    AgentPhase,
    EvidenceSourceType,
    GraphEdgeType,
    GraphNodeType,
    Verdict,
)
from redteam.core.ids import EvidenceId, new_id
from redteam.core.tool_binding import execution_display_key
from redteam.orchestration.analysis import (
    BudgetedAnalysisVerifier,
    VerificationAssessment,
    VerificationRequest,
    append_review_receipt,
    hypothesis_status_for_assessment,
    resolve_claim_for_review,
)
from redteam.orchestration.analysis_budget import verification_budget, verification_guard
from redteam.orchestration.authentication import (
    ACCESS_CONTROL_VIOLATION,
    assess_authentication,
)
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.delegation_decision import record_decision_consumption
from redteam.orchestration.diagnostics import operational_exception_diagnostic
from redteam.orchestration.events import (
    AUTHENTICATION_CHECKED,
    HYPOTHESIS_RESOLVED,
    OBSERVATION_RECORDED,
)
from redteam.orchestration.findings import (
    FindingPromoter,
    PromotionRequest,
    claim_keys_from_findings,
)
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.runtime_observation import (
    RuntimeObservation,
    append_runtime_observation,
)
from redteam.orchestration.service_status import (
    severity_for,
)
from redteam.orchestration.state import GraphState


def _follow_up_possible(step: object | None) -> bool:
    """Whether a further verification of this question can still run.

    The Work Ledger permanently dedups a candidate once it is terminal
    unless its step is repeatable, so a non-repeatable step genuinely has
    no follow-up available -- leaving its hypothesis open would park the run
    on a question nothing will ever ask again.
    """

    return bool(getattr(step, "repeatable", False))


async def verify_node(
    state: GraphState,
    *,
    deps: RuntimeDeps,
    budget: DecisionBudget | None = None,
    requester_guard: Callable[[], Awaitable[None]] | None = None,
) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    step = state.get("plan_step")
    assert proposal is not None and result is not None
    budget = budget if budget is not None else verification_budget(state, deps=deps)
    attempts_before = budget.used
    tokens_before = budget.tokens_used
    unknown_before = budget.unknown_usage_count

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.verify")
    service = graph_service(deps)

    evidence = tool_result_to_evidence(
        result,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        hypothesis_id=proposal.hypothesis_id,
        summary=_summarize_result(result),
    )
    # Target-independent execution: a targetless proposal has no graph
    # target to name. The key is left out, never invented; the action,
    # invocation, and proposal IDs below still identify the call.
    evidence_metadata_patch: dict = {}
    if proposal.target is not None:
        evidence_metadata_patch["target_node_id"] = str(node_id_for_target(proposal.target))
    evidence = evidence.model_copy(
        update={
            "metadata": {
                **evidence.metadata,
                **evidence_metadata_patch,
                "agent_id": str(proposal.agent_id),
                "capability": proposal.capability,
                "operation": proposal.operation,
                "candidate_key": step.candidate_key if step is not None else "",
                **_evidence_metadata(
                    proposal, result, step, runtime,
                    work_id=state.get("current_work_id"),
                ),
            }
        }
    )
    # Read the selected mapper's validated observations. Physical tool ids
    # and tools sharing the comparison mapper use the same evidence path.
    # The per-call outcome travels on the evidence record itself so every
    # preserved result carries its mapping status; the raw tool_result
    # payload stays immutable and no_mapper never refuses execution.
    outcome = deps.result_mappers.map_result_with_outcome(
        tool_id=result.tool_id, target=proposal.target,
        arguments=proposal.arguments, result=result,
    )
    mutation = outcome.mutation
    evidence.metadata["mapping_status"] = outcome.status
    evidence.metadata["mapping_outcome"] = outcome.status
    responses = [
        item
        for node in mutation.nodes
        for item in node.observations
        if item.get("kind") == "http_response"
    ]
    if responses:
        evidence.metadata["http_observation"] = responses[0]
    comparisons = [
        item
        for node in mutation.nodes
        for item in node.observations
        if item.get("kind") == "http_session_comparison"
    ]
    if comparisons:
        evidence.metadata["session_comparison"] = comparisons[0]
    if not result.success and result.error is not None:
        # RC#2: keep the non-secret failure code so a later planning turn can
        # read *why* a step failed and adapt. The free-text message and
        # details may carry target data, so only the code/retryable flag --
        # both bounded and non-secret -- are recorded here.
        evidence.metadata["error"] = {
            "code": result.error.code,
            "retryable": result.error.retryable,
        }
    if deps.methodology_library is not None:
        evidence.metadata.update(deps.methodology_library.capture(proposal, result))
    await deps.evidence_store.append(evidence)

    created = await publisher.publish(
        EventType.EVIDENCE_CREATED,
        {
            "evidence_id": evidence.evidence_id,
            "action_id": proposal.action_id,
            "invocation_id": evidence.invocation_id,
            "hypothesis_id": proposal.hypothesis_id,
            "candidate_key": step.candidate_key if step is not None else "",
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )
    last_event_id = created.event_id

    expectation = step.expectation if step is not None else None
    verification_failed = False
    verification_diagnostic = None
    # Pure observation with no hypothesis and no specialized check claims
    # nothing: skip the meaning-review call and admit UNKNOWN directly.
    # Anything with a hypothesis or an authentication check still goes
    # through the verifier below.
    observation_skip = (
        step is not None
        and step.assessment_kind == "observation"
        and proposal.hypothesis_id is None
        and step.authentication_check is None
    )
    try:
        hypothesis_claim_text, violation_flag = await resolve_claim_for_review(
            service, proposal.hypothesis_id, step
        )
    except Exception:
        hypothesis_claim_text, violation_flag = (None, None)
    if observation_skip:
        assessment = VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason="observation only; no hypothesis or security claim to review",
            reviewer_id="analysis:observation_skip",
            source_refs=[str(evidence.evidence_id), str(result.invocation_id)],
            outcome="unknown",
            claim_supported=False,
        )
    else:
        try:
            assert deps.verifier is not None
            request = VerificationRequest(
                proposal=proposal,
                result=result,
                expectation=expectation,
                source_refs=[str(evidence.evidence_id), str(result.invocation_id)],
                hypothesis_claim=hypothesis_claim_text,
                violation_if_confirmed=violation_flag,
                principal_ref=getattr(step, "principal_ref", None) if step is not None else None,
            )
            before_call = requester_guard or verification_guard(state, deps=deps, budget=budget)
            if isinstance(deps.verifier, BudgetedAnalysisVerifier):
                assessment = await deps.verifier.verify_with_budget(
                    request, budget=budget, before_call=before_call
                )
            elif getattr(step, "delegation_id", None) is not None:
                # An unmetered verifier may hide model calls/retries. Preserve
                # the original evidence rather than bypass a delegated limit.
                assessment = VerificationAssessment(
                    verdict=Verdict.UNKNOWN,
                    reason="analysis verifier does not support delegated request budgets",
                    reviewer_id="analysis:budgeting_unsupported",
                    source_refs=request.source_refs,
                    outcome="unknown",
                    claim_supported=False,
                )
            else:
                await before_call()
                assessment = await deps.verifier.verify(request)
        except Exception as exc:  # noqa: BLE001 - preserve evidence and close work safely
            verification_failed = True
            verification_diagnostic = operational_exception_diagnostic(
                exc,
                stage="verify",
                code="verification_failed",
                summary="analysis verifier failed before reaching a verdict",
            )
            result = result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        "verification_exception": type(exc).__name__,
                        "verification_diagnostic": verification_diagnostic.model_dump(
                            mode="json"
                        ),
                    }
                }
            )
            assessment = VerificationAssessment(
                verdict=Verdict.UNKNOWN,
                reason="analysis verifier failed before reaching a verdict",
                reviewer_id=f"analysis:error:{type(exc).__name__}",
                source_refs=[str(evidence.evidence_id), str(result.invocation_id)],
                outcome="unknown",
                claim_supported=False,
            )
    verdict, reason = assessment.verdict, assessment.reason

    # The public receipt is appended once below as the final admission,
    # after contradiction/observation/failure downgrades. The verifier may
    # already have left an interim receipt; the final one supersedes it so a
    # stale positive can never count as approval.
    prior_record_stage = getattr(assessment, "last_record_stage", None)

    provenance = Provenance(
        step=runtime.step_count,
        source=f"tool:{result.tool_id}",
        evidence_id=str(evidence.evidence_id),
        run_id=str(runtime.run_id),
        confidence=Confidence.MEASURED,
    )
    if deps.methodology_library is not None and (
        "methodology_chunk" in evidence.metadata or "methodology_rejected" in evidence.metadata
    ):
        # Paging changes evidence even when the same operation/Asset is reused.
        # Publish only bounded progress, never document bodies or permission.
        progress = deps.methodology_library.progress(
            await deps.evidence_store.list_for_run(runtime.run_id)
        )
        # No measured target, no measured node: progress without a target
        # stays on the evidence record, never as a graph fact.
        if proposal.target is not None:
            await service.observe_target(
                proposal.target,
                facts={"methodology_progress": list(progress)},
                provenance=provenance,
                step=runtime.step_count,
            )

    auth_assessment = None
    auth_contradiction = False
    if step is not None and step.authentication_check is not None:
        auth_assessment = await assess_authentication(
            step.authentication_check,
            proposal,
            result,
            evidence,
            deps.evidence_store,
        )
        comparison = {
            **auth_assessment.model_dump(),
            "session_ref": step.authentication_check.session_ref,
            "path": proposal.arguments.get("path"),
            "expected_access": step.authentication_check.expected_access,
        }
        # Authentication facts describe a measured target; without one
        # the comparison still travels on the event below, never as a
        # graph fact about an invented node.
        if proposal.target is not None:
            await service.observe_target(
                proposal.target,
                facts={"authentication_checks": [comparison]},
                provenance=provenance,
                step=runtime.step_count,
            )
        if proposal.hypothesis_id is not None:
            await service.upsert_node(
                GraphNodeType.HYPOTHESIS,
                node_id=hypothesis_node_id(str(proposal.hypothesis_id)),
                facts={
                    "evidence_ids": auth_assessment.evidence_ids,
                    "authentication_outcome": auth_assessment.outcome,
                },
                provenance=provenance,
                step=runtime.step_count,
            )
        if proposal.target is not None and auth_assessment.outcome in {
            "session_access_observed", ACCESS_CONTROL_VIOLATION,
        }:
            # Session nodes name a measured target; without one there is
            # no session identity to record. The verdict path below still
            # runs on the preserved evidence.
            target_ref = str(proposal.arguments.get("target_ref") or "")
            if not target_ref and proposal.target is not None:
                target_ref = str(node_id_for_target(proposal.target))
            session_ref = step.authentication_check.session_ref
            principal = await service.upsert_node(
                GraphNodeType.PRINCIPAL,
                node_id=session_principal_node_id(target_ref, session_ref),
                attrs={"authenticated": True, "operator_managed_session": True},
                facts={"target_ref": target_ref, "session_refs": [session_ref]},
                provenance=provenance,
                step=runtime.step_count,
            )
            credential = await service.upsert_node(
                GraphNodeType.CREDENTIAL,
                node_id=session_credential_node_id(target_ref, session_ref),
                attrs={"reference_only": True},
                facts={
                    "target_ref": target_ref,
                    "session_ref": session_ref,
                    "credential_kind": "session_reference",
                },
                provenance=provenance,
                step=runtime.step_count,
            )
            await service.link(
                GraphEdgeType.RELATED_TO,
                credential.id,
                principal.id,
                properties={"relation": "session_for"},
                provenance=provenance,
            )
            await service.link(
                GraphEdgeType.CAN_ACCESS,
                principal.id,
                node_id_for_target(proposal.target),
                properties={
                    "outcome": auth_assessment.outcome,
                    "expected_access": step.authentication_check.expected_access,
                },
                provenance=provenance,
            )
        compared = await publisher.publish(
            AUTHENTICATION_CHECKED,
            {
                **comparison,
                "action_id": proposal.action_id,
                "hypothesis_id": proposal.hypothesis_id,
                # The step's own declaration, not the outcome. A security_check
                # whose deny expectation was *enforced* is still a security
                # check -- labelling it "observation" would misreport what was
                # under test, and the label is what downstream consumers use to
                # decide whether a conclusion was claimed at all.
                "assessment_kind": step.assessment_kind,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        last_event_id = compared.event_id
        # Code never raises a verdict. The comparison above stays mapped as
        # physical observations; the semantic verdict comes only from the LLM.
        # A comparison that contradicts a positive LLM judgment on the same
        # hypothesis falls back to UNKNOWN with feedback for replan.
        llm_outcome = getattr(assessment, "outcome", None)
        llm_dir: str | None = None
        if not verification_failed and assessment.verdict is not Verdict.UNKNOWN:
            if llm_outcome == "success":
                llm_dir = "confirm"
            elif llm_outcome == "refuted":
                llm_dir = "reject"
        auth_outcome = auth_assessment.outcome
        auth_dir: str | None = None
        if auth_outcome in {ACCESS_CONTROL_VIOLATION, "session_access_observed"}:
            auth_dir = "confirm"
        elif auth_outcome in {"session_not_accepted", "access_denied"}:
            auth_dir = "reject"
        auth_contradiction = llm_dir is not None and auth_dir is not None and llm_dir != auth_dir

    observation_only = step is not None and step.assessment_kind == "observation"
    if observation_only:
        observed = await publisher.publish(
            OBSERVATION_RECORDED,
            {
                "evidence_id": evidence.evidence_id,
                "action_id": proposal.action_id,
                **evidence_metadata_patch,
                "assessment_kind": "observation",
                "tool_success": result.success,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        last_event_id = observed.event_id

    # Final admission: contradiction, observation-only, or verification failure
    # downgrades the code-approved assessment itself to outcome=unknown. The
    # auth branch below can never re-raise it (no re-promotion), and support
    # evidence plus the final receipt derive from this admitted assessment.
    downgrade_cause: str | None = None
    if verification_failed:
        downgrade_cause = "verification_failed"
    elif observation_only:
        downgrade_cause = "observation_only"
    elif auth_contradiction:
        downgrade_cause = "auth_contradiction"
    if downgrade_cause is not None:
        admitted = VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason=(
                "admitted UNKNOWN ("
                + downgrade_cause
                + "); original "
                + str(
                    assessment.verdict.value
                    if hasattr(assessment.verdict, "value")
                    else assessment.verdict
                )
                + "/"
                + str(getattr(assessment, "outcome", None) or "unknown")
                + ": "
                + str(assessment.reason)
            )[:1000],
            reviewer_id=assessment.reviewer_id,
            source_refs=list(assessment.source_refs),
            outcome="unknown",
            last_record_stage=getattr(assessment, "last_record_stage", None),
            claim_supported=False,
        )
    else:
        admitted = assessment
    verdict, reason = admitted.verdict, admitted.reason

    # Final admission receipt: supersedes any interim receipt so a stale
    # positive can never count as approval. Completion consumers must require
    # admission == "final" (see is_final_admission_receipt in analysis).
    superseded_stages: list[str] = []
    if prior_record_stage is not None:
        superseded_stages.append(str(prior_record_stage))
    try:
        await append_review_receipt(
            evidence_store=deps.evidence_store,
            proposal=proposal,
            result=result,
            assessment=admitted,
            admission="final",
            supersedes=superseded_stages,
            diagnostic=verification_diagnostic,
        )
        try:
            admitted.last_record_stage = "verify_node_final"
        except Exception:
            pass
    except Exception:
        result = result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "verification_exception": "review_receipt_write_failed",
                }
            }
        )
        verification_failed = True
        admitted = VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason="review_receipt_write_failed; evidence preserved, no verdict reached",
            reviewer_id=admitted.reviewer_id,
            source_refs=list(admitted.source_refs),
            outcome="unknown",
            last_record_stage=None,
            claim_supported=False,
        )
        verdict, reason = admitted.verdict, admitted.reason

    hypothesis_status = ""
    resolution_reason = ""
    if proposal.hypothesis_id is not None:
        hypothesis_status = hypothesis_status_for_assessment(admitted)
        if (
            observation_only
            or result.metadata.get("orchestration_exception")
            or verification_failed
        ):
            hypothesis_status = "inconclusive"
        if auth_assessment is not None:
            # Guarded adoption: same hypothesis and same outcome direction only.
            # Anything else stays inconclusive; code never confirms via override.
            auth_status = auth_assessment.hypothesis_status
            auth_dir = None
            if auth_status == "confirmed":
                auth_dir = "confirm"
            elif auth_status == "rejected":
                auth_dir = "reject"
            if admitted.verdict is Verdict.UNKNOWN or observation_only:
                hypothesis_status = "inconclusive"
                auth_dir = None
            base_outcome = getattr(admitted, "outcome", None)
            base_dir: str | None = None
            if not verification_failed and base_outcome == "success":
                base_dir = "confirm"
            elif not verification_failed and base_outcome == "refuted":
                base_dir = "reject"
            if auth_dir is not None and auth_dir == base_dir and base_dir is not None:
                hypothesis_status = auth_status
            elif auth_dir is not None and base_dir is not None and auth_dir != base_dir:
                hypothesis_status = "inconclusive"
            if expectation is None and not observation_only and not resolution_reason:
                resolution_reason = "needs_test_expectation_for_claim"
        if hypothesis_status == HYPOTHESIS_OPEN and not _follow_up_possible(step):
            hypothesis_status = HYPOTHESIS_INCONCLUSIVE
            resolution_reason = "no_follow_up_verification_available"
        resolved = await service.resolve_hypothesis(
            str(proposal.hypothesis_id),
            status=hypothesis_status,
            step=runtime.step_count,
        )
        try:
            outcome_name = getattr(admitted, "outcome", None)
            supporting_ids: list[str] = []
            refuting_ids: list[str] = []
            if outcome_name == "success":
                supporting_ids = [str(evidence.evidence_id)]
            elif outcome_name == "refuted":
                refuting_ids = [str(evidence.evidence_id)]
            await service.upsert_node(
                GraphNodeType.HYPOTHESIS,
                node_id=hypothesis_node_id(str(proposal.hypothesis_id)),
                facts={
                    "supporting_evidence_ids": supporting_ids,
                    "refuting_evidence_ids": refuting_ids,
                    "verification_outcome": outcome_name,
                    "verification_verdict": verdict.value,
                },
                provenance=provenance,
                step=runtime.step_count,
            )
        except Exception as exc:
            try:
                audit_payload: dict = {
                    "record_kind": "hypothesis_support_audit",
                    "status": "failed",
                    "hypothesis_id": str(proposal.hypothesis_id),
                    "verification_outcome": outcome_name,
                    "verification_verdict": verdict.value,
                    "error": type(exc).__name__,
                    "source_evidence_id": str(evidence.evidence_id),
                }
                audit_record = EvidenceRecord(
                    evidence_id=EvidenceId(new_id("ev")),
                    run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_type=EvidenceSourceType.SYSTEM,
                    source_id="orchestration.verify",
                    action_id=proposal.action_id,
                    invocation_id=result.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    content_type="application/json",
                    artifact_ref=None,
                    digest=digest_payload(audit_payload),
                    summary=("hypothesis support audit failed for " + str(proposal.hypothesis_id))[
                        :280
                    ],
                    created_at=datetime.now(timezone.utc),
                    metadata=audit_payload,
                )
                await deps.evidence_store.append(audit_record)
            except Exception:
                result = result.model_copy(
                    update={
                        "metadata": {
                            **result.metadata,
                            "support_audit_write_failed": "hypothesis_support_audit",
                        }
                    }
                )
        if resolved is not None:
            settled = await publisher.publish(
                HYPOTHESIS_RESOLVED,
                {
                    "hypothesis_id": str(proposal.hypothesis_id),
                    "status": hypothesis_status,
                    "reason": resolution_reason or reason,
                    "verdict": verdict.value,
                    "action_id": proposal.action_id,
                    "candidate_key": step.candidate_key if step is not None else "",
                },
                correlation_id=correlation_id,
                causation_id=last_event_id,
            )
            last_event_id = settled.event_id

    existing_claims = claim_keys_from_findings(await service.list_findings())
    decision = FindingPromoter().promote(
        PromotionRequest(
            verdict=verdict,
            reason=reason,
            evidence_ids=(
                list(auth_assessment.evidence_ids)
                if auth_assessment is not None
                else [str(evidence.evidence_id)]
            ),
            hypothesis_id=str(proposal.hypothesis_id) if proposal.hypothesis_id else None,
            hypothesis_status=hypothesis_status,
            target_node_id=str(node_id_for_target(proposal.target)) if proposal.target else "",
            capability=proposal.capability,
            operation=proposal.operation,
            agent_id=str(proposal.agent_id),
            action_id=str(proposal.action_id),
            reviewer_id=admitted.reviewer_id,
            source_refs=list(admitted.source_refs),
            severity=severity_for(step),
            assessment_kind=(step.assessment_kind if step is not None else "observation"),
        ),
        existing_claims=existing_claims,
    )
    finding = decision.finding
    if finding is not None and proposal.target is not None:
        await service.record_finding(
            finding,
            hypothesis_id=str(proposal.hypothesis_id) if proposal.hypothesis_id else None,
            about=node_id_for_target(proposal.target),
            step=runtime.step_count,
            provenance=provenance,
        )
        finding_created = await publisher.publish(
            EventType.FINDING_CREATED,
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "evidence_ids": finding.evidence_ids,
                "hypothesis_id": proposal.hypothesis_id,
                "action_id": proposal.action_id,
                "candidate_key": step.candidate_key if step is not None else "",
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        finding_verified = await publisher.publish(
            EventType.FINDING_VERIFIED,
            {"finding_id": finding.finding_id, "reason": reason},
            correlation_id=correlation_id,
            causation_id=finding_created.event_id,
        )
        last_event_id = finding_verified.event_id
    elif verdict is Verdict.VIOLATION:
        # A reviewer claimed a violation that could not be grounded. The
        # honest verdict is "no verdict", not "violation with no finding":
        # recording it through the existing observation event keeps one
        # event vocabulary while making the refusal traceable.
        verdict = Verdict.UNKNOWN
        refused_reason = (
            "no measured target for a target finding"
            if proposal.target is None else decision.refused_reason
        )
        reason = f"violation was not promoted to a Finding: {refused_reason}"
        refused = await publisher.publish(
            OBSERVATION_RECORDED,
            {
                "evidence_id": evidence.evidence_id,
                "action_id": proposal.action_id,
                **evidence_metadata_patch,
                "assessment_kind": "observation",
                "tool_success": result.success,
                "finding_promotion_refused": refused_reason,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        last_event_id = refused.event_id

    delegation_id = getattr(step, "delegation_id", None)
    delegation = runtime.delegations.get(delegation_id) if delegation_id is not None else None
    if delegation is not None and requester_guard is None:
        # Persist sequential/approval-resume reviews as well. Isolated workers
        # return these same deltas through WorkerOutcome; their runtime copy
        # is discarded, so aggregation owns the parent-state update.
        spent = record_decision_consumption(
            delegation,
            requests=budget.used - attempts_before,
            tokens=(
                None
                if budget.unknown_usage_count > unknown_before
                else budget.tokens_used - tokens_before
            ),
        )
        runtime = runtime.model_copy(
            update={
                "delegations": {
                    **runtime.delegations,
                    delegation_id: spent,
                }
            }
        )

    admitted_runtime = runtime.model_copy(
        update={
            "phase": AgentPhase.UPDATE,
            "model_requests_used": runtime.model_requests_used + budget.used - attempts_before,
            "model_tokens_used": runtime.model_tokens_used + budget.tokens_used - tokens_before,
            "model_usage_unknown": (
                runtime.model_usage_unknown
                + budget.unknown_usage_count
                - unknown_before
            ),
            "last_verdict": verdict,
            "consecutive_failures": (
                runtime.consecutive_failures + 1
                if verification_failed
                else runtime.consecutive_failures
            ),
        }
    )
    if verification_failed and verification_diagnostic is not None:
        admitted_runtime = append_runtime_observation(
            admitted_runtime,
            RuntimeObservation(
                kind="state",
                summary="Analysis verifier failed before reaching a verdict.",
                outcome="unknown",
                step=runtime.step_count,
                action_id=str(proposal.action_id),
                capability=proposal.capability,
                operation=proposal.operation,
                diagnostic=verification_diagnostic,
                alternative_allowed=True,
            ),
        )
    return advance(
        state,
        admitted_runtime,
        last_event_id=last_event_id,
        tool_result=result,
        result_evidence_id=evidence.evidence_id,
    )


def _summarize_result(result) -> str:
    """One line on what actually happened -- output shape or error code.

    Never the expected text: the plan's hope is stored separately under
    "expected_observation". Values stay out on purpose (keys and counts
    only), so the summary cannot leak payload data into reports.
    """
    tool = str(result.tool_id)
    if result.success:
        output = result.output
        if isinstance(output, dict):
            status = output.get("status_code")
            if type(status) is int:
                return tool + " succeeded (status " + str(status) + ")"
            keys = sorted(str(key) for key in list(output)[:5])
            if keys:
                return tool + " succeeded (output keys: " + ",".join(keys) + ")"
            return tool + " succeeded (empty output)"
        if isinstance(output, str):
            if output.strip():
                return tool + " succeeded (text output, " + str(len(output)) + " chars)"
            return tool + " succeeded (empty output)"
        return tool + " succeeded (no output)"
    code = result.error.code if result.error is not None else "unknown_error"
    return tool + " failed (" + code + ")"


def _is_native_step(step: object | None) -> bool:
    """Whether this step carries native call lineage.

    Native is identified by ``execution_origin_version == 1``. A missing
    path is not sufficient: legacy steps may also lack path refs while
    native steps may carry them for audit. Defaults to legacy (``0``).
    """
    if step is None:
        return False
    version = getattr(step, "execution_origin_version", None)
    if version is None:
        version = getattr(step, "origin_version", 0)
    try:
        return int(version or 0) == 1
    except (TypeError, ValueError):
        return False


def _evidence_metadata(proposal, result, step, runtime, *, work_id=None) -> dict:
    """Metadata for the evidence record: raw payload, split expectation, path refs.

    The summary already carries what happened; here the full raw result is
    preserved for later reprocessing, the plan's expected observation is
    kept as its own entry (never as the summary), and the path position
    (path/version/step/account) is recorded when known -- missing refs are
    left out, never invented.

    Native call lineage (``execution_origin_version`` /
    ``model_tool_call_id`` / ``mission_task_id`` / ``task_purpose_hash`` /
    ``work_id`` / ``candidate_key`` plus ``mission_id`` and the raw
    ``proposal``) travels here so the stored ``ToolResult`` payload stays
    immutable. ``_is_native_step`` is the only origin check; no verifier
    bypass is keyed on origin.
    """
    metadata: dict = {}
    if runtime.task_ownership_version:
        metadata["central_execution_version"] = 1
        metadata["task_ownership_version"] = runtime.task_ownership_version
    if proposal.expected_observation is not None:
        metadata["expected_observation"] = proposal.expected_observation
    metadata["tool_result"] = result.model_dump(mode="json")
    metadata["action_id"] = str(proposal.action_id)
    metadata["invocation_id"] = str(result.invocation_id)
    metadata["tool_id"] = str(result.tool_id)
    metadata["canonical_operation"] = execution_display_key(proposal)
    metadata["binding_mode"] = proposal.binding_mode
    if runtime.mission_id is not None:
        metadata["mission_id"] = str(runtime.mission_id)
    metadata["proposal"] = proposal.model_dump(mode="json")
    if step is not None:
        metadata["plan_step"] = step.model_dump(mode="json")
    step_task_id = getattr(step, "mission_task_id", None) if step is not None else None
    if step_task_id is None:
        step_task_id = getattr(runtime, "active_task_id", None)
    if step_task_id is not None:
        metadata["mission_task_id"] = str(step_task_id)
    origin_version = getattr(step, "execution_origin_version", None) if step is not None else None
    if origin_version is None and step is not None:
        origin_version = getattr(step, "origin_version", 0)
    try:
        metadata["execution_origin_version"] = int(origin_version or 0)
    except (TypeError, ValueError):
        metadata["execution_origin_version"] = 0
    metadata["model_tool_call_id"] = str(
        getattr(step, "model_tool_call_id", "") if step is not None else ""
    )
    metadata["task_purpose_hash"] = str(
        getattr(step, "task_purpose_hash", "") if step is not None else ""
    )
    candidate_key = getattr(step, "candidate_key", None) if step is not None else None
    if candidate_key:
        metadata["candidate_key"] = str(candidate_key)
    if work_id is not None:
        metadata["work_id"] = str(work_id)
    path_id = getattr(step, "path_id", None)
    if path_id is None:
        path_id = getattr(runtime, "active_path_id", None)
    path_version = getattr(step, "path_version", None)
    if path_version is None:
        path_version = getattr(runtime, "active_path_version", None)
    path_step_id = getattr(step, "path_step_id", None)
    if path_step_id is None:
        path_step_id = getattr(runtime, "active_path_step_id", None)
    principal_ref = getattr(step, "principal_ref", None)
    operator_intent_id = getattr(step, "operator_intent_id", None)
    if operator_intent_id is None:
        operator_intent_id = getattr(runtime, "operator_intent_id", None)
    if path_id is not None:
        metadata["path_id"] = path_id
    if path_version is not None:
        metadata["path_version"] = path_version
    if path_step_id is not None:
        metadata["path_step_id"] = path_step_id
    if principal_ref is not None:
        metadata["principal_ref"] = principal_ref
    if operator_intent_id is not None:
        metadata["operator_intent_id"] = str(operator_intent_id)
    return metadata
