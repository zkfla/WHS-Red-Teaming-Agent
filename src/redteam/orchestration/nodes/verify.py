"""VERIFY — append Evidence, then reach a ``Verdict`` against the
expectation recorded before the action ran. A VIOLATION produces a
verified ``Finding`` carrying the ``EvidenceId`` behind it.
"""

from __future__ import annotations

from redteam.attack_graph.identity import (
    hypothesis_node_id,
    node_id_for_target,
    session_credential_node_id,
    session_principal_node_id,
)
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import HYPOTHESIS_INCONCLUSIVE, HYPOTHESIS_OPEN
from redteam.core.contracts.events import EventType
from redteam.core.convert import tool_result_to_evidence
from redteam.core.enums import AgentPhase, GraphEdgeType, GraphNodeType, Verdict
from redteam.orchestration.analysis import VerificationAssessment, VerificationRequest
from redteam.orchestration.authentication import (
    ACCESS_CONTROL_VIOLATION,
    assess_authentication,
    http_observation,
)
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import (
    AUTHENTICATION_CHECKED,
    HYPOTHESIS_RESOLVED,
    OBSERVATION_RECORDED,
)
from redteam.orchestration.findings import FindingPromoter, PromotionRequest
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.service_status import (
    HYPOTHESIS_STATUS_BY_VERDICT,
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


async def verify_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    step = state.get("plan_step")
    assert proposal is not None and result is not None

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.verify")
    service = graph_service(deps)

    evidence = tool_result_to_evidence(
        result,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        hypothesis_id=proposal.hypothesis_id,
        summary=proposal.expected_observation,
    )
    evidence = evidence.model_copy(
        update={
            "metadata": {
                **evidence.metadata,
                "target_node_id": str(node_id_for_target(proposal.target)),
                "agent_id": str(proposal.agent_id),
                "capability": proposal.capability,
                "operation": proposal.operation,
                "candidate_key": step.candidate_key if step is not None else "",
                "mission_task_id": str(runtime.active_task_id) if runtime.active_task_id else None,
            }
        }
    )
    response_observation = http_observation(proposal.arguments, result)
    if response_observation is not None:
        evidence.metadata["http_observation"] = response_observation
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
    try:
        assessment = await deps.verifier.verify(
            VerificationRequest(
                proposal=proposal,
                result=result,
                expectation=expectation,
                source_refs=[str(evidence.evidence_id), str(result.invocation_id)],
            )
        )
    except Exception as exc:  # noqa: BLE001 - preserve evidence and close work safely
        verification_failed = True
        result = result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "verification_exception": type(exc).__name__,
                }
            }
        )
        assessment = VerificationAssessment(
            verdict=Verdict.UNKNOWN,
            reason="analysis verifier failed before reaching a verdict",
            reviewer_id=f"analysis:error:{type(exc).__name__}",
            source_refs=[str(evidence.evidence_id), str(result.invocation_id)],
        )
    verdict, reason = assessment.verdict, assessment.reason

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
        await service.observe_target(
            proposal.target,
            facts={"methodology_progress": list(progress)},
            provenance=provenance,
            step=runtime.step_count,
        )

    auth_assessment = None
    if step is not None and step.authentication_check is not None:
        auth_assessment = await assess_authentication(
            step.authentication_check,
            proposal,
            result,
            evidence,
            deps.evidence_store,
        )
        is_violation = auth_assessment.outcome == ACCESS_CONTROL_VIOLATION
        comparison = {
            **auth_assessment.model_dump(),
            "session_ref": step.authentication_check.session_ref,
            "path": proposal.arguments.get("path"),
            "expected_access": step.authentication_check.expected_access,
        }
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
        if auth_assessment.outcome in {"session_access_observed", ACCESS_CONTROL_VIOLATION}:
            target_ref = str(
                proposal.arguments.get("target_ref") or node_id_for_target(proposal.target)
            )
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
        if is_violation:
            verdict = Verdict.VIOLATION
            reason = (
                "registered session reached an operator-declared deny path "
                f"(status {auth_assessment.session_status}); access-control "
                "violation confirmed against the anonymous 401 baseline"
            )
        elif (
            step is not None
            and step.assessment_kind == "security_check"
            and step.authentication_check.expected_access == "deny"
            and auth_assessment.outcome in {"session_not_accepted", "access_denied"}
        ):
            verdict = Verdict.PASS
            reason = "the operator-declared deny expectation was enforced"

    observation_only = step is not None and step.assessment_kind == "observation"
    if observation_only:
        verdict = Verdict.UNKNOWN
        reason = "response recorded as an observation; no security claim was verified"
        observed = await publisher.publish(
            OBSERVATION_RECORDED,
            {
                "evidence_id": evidence.evidence_id,
                "action_id": proposal.action_id,
                "target_node_id": str(node_id_for_target(proposal.target)),
                "assessment_kind": "observation",
                "tool_success": result.success,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        last_event_id = observed.event_id

    hypothesis_status = ""
    resolution_reason = ""
    if proposal.hypothesis_id is not None:
        hypothesis_status = HYPOTHESIS_STATUS_BY_VERDICT[verdict]
        if (
            observation_only
            or result.metadata.get("orchestration_exception")
            or verification_failed
        ):
            hypothesis_status = "inconclusive"
        if auth_assessment is not None:
            hypothesis_status = auth_assessment.hypothesis_status
        if hypothesis_status == HYPOTHESIS_OPEN and not _follow_up_possible(step):
            hypothesis_status = HYPOTHESIS_INCONCLUSIVE
            resolution_reason = "no_follow_up_verification_available"
        resolved = await service.resolve_hypothesis(
            str(proposal.hypothesis_id),
            status=hypothesis_status,
            step=runtime.step_count,
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

    existing_claims = {
        str(item.metadata["claim_key"])
        for item in await service.list_findings()
        if item.metadata.get("claim_key")
    }
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
            target_node_id=str(node_id_for_target(proposal.target)),
            capability=proposal.capability,
            operation=proposal.operation,
            agent_id=str(proposal.agent_id),
            action_id=str(proposal.action_id),
            reviewer_id=assessment.reviewer_id,
            source_refs=list(assessment.source_refs),
            severity=severity_for(step),
            assessment_kind=(step.assessment_kind if step is not None else "observation"),
        ),
        existing_claims=existing_claims,
    )
    finding = decision.finding
    if finding is not None:
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
        reason = f"violation was not promoted to a Finding: {decision.refused_reason}"
        refused = await publisher.publish(
            OBSERVATION_RECORDED,
            {
                "evidence_id": evidence.evidence_id,
                "action_id": proposal.action_id,
                "target_node_id": str(node_id_for_target(proposal.target)),
                "assessment_kind": "observation",
                "tool_success": result.success,
                "finding_promotion_refused": decision.refused_reason,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        last_event_id = refused.event_id

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.UPDATE,
                "last_verdict": verdict,
                "consecutive_failures": (
                    runtime.consecutive_failures + 1
                    if verification_failed
                    else runtime.consecutive_failures
                ),
            }
        ),
        last_event_id=last_event_id,
        tool_result=result,
        result_evidence_id=evidence.evidence_id,
    )
