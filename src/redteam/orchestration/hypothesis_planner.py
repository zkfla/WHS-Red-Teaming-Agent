"""One candidate/probe judgment over captured originals and existing central admission."""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding, GraphNode, Hypothesis, HypothesisUpdate
from redteam.core.contracts.mission import MissionPlan, MissionTask, redact_secret_material
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId, new_id
from redteam.orchestration.agents.tool_access import role_readiness
from redteam.orchestration.central_results import RESULT_UPDATED
from redteam.orchestration.central_runtime import (
    event,
    process_controls,
    runtime_token,
    task_is_claimable,
)
from redteam.orchestration.central_state import receipt_failure
from redteam.orchestration.cognition import (
    DecisionFailure,
    DecisionValidationError,
    UnstartedRequestHalt,
)
from redteam.orchestration.coordination import (
    CandidateAdmissionConflict,
    PlanningInputsChanged,
    StateConflict,
    content_hash,
)
from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    diagnostic_for_exception,
    error_type_for,
    exception_chain_for,
    exception_origins_for,
)
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.investigation_context import context_bytes, context_rule_diagnostic
from redteam.orchestration.knowledge_validation import known_principals, known_target_ids
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.planner_context import (
    PlannerContextRequest,
    PlannerPassContext,
    lookup_only,
    planner_lookup_diagnostic,
    project_planner_knowledge,
)
from redteam.orchestration.probe_atomization import (
    HypothesisPlanSelection,
    TaskAtomizationDraft,
    bind_candidate_updates,
    bind_plan_selection,
    build_atomizer_input,
    needs_atomization,
    post_update_candidates,
    resolve_atomized_tasks,
    validate_atomized_tasks,
)
from redteam.orchestration.specialist_tasks import (
    HypothesisProbeProposal,
    ProbeTaskProposal,
    SpecialistProposalError,
    register_probe_tasks,
    validate_probe_subjects,
)

PLANNING_INVALIDATED = "HypothesisPlanningInvalidated"
PROBE_WAKE_EVENTS = frozenset({
    RESULT_UPDATED, EventType.EVIDENCE_VALIDITY_CHANGED, EventType.TASK_COMPLETED,
    EventType.OPERATOR_INTENT_APPLIED,
    PLANNING_INVALIDATED,
})


class HypothesisDecision(BaseModel):
    """New updates use a proposal-local hypothesis_id, mapped to code-issued IDs."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    updates: list[HypothesisUpdate] = Field(default_factory=list)
    tasks: list[ProbeTaskProposal] = Field(default_factory=list)
    reviewed_task_ids: list[str] = Field(default_factory=list)
    reviewed_hypothesis_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    termination: Literal["continue", "replan", "held", "exhausted", "goal_achieved"] = "continue"
    resume_conditions: list[str] = Field(default_factory=list)
    context_request: PlannerContextRequest | None = None


class HypothesisProposalError(ValueError):
    """Safe, code-owned repair reason; never contains a proposed body or secret."""

    def __init__(self, code: str, *, diagnostic: DecisionDiagnostic | None = None):
        self.code = code
        if diagnostic is not None:
            self.diagnostic = diagnostic
        super().__init__(code)


def planner_candidate_wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expose only the candidate transitions owned by the active Planner.

    ``CandidateHypothesis`` retains its domain default for historical readers and other
    callers. Final independent review application remains code-owned, so the Planner wire
    copy has only nonterminal statuses and cannot select a review receipt.
    """
    result = deepcopy(schema)
    candidate = result.get("$defs", {}).get("CandidateHypothesis")
    if not isinstance(candidate, dict):
        raise ValueError("planner_candidate_schema_missing")
    required = list(candidate.get("required", []))
    candidate["required"] = list(dict.fromkeys([*required, "status"]))
    status = candidate.get("properties", {}).get("status", {})
    status.pop("default", None)
    status["description"] = (
        "Planner-owned updates use open, inconclusive, or held. confirmed/rejected values are "
        "read-only canonical outcomes and are rejected from Planner output by admission."
    )
    update = result.get("$defs", {}).get("HypothesisUpdate")
    if not isinstance(update, dict):
        raise ValueError("planner_candidate_update_schema_missing")
    review = update.get("properties", {}).get("review_evidence_id")
    if not isinstance(review, dict):
        raise ValueError("planner_candidate_review_schema_missing")
    review["description"] = (
        "Planner must leave this null. Central review application exclusively consumes final "
        "review receipts and applies terminal candidate state; admission rejects selection here."
    )
    return result


def validate_candidate_status_presence(raw: Any) -> None:
    """Reject omitted candidate statuses before Pydantic can default them to ``open``."""
    if not isinstance(raw, dict) or not isinstance(raw.get("updates"), list):
        return
    for index, update in enumerate(raw["updates"]):
        if not isinstance(update, dict):
            continue
        hypothesis = update.get("hypothesis")
        if not isinstance(hypothesis, dict) or "status" in hypothesis:
            continue
        code = "candidate_status_required"
        location = f"$.updates[{index}].hypothesis.status"
        raise HypothesisProposalError(code, diagnostic=DecisionDiagnostic(
            code=code, stage="hypothesis_planning", kind="validation",
            location=location, retryable=True, cause_scope="model_input",
            owner="orchestration.hypothesis_planner",
            summary="Every candidate update must state its status explicitly. Candidate prose "
                    "does not supply a missing status.",
            repair_condition="Include open, inconclusive, or held. Terminal confirmed/rejected "
                             "state is applied by the central final-review path and must be "
                             "consumed from canonical state.",
            validation_issues=[{"rule": code, "location": location}],
        ))


def validate_planner_candidate_authority(raw: Any) -> None:
    """Keep final-review receipt selection and terminal transitions out of Planner output."""
    if not isinstance(raw, dict) or not isinstance(raw.get("updates"), list):
        return
    for index, update in enumerate(raw["updates"]):
        if not isinstance(update, dict):
            continue
        if update.get("review_evidence_id") is not None:
            location = f"$.updates[{index}].review_evidence_id"
            raise HypothesisProposalError(
                "planner_review_application_owned_by_code",
                diagnostic=DecisionDiagnostic(
                    code="planner_review_application_owned_by_code",
                    stage="hypothesis_planning",
                    kind="validation",
                    location=location,
                    retryable=True,
                    cause_scope="model_input",
                    owner="orchestration.candidate_review_application",
                    summary=(
                        "The Planner cannot select or reapply a final review receipt. Canonical "
                        "review application state already owns that transition."
                    ),
                    repair_condition=(
                        "Omit review_evidence_id and consume candidate_review_state. If the "
                        "application is pending, leave the candidate nonterminal; if it is "
                        "applied, keep the terminal candidate unchanged and propose only a "
                        "separate bounded follow-up question or completion judgment."
                    ),
                    validation_issues=[{
                        "rule": "planner_review_application_owned_by_code",
                        "location": location,
                    }],
                ),
            )
        hypothesis = update.get("hypothesis")
        if not isinstance(hypothesis, dict) or hypothesis.get("status") not in {
            "confirmed", "rejected"
        }:
            continue
        location = f"$.updates[{index}].hypothesis.status"
        raise HypothesisProposalError(
            "planner_terminal_candidate_owned_by_review_application",
            diagnostic=DecisionDiagnostic(
                code="planner_terminal_candidate_owned_by_review_application",
                stage="hypothesis_planning",
                kind="validation",
                location=location,
                retryable=True,
                cause_scope="model_input",
                owner="orchestration.candidate_review_application",
                summary=(
                    "The Planner cannot confirm or reject a candidate. Only the central "
                    "application of a bound final review receipt owns terminal state."
                ),
                repair_condition=(
                    "Use the canonical candidate and candidate_review_state as already applied. "
                    "For an open candidate with a pending application, wait for its retry path. "
                    "Otherwise propose only nonterminal premise/scope updates, a separate "
                    "follow-up question, or a completion judgment."
                ),
                validation_issues=[{
                    "rule": "planner_terminal_candidate_owned_by_review_application",
                    "location": location,
                }],
            ),
        )


def validate_candidate_review_selections(
    updates: Sequence[HypothesisUpdate], records: Mapping[str, EvidenceRecord],
) -> None:
    """Compatibility guard for direct/older callers; active Planners cannot select receipts."""
    for index, update in enumerate(updates):
        if update.hypothesis.status not in {"confirmed", "rejected"}:
            if update.review_evidence_id is not None:
                code = "candidate_review_receipt_requires_terminal_status"
                location = f"$.updates[{index}].review_evidence_id"
                raise HypothesisProposalError(code, diagnostic=DecisionDiagnostic(
                    code=code, stage="hypothesis_planning", kind="validation",
                    location=location, retryable=True, cause_scope="model_input",
                    owner="orchestration.hypothesis_planner",
                    summary="A review_evidence_id is consumed only with a confirmed or rejected "
                            "candidate update; it cannot change a nonterminal candidate.",
                    repair_condition="If the candidate remains open, held, or inconclusive, omit "
                                     "review_evidence_id. Propose confirmed or rejected only "
                                     "when preserved originals support that verdict and the "
                                     "receipt matches the current candidate revision and purpose.",
                    validation_issues=[{"rule": code, "location": location}],
                ))
            continue
        failure = receipt_failure(
            update, update.hypothesis, records.get(update.review_evidence_id or ""),
        )
        if failure is None:
            continue
        code = "candidate_review_receipt_invalid"
        raise HypothesisProposalError(code, diagnostic=DecisionDiagnostic(
            code=code, stage="hypothesis_planning", kind="validation",
            location=f"$.updates[{index}].review_evidence_id", retryable=True,
            cause_scope="model_input", owner="orchestration.hypothesis_planner",
            summary="Select a final public independent review receipt matching the candidate, "
                    "revision, purpose and outcome. A Task completion receipt is not a "
                    "candidate verdict review.",
            repair_condition="Use a matching captured review_receipt, or keep the candidate "
                             "open and request independent review of retained originals.",
            trace=[{"receipt_failure": failure}],
        ))


# Code-owned repair guidance keyed by proposal error code: (contract field, guidance).
# Fields name the judgment contract only; they never carry model-supplied refs or values.
_REPAIR_GUIDANCE: dict[str, tuple[str, str]] = {
    "continue_requires_proposed_or_existing_work": (
        "$.termination",
        "Continue is valid only with proposed tasks/questions or referenced existing work "
        "that is still claimable/running, ready, unsuppressed and current in candidate "
        "purpose; candidate updates alone are not progress. Use continue only with that path; "
        "otherwise use replan, held for a prerequisite, or exhausted. "
        "goal_achieved only proposes the separate completion gate.",
    ),
    "new_work_requires_continue_judgment": (
        "$.termination",
        "Proposed questions or tasks require termination continue. Use held, exhausted, "
        "replan or goal_achieved only for a judgment that proposes neither.",
    ),
    "empty_judgment_needs_termination_and_resume_conditions": (
        "$.resume_conditions",
        "An empty judgment with termination held or exhausted needs at least one nonblank "
        "resume_condition naming what would change the decision. Continue, replan and "
        "goal_achieved do not need one.",
    ),
    "judgment_reason_or_resume_condition_blank": (
        "$",
        "The reason and every resume_condition must be nonblank text.",
    ),
}


_LEGACY_SUBJECT_SUMMARY = (
    "Each task's target_node_ids and principal_refs must lie inside every candidate named in its "
    "hypothesis_revisions, after this proposal's updates. Narrow the task to those subjects, "
    "split it, or propose a justified candidate update for them. Do not invent or unilaterally "
    "broaden candidate subjects. Correct the location named in the diagnostic."
)
_SUBJECT_FIELDS = {"target": "target_node_ids", "principal": "principal_refs"}


def _legacy_subject_rejection(kind: str, *, index: int) -> HypothesisProposalError:
    """Attached indexed diagnostic for a task subject outside its post-update candidates."""
    code = f"probe_{kind}_outside_candidate"
    location = f"$.tasks[{index}].{_SUBJECT_FIELDS[kind]}"
    return HypothesisProposalError(code, diagnostic=DecisionDiagnostic(
        code=code, stage="hypothesis_planning", kind="validation", retryable=True,
        cause_scope="model_input", owner="orchestration.hypothesis_planner",
        location=location, summary=_LEGACY_SUBJECT_SUMMARY,
        validation_issues=[{"rule": code, "location": location}],
    ))


def _legacy_view_missing(index: int) -> HypothesisProposalError:
    """Fail-closed invariant: proposal views and versions must describe the same candidates."""
    return HypothesisProposalError("candidate_subject_view_missing", diagnostic=DecisionDiagnostic(
        code="candidate_subject_view_missing", stage="hypothesis_planning", kind="internal",
        retryable=False, cause_scope="runtime_contract", owner="orchestration.hypothesis_planner",
        location=f"$.tasks[{index}].hypothesis_revisions",
        summary="A named candidate has no subject view in this proposal. The planner state is "
                "inconsistent; no change was committed.",
    ))


def _proposal_repair_summary(exc: BaseException, fallback: str) -> str:
    """Shared single-stage and split repair text; unmapped codes keep the caller's fallback."""
    code = getattr(exc, "code", None)
    rule = _REPAIR_GUIDANCE.get(code) if isinstance(code, str) else None
    return rule[1] if rule is not None else fallback


def _judgment_controls(planner, roles) -> dict:
    """Planning-time controls stored with the judgment, so its commit enforces them."""
    return {
        "replan_max_passes": planner.deps.settings.planner_replan_max_passes,
        "witness_inputs": {
            "eligible_roles": sorted(roles),
            "require_probe_limits": planner.policy.probe_limits is not None,
        },
    }


def _existing_work_witnesses(
    reviewed_task_ids, snapshot, *, eligible_roles, require_probe_limits, candidate_revisions,
    allow_running=True,
):
    """Select only referenced work that still has a canonical execution path."""
    return {
        identity for identity in reviewed_task_ids
        if identity in snapshot.tasks and task_is_claimable(
            snapshot.tasks[identity],
            eligible_roles=eligible_roles,
            mission=snapshot.mission,
            require_probe_limits=require_probe_limits,
            candidate_revisions=candidate_revisions,
            allow_running=allow_running,
        )
    }


_SECONDARY_KEY = "related_admission_diagnostics"


def _generic_admission_rejection() -> DecisionDiagnostic:
    return DecisionDiagnostic(
        code="hypothesis_admission_rejected", stage="hypothesis_planning", kind="validation",
        summary="Final admission rejected without an owned field diagnostic. Safe exception "
                "type and spent budget are recorded; original message withheld. No changes "
                "committed.",
        repair_condition="Check the original evidence, verdict and scope records for this pass.",
        retryable=False,
    )


def _admission_rejection_diagnostic(exc: BaseException, budget) -> DecisionDiagnostic:
    """Admission rejection carrying the chain's spent counters and a safe exception type/chain.

    A typed diagnostic attached by the rejecting owner is kept, moved to this planning stage and
    never made retryable here, so the model repair budget cannot bypass current controls. The
    exception message is never copied because StateConflict and ValueError can carry model values.
    Counters are read from the budget only; nothing is charged here.
    """
    attached = getattr(exc, "diagnostic", None)
    base = (
        attached.model_copy(update={"stage": "hypothesis_planning", "retryable": False})
        if isinstance(attached, DecisionDiagnostic) else _generic_admission_rejection()
    )
    spent = budget.diagnostic_details()
    update: dict[str, Any] = {
        "attempts": budget.used,
        "attempt_limit": spent["attempt_limit"],
        "stage_attempts": spent["stage_attempts"],
        "normal_limit": spent["normal_limit"],
        "repair_limit": spent["repair_limit"],
        "transport_limit": spent["transport_limit"],
        "normal_used": spent["normal_used"],
        "repair_used": spent["repair_used"],
        "transport_used": spent["transport_used"],
    }
    # Prefer owner trace over budget trace; retain newest rows within existing bound before
    # appending safe origin.
    trace = list(base.trace or spent["trace"])
    origin_row = {"outcome": "admission_exception_origin", "origins": exception_origins_for(exc)}
    update["trace"] = [*(trace[-19:] if len(trace) >= 20 else trace), origin_row]
    if not base.failure_counts:
        update["failure_counts"] = spent["failure_counts"]
    if not base.error_type:
        update["error_type"] = error_type_for(exc)
    if not base.exception_chain:
        update["exception_chain"] = exception_chain_for(exc)
    return base.model_copy(update=update)


def _related_projection(item: DecisionDiagnostic) -> dict:
    return {
        "code": item.code, "owner": item.owner, "cause_scope": item.cause_scope,
        "criterion_ids": list(item.criterion_ids[:10]),
        "evidence_ids": list(item.evidence_ids[:10]),
        "repair_condition": item.repair_condition,
    }


def _candidate_admission_diagnostic(exc: CandidateAdmissionConflict, budget) -> DecisionDiagnostic:
    """Primary owner diagnostic with planner stage/attempts and one bounded secondary trace row."""
    if not exc.diagnostics:
        return _admission_rejection_diagnostic(exc, budget)
    primary, *related = exc.diagnostics
    diagnostic = primary.model_copy(
        update={"stage": "hypothesis_planning", "attempts": budget.used},
    )
    if not related:
        return diagnostic
    projections = [_related_projection(item) for item in related[:7]]
    trace = list(diagnostic.trace)
    if len(trace) < 20:
        trace.append({_SECONDARY_KEY: projections})
    elif _SECONDARY_KEY not in trace[-1]:
        trace[-1] = {**trace[-1], _SECONDARY_KEY: projections}
    return diagnostic.model_copy(update={"trace": trace})


def _proposal_validation_diagnostic(
    exc: BaseException, *, stage: str, summary: str, schema: dict[str, Any],
) -> DecisionDiagnostic:
    """Merge diagnostic_for_exception's safe details with code-owned repair code/summary.

    diagnostic_for_exception already derives validation_issues/error_type/exception_chain
    safely (pydantic .errors() loc/type, never raw input/messages). We only need to
    override `code` with the proposal error's own `.code` when present (preserving
    HypothesisProposalError/SpecialistProposalError/AtomizationProposalError semantics)
    and keep the existing human-readable repair `summary` instead of the generic one.
    """
    base = diagnostic_for_exception(exc, stage=stage, schema=schema)
    lookup_diagnostic = planner_lookup_diagnostic(exc, stage=stage)
    if lookup_diagnostic is not None:
        return lookup_diagnostic
    context_diagnostic = context_rule_diagnostic(exc, stage=stage, schema=schema)
    if context_diagnostic is not None:
        return context_diagnostic
    detail = getattr(exc, "diagnostic", None)
    if isinstance(detail, DecisionDiagnostic):
        return detail.model_copy(update={
            "stage": stage, "error_type": base.error_type or error_type_for(exc),
            "exception_chain": base.exception_chain or exception_chain_for(exc),
        })
    code = getattr(exc, "code", None) or "hypothesis_proposal_invalid"
    update = {"code": code, "summary": summary, "kind": "validation", "retryable": True}
    rule = _REPAIR_GUIDANCE.get(code)
    if rule is not None:
        update["location"] = rule[0]
        update["validation_issues"] = [{"rule": code, "location": rule[0]}]
    return base.model_copy(update=update)


@dataclass
class PlanningSnapshot:
    mission: MissionPlan
    tasks: dict[str, MissionTask]
    nodes: dict[str, GraphNode]
    candidates: dict[str, Hypothesis]
    binding: str
    sequence: int
    consumed: int | None
    executions: tuple = ()
    edges: tuple = ()
    graph_roots: tuple[str, ...] = ()
    graph_truncated: bool = False
    findings: tuple[Finding, ...] = ()
    review_applications: tuple[dict[str, Any], ...] = ()
    finding_promotions: tuple[dict[str, Any], ...] = ()


def capture_snapshot(tx):
    mission = tx.admit()
    nodes = {}
    for node in tx.graph.list_nodes():
        identity = str(node.id).removeprefix(tx.engagement_id + "\x1f")
        nodes[identity] = node.model_copy(update={"id": GraphNodeId(identity)})
    candidates = {facts_of(node.properties)["hypothesis_id"]:
                  Hypothesis.model_validate(facts_of(node.properties))
                  for node in nodes.values() if node.type is GraphNodeType.HYPOTHESIS}
    findings = []
    for node in nodes.values():
        if node.type is not GraphNodeType.FINDING:
            continue
        try:
            findings.append(Finding.model_validate(facts_of(node.properties)))
        except ValueError:
            continue
    graph_subjects = sorted(identity for identity, node in nodes.items()
                            if node.type is not GraphNodeType.HYPOTHESIS)
    roots = tuple(graph_subjects[:16])
    _, edges, truncated = tx.graph.context_neighborhood(
        roots, hops=1, max_nodes=32, max_edges=64,
    )
    return PlanningSnapshot(
        mission, {str(task.task_id): task for task in tx.list_tasks()}, nodes, candidates,
        tx.binding()[1], tx.latest_event_sequence(tuple(PROBE_WAKE_EVENTS)),
        tx.hypothesis_planner_cursor(), tuple(tx.list_executions()), tuple(edges), roots,
        truncated or len(graph_subjects) > len(roots), tuple(findings),
        tuple(tx.candidate_review_applications()), tuple(tx.candidate_finding_promotions()),
    )


def bind_decision(
    decision, snapshot, *, issued, targets, principals, roles, max_tasks, limits,
):
    """Resolve local references and validate purpose inputs without mutating originals."""
    tasks = []
    names = [update.hypothesis.hypothesis_id for update in decision.updates]
    if not decision.reason.strip() or any(
        not value.strip() for value in decision.resume_conditions
    ):
        raise HypothesisProposalError("judgment_reason_or_resume_condition_blank")
    if decision.tasks and decision.termination != "continue":
        raise HypothesisProposalError("new_work_requires_continue_judgment")
    if len(set(names)) != len(names) or len(decision.tasks) > max_tasks:
        raise HypothesisProposalError("candidate_identity_or_task_count_invalid")
    updates = bind_candidate_updates(
        decision.updates, snapshot, issued=issued, targets=targets,
        principals=principals, resume_conditions=decision.resume_conditions,
        error=HypothesisProposalError,
    )
    views = post_update_candidates(snapshot, updates)
    versions = {identity: candidate.revision for identity, candidate in snapshot.candidates.items()}
    versions.update({update.hypothesis.hypothesis_id: (update.expected_revision or 0) + 1
                     for update in updates})
    for index, draft in enumerate(decision.tasks):
        if draft.role_id not in roles:
            raise HypothesisProposalError("probe_role_not_ready")
        revisions = {issued.get(identity, identity): revision
                     for identity, revision in draft.hypothesis_revisions.items()}
        if any(versions.get(identity) != revision for identity, revision in revisions.items()):
            raise HypothesisProposalError("probe_candidate_revision_not_in_proposal")
        for identity in revisions:
            if identity not in views:
                raise _legacy_view_missing(index)
            mismatches = views[identity].probe_subject_mismatches(
                draft.target_node_ids, draft.principal_refs,
            )
            if mismatches:
                raise _legacy_subject_rejection(mismatches[0], index=index)
        if bool(draft.source_task_id) != bool(draft.source_result_id) or (
            draft.source_question_id and not draft.source_task_id
        ):
            raise HypothesisProposalError("probe_followup_requires_original_task_result_pair")
        required = {issued.get(ref, ref) for ref in draft.goal_required_hypothesis_ids}
        if required - revisions.keys() or required and not draft.required:
            raise HypothesisProposalError("goal_required_candidates_need_related_required_probe")
        if any(condition.hypothesis_id and
               issued.get(condition.hypothesis_id, condition.hypothesis_id) not in revisions
               for condition in draft.completion_criteria):
            raise HypothesisProposalError("criterion_candidate_outside_probe_purpose")
        if draft.source_task_id:
            source = snapshot.tasks.get(draft.source_task_id)
            if source is None or source.result is None or (
                source.result.result_id != draft.source_result_id
            ):
                raise HypothesisProposalError("probe_source_result_not_in_snapshot")
        if draft.probe_limits is not None and (
            draft.probe_limits.max_actions > limits.max_actions
            or draft.probe_limits.max_model_requests > limits.max_model_requests
            or (
                draft.probe_limits.max_repair_requests is not None
                and limits.max_repair_requests is not None
                and draft.probe_limits.max_repair_requests > limits.max_repair_requests
            )
            or (
                draft.probe_limits.max_consecutive_provider_failures is not None
                and limits.max_consecutive_provider_failures is not None
                and draft.probe_limits.max_consecutive_provider_failures
                > limits.max_consecutive_provider_failures
            )
        ):
            raise HypothesisProposalError("probe_limits_exceed_profile")
        tasks.append(draft.model_copy(update={
            "hypothesis_revisions": revisions,
            "goal_required_hypothesis_ids": [issued.get(ref, ref)
                                              for ref in draft.goal_required_hypothesis_ids],
            "completion_criteria": [condition.model_copy(update={
                "hypothesis_id": issued.get(condition.hypothesis_id, condition.hypothesis_id),
            }) for condition in draft.completion_criteria],
        }))
    if set(decision.reviewed_task_ids) - snapshot.tasks.keys() or (
        set(decision.reviewed_hypothesis_ids) - snapshot.candidates.keys()
    ):
        raise HypothesisProposalError("reviewed_reference_not_in_snapshot")
    witnesses = _existing_work_witnesses(
        decision.reviewed_task_ids, snapshot, eligible_roles=roles,
        require_probe_limits=limits is not None, candidate_revisions=versions,
    )
    if decision.termination == "continue" and not tasks and not witnesses:
        raise HypothesisProposalError("continue_requires_proposed_or_existing_work")
    if not (updates or tasks):
        if decision.termination not in {"continue", "replan", "goal_achieved"} and (
            not decision.resume_conditions
        ):
            raise HypothesisProposalError("empty_judgment_needs_termination_and_resume_conditions")
        return None
    proposal = HypothesisProbeProposal(updates=updates, tasks=tasks)
    validate_probe_subjects(proposal, known_targets=targets, known_principals=principals,
                            known_hypotheses=versions.keys())
    return proposal


def related_reads(decision, proposal, snapshot, records, *, questions=(), issued=None,
                  eligible_roles=(), require_probe_limits=False):
    hypotheses = set(decision.reviewed_hypothesis_ids)
    tasks = set(decision.reviewed_task_ids)
    subjects = set()
    evidence = set()
    new = ({update.hypothesis.hypothesis_id for update in proposal.updates
            if update.expected_revision is None} if proposal is not None else set())
    if proposal is not None:
        for update in proposal.updates:
            candidate = update.hypothesis
            if update.expected_revision is not None:
                hypotheses.add(candidate.hypothesis_id)
            hypotheses.update(candidate.source_hypothesis_ids)
            subjects.update(candidate.target_node_ids)
            evidence.update([*candidate.supporting_evidence_ids, *candidate.refuting_evidence_ids])
            if update.review_evidence_id:
                evidence.add(update.review_evidence_id)
        for task in proposal.tasks:
            hypotheses.update(set(task.hypothesis_revisions) - new)
            subjects.update(task.target_node_ids)
            evidence.update(task.evidence_ids)
            if task.source_task_id:
                tasks.add(task.source_task_id)
    for question in questions:
        revisions = ({issued.get(identity, identity): revision
                      for identity, revision in question.hypothesis_revisions.items()}
                     if issued is not None else dict(question.hypothesis_revisions))
        hypotheses.update(set(revisions) - new)
        subjects.update(question.target_node_ids)
        evidence.update(question.evidence_ids)
        if question.source_task_id:
            tasks.add(question.source_task_id)
    if decision.termination not in {"continue", "replan"}:
        # A global stop/exhaustion judgment consumed all current work, not one candidate.
        hypotheses.update(snapshot.candidates)
        tasks.update(snapshot.tasks)
    elif decision.termination == "continue" and not (
        (proposal is not None and proposal.tasks) or questions
    ):
        # A claimable task's candidate revisions are part of the witness contract.
        # Running work retains its captured purpose, so only pending/ready witnesses
        # consume candidate graph inputs here.
        candidate_revisions = {
            identity: candidate.revision
            for identity, candidate in snapshot.candidates.items()
        }
        if proposal is not None:
            candidate_revisions.update({
                update.hypothesis.hypothesis_id: (update.expected_revision or 0) + 1
                for update in proposal.updates
            })
        claimable_witness_ids = _existing_work_witnesses(
            decision.reviewed_task_ids, snapshot, eligible_roles=eligible_roles,
            require_probe_limits=require_probe_limits,
            candidate_revisions=candidate_revisions, allow_running=False,
        )
        for identity in claimable_witness_ids:
            hypotheses.update(set(snapshot.tasks[identity].hypothesis_revisions) - new)
    for record in records:
        if str(record.evidence_id) in evidence:
            identity = record.metadata.get("mission_task_id")
            if identity:
                if identity not in snapshot.tasks:
                    raise HypothesisProposalError("source_observation_arrived_after_snapshot")
                tasks.add(identity)
    for identity in tasks:
        subjects.update(snapshot.tasks[identity].related_attack_graph_node_ids)
    for identity in hypotheses:
        subjects.add("hypothesis:" + identity)
        subjects.update(snapshot.candidates[identity].target_node_ids)
    return ({str(ref): content_hash(snapshot.nodes.get(str(ref))) for ref in subjects},
            {identity: content_hash(snapshot.tasks[identity]) for identity in tasks})


class _PlanningPassInvalidated(UnstartedRequestHalt):
    """Only the coordination owner's allow-listed cause crosses the request boundary."""

    def __init__(self, changed: PlanningInputsChanged):
        self.cause_code = changed.code
        super().__init__("related_planning_inputs_changed")


class _PlanningRequestGuard:
    """Fence known related reads before a request; the final CAS remains authoritative."""

    def __init__(self, state, snapshot, pass_context, engine):
        self.state, self.snapshot = state, snapshot
        self.context, self.engine = pass_context, engine
        self.graph_read: dict[str, str] = {}
        self.task_read: dict[str, str] = {}

    def include(self, graph_read, task_read):
        self.graph_read.update(graph_read)
        self.task_read.update(task_read)

    async def __call__(self):
        await self.engine.guard_reasoning()
        graph_read, task_read = self.context.read_dependencies()
        graph_read.update(self.graph_read)
        task_read.update(self.task_read)

        def check(tx):
            tx.check_planning_inputs(
                self.snapshot.mission.revision, self.snapshot.binding, graph_read, task_read,
            )
            # Only captured explicit edge/execution reads; never the whole inventory.
            tx.check_planner_inventory({
                "termination": "continue", **self.context.auxiliary_reads(),
            })

        try:
            await self.state.transaction(check, write=False)
        except PlanningInputsChanged as changed:
            raise _PlanningPassInvalidated(changed) from changed


async def _queue_planning_invalidation(deps, identity, cause_code):
    state = deps.central_state
    await state.transaction(lambda tx: tx.queue(event(
        state, PLANNING_INVALIDATED,
        {"pass_id": identity, "reason_code": "related_planning_inputs_changed",
         "cause_code": cause_code},
    )))


async def advance_hypotheses(planner, runtime, schema):
    try:
        return await _advance_hypotheses(planner, runtime, schema)
    except _PlanningPassInvalidated as changed:
        await _queue_planning_invalidation(planner.deps, new_id("planner_pass"), changed.cause_code)
        await planner.deps.event_bus.flush()
        return []


async def _advance_hypotheses(planner, runtime, schema):
    deps, state = planner.deps, planner.deps.central_state
    await process_controls(deps)
    await runtime_token(deps, runtime, require_task=False)
    runtime = runtime.model_copy(deep=True, update={"active_task_id": None, "current_agent": None})
    snapshot = await state.transaction(capture_snapshot, write=False)
    if snapshot.consumed is not None and snapshot.sequence <= snapshot.consumed:
        return []  # Exact event re-delivery/reopen, never semantic probe deduplication.
    engine = InvestigationEngine(deps)
    budget = engine.begin_native_turn(runtime)
    context = await engine.context(runtime, None)
    material = await engine.execution_material(runtime)
    targets = known_target_ids(material) & snapshot.nodes.keys()
    principals = known_principals(material)
    readiness = await role_readiness(deps)
    roles = {item["role_id"] for item in readiness if item["activation_eligible"]}
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    pass_context = PlannerPassContext(
        snapshot, records, context, run_id=runtime.run_id, engagement_id=runtime.engagement_id,
        evidence_store=deps.evidence_store, graph=AttackGraphService(deps.graph_store),
    )
    fields = pass_context.base_fields()
    request_guard = _PlanningRequestGuard(state, snapshot, pass_context, engine)
    payload = redact_secret_material({
        "mission": snapshot.mission.model_dump(mode="json"),
        **fields,
        "knowledge": project_planner_knowledge(context["knowledge"]),
        "constraints": context["engagement_constraints"],
        "operator_instructions": context["operator_instructions"],
        "role_readiness": readiness, "tools_by_role": context["tools_by_role"],
        "tool_cards": context["tool_cards"], "known_target_ids": sorted(targets)[:16],
        "known_target_count": len(targets),
        "program_execution_environment": context["program_execution_environment"],
        "program_manifest_status": context["program_manifest_status"],
        "workroom_status": context["workroom_status"],
        "tool_cards_total": context["tool_cards_total"],
        "tool_cards_truncated": context["tool_cards_truncated"],
        "tool_cards_guidance": context["tool_cards_guidance"],
        "registered_principal_refs": sorted(principals)[:16],
        "registered_principal_count": len(principals),
        "remaining_budget": context["remaining_budget"],
        "probe_limits": planner.policy.probe_limits.model_dump(),
        "max_tasks": planner.policy.max_tasks_per_strategy,
        "input_sequence": snapshot.sequence,
    })
    issued = {}
    if getattr(planner.policy, "probe_atomization", False) is True:
        return await _advance_split(
            planner, runtime, schema, snapshot, engine, budget, payload,
            targets, principals, roles, records, pass_context, request_guard,
        )

    def bind(raw):
        validate_candidate_status_presence(raw)
        validate_planner_candidate_authority(raw)
        decision = HypothesisDecision.model_validate(raw)
        if lookup_only(decision):
            return decision, None, ({}, {})
        pass_context.validate_reviewed(decision)
        proposal = bind_decision(
            decision, snapshot, issued=issued, targets=targets, principals=principals,
            roles=roles, max_tasks=planner.policy.max_tasks_per_strategy,
            limits=planner.policy.probe_limits,
        )
        return decision, proposal, related_reads(
            decision, proposal, snapshot, records, eligible_roles=roles,
            require_probe_limits=planner.policy.probe_limits is not None,
        )

    def validate(raw):
        try:
            bind(raw)
        except (ValueError, SpecialistProposalError) as exc:
            raise DecisionValidationError(_proposal_validation_diagnostic(
                exc, stage="hypothesis_planning",
                summary=_proposal_repair_summary(exc, (
                    "Use captured subjects, ready roles, candidate revisions and original "
                    "source results. A new candidate key is local to this proposal. Continue "
                    "requires proposed tasks or a referenced executable existing task; candidate "
                    "updates alone are not progress. Use replan for a candidate/empty follow-up, "
                    "held for an external prerequisite, exhausted when no useful work remains, "
                    "or goal_achieved only as a completion proposal."
                )),
                schema=planner_candidate_wire_schema(HypothesisDecision.model_json_schema()),
            )) from exc

    with llm_request_scope(str(runtime.run_id), "hypothesis_planning"):
        raw = await _ask_with_context(planner, pass_context, engine,
            stage="hypothesis_planning", payload=payload, budget=budget,
            response_schema=planner_candidate_wire_schema(
                schema(HypothesisDecision.model_json_schema())
            ),
            validate_response=validate, before_call=request_guard,
            system=_SINGLE_PLAN_SYSTEM,
        )
    decision, proposal, (graph_read, task_read) = bind(raw)
    lookup_graph, lookup_tasks = pass_context.read_dependencies()
    graph_read.update(lookup_graph)
    task_read.update(lookup_tasks)
    identity = new_id("planner_pass")
    judgment = {**decision.model_dump(mode="json"), "sequence": snapshot.sequence,
                "issued_candidate_ids": issued, "graph_read": graph_read, "task_read": task_read,
                "candidate_inventory": sorted(snapshot.candidates),
                "task_inventory": sorted(snapshot.tasks), **pass_context.auxiliary_reads(),
                **_judgment_controls(planner, roles)}
    return await _finalize_hypothesis_pass(
        deps, snapshot, budget, identity=identity, proposal=proposal, judgment=judgment,
        graph_read=graph_read, task_read=task_read, targets=targets, principals=principals,
        max_tasks=planner.policy.max_tasks_per_strategy,
    )


async def _finalize_hypothesis_pass(deps, snapshot, budget, *, identity, proposal, judgment,
                                    graph_read, task_read, targets, principals, max_tasks):
    """One commit path for legacy and split judgments: single CAS or judgment-only write."""
    state = deps.central_state
    try:
        if proposal is not None:
            receipt = await register_probe_tasks(
                deps, proposal_id=identity, proposal=proposal,
                expected_revision=snapshot.mission.revision, expected_binding_hash=snapshot.binding,
                graph_read=graph_read, task_read=task_read, known_targets=targets,
                known_principals=principals, max_tasks=max_tasks,
                planner_judgment=judgment,
            )
            identities = receipt["task_ids"]
        else:
            def commit(tx):
                tx.check_planning_inputs(snapshot.mission.revision, snapshot.binding,
                                         graph_read, task_read)
                tx.check_planner_inventory(judgment)
                tx.finish_hypothesis_judgment(identity, judgment)
            await state.transaction(commit)
            identities = []
    except PlanningInputsChanged as exc:
        await _queue_planning_invalidation(deps, identity, exc.code)
        identities = []
    except CandidateAdmissionConflict as exc:
        raise DecisionFailure("hypothesis_planning", "format", budget.used,
                              diagnostic=_candidate_admission_diagnostic(exc, budget)) from exc
    except (StateConflict, ValueError) as exc:
        raise DecisionFailure("hypothesis_planning", "format", budget.used,
                              diagnostic=_admission_rejection_diagnostic(exc, budget)) from exc
    await deps.event_bus.flush()
    return identities


PLANNER_REQUEST_CONTRACT_VERSION = 9


FEEDBACK_SYSTEM = (
    f"이 호출은 Planner request contract v{PLANNER_REQUEST_CONTRACT_VERSION}을 사용합니다. "
    "user payload는 범위가 제한된 현재 snapshot DATA이며 응답은 제공된 JSON schema를 "
    "정확히 따릅니다. "
    "필요한 기록이 생략된 경우에만 context_request를 사용합니다. 조회 전용 응답은 "
    "termination=continue, 빈 updates/questions/tasks/reviewed refs를 사용하고 상태 변경을 "
    "함께 제안하지 않습니다. 조회 결과가 없다는 사실은 그 query 범위 밖의 부재를 "
    "증명하지 않습니다. "
    "candidate contract는 v2입니다. Planner update는 open, inconclusive, held만 사용합니다. "
    "새 candidate는 proposal-local hypothesis_id와 expected_revision=null을, 기존 candidate는 "
    "captured ID와 revision을 사용합니다. final review receipt 적용과 confirmed/rejected "
    "transition은 중앙 코드가 소유하므로 review_evidence_id를 선택하거나 terminal update를 "
    "반환하지 않습니다. candidate_review_state의 canonical 적용 결과를 소비합니다. "
    "continue에는 새 question/task 또는 실제 claimable/running work가 필요합니다. held와 "
    "exhausted에는 정확한 resume_conditions가 필요합니다. replan은 제한된 추가 판단을 "
    "예약하며 goal_achieved는 별도 완료 검토를 위한 제안입니다. "
)

_SINGLE_PLAN_SYSTEM = FEEDBACK_SYSTEM + (
    "이 호출은 candidate update와 Task를 한 응답으로 반환합니다. 새 update는 proposal-local "
    "hypothesis_id와 expected_revision=null을 사용하고, 이를 참조하는 Task는 revision=1을 "
    "사용합니다. 기존 update는 captured ID/revision을, Task는 resulting revision을 사용합니다. "
    "source Task/result "
    "결속과 실제 reviewed_task_ids/reviewed_hypothesis_ids를 유지합니다."
)


_SPLIT_PLAN_SYSTEM = FEEDBACK_SYSTEM + (
    "candidate updates와 조사 questions만 제안하고 Worker Task는 만들지 않습니다. "
    "question은 objective, admitted subject refs, candidate/source/evidence refs와 completion "
    "criteria를 가집니다. candidate_refs에는 captured ID 또는 이번 proposal의 local key만 "
    "쓰고 revision은 쓰지 않습니다. criterion principal_refs는 question principal_refs의 "
    "부분집합입니다. source_task_id가 있으면 captured Task를 사용하고 result ID는 코드가 "
    "복원합니다. 모든 응답에 questions와 termination을 명시합니다. "
)

_SPLIT_ATOMIZE_SYSTEM = (
    "각 Task는 제공된 question_id와 completion_criterion_ids를 사용합니다. criterion 본문, "
    "candidate revision, required/source binding은 코드가 원본 question에서 복원합니다. "
    "전체 Task 집합은 각 question의 subjects와 criteria를 정확히 덮어야 하며 paired "
    "comparison criterion은 나누지 않습니다. 제공된 role과 subject만 사용합니다."
)

_SPLIT_PLAN_REQUIRED = ("questions", "termination")
_CANDIDATE_REFS_DESCRIPTION = (
    "이 question이 다루는 captured candidate ID 또는 이번 updates의 local hypothesis_id를 "
    "지정합니다. candidate와 무관하면 []를 사용하며 revision은 코드가 복사합니다."
)
_CRITERION_PRINCIPALS_DESCRIPTION = (
    "이 criterion이 다루는 principals입니다. question의 principal_refs 부분집합이며 등록되고 "
    "허용된 항목만 사용합니다. subjects를 넓히거나 권한을 부여하지 않습니다."
)
_SPLIT_PLAN_DESCRIPTIONS = {
    "questions": (
        "필수입니다. 새 question이 없거나 context_request만 할 때 []를 사용합니다. 완료된 Task의 "
        "후속 조사는 새 목적의 question으로 제안합니다."
    ),
    "termination": (
        "필수입니다. continue는 새 question이나 claimable/running work, held/exhausted는 정확한 "
        "resume_conditions를 요구합니다. replan은 제한된 추가 판단, goal_achieved는 완료 "
        "제안입니다. "
        "context_request는 continue와 빈 updates/questions를 사용합니다."
    ),
}


def _split_plan_response_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Model-facing stage-1 schema; persisted HypothesisPlanDraft defaults stay unchanged."""
    schema = deepcopy(schema)
    required = list(schema.get("required", []))
    schema["required"] = list(dict.fromkeys([*required, *_SPLIT_PLAN_REQUIRED]))
    for name, text in _SPLIT_PLAN_DESCRIPTIONS.items():
        schema["properties"][name]["description"] = text
    defs = schema.get("$defs", {})
    criterion = defs.get("CompletionCriterion")
    if criterion is not None:
        criterion_required = ("kind", "description")
        criterion["required"] = list(dict.fromkeys([
            *criterion.get("required", []), *criterion_required,
        ]))
        criterion["properties"]["principal_refs"]["description"] = (
            _CRITERION_PRINCIPALS_DESCRIPTION
        )
    question = defs.get("PlannedQuestionSelection")
    if question is not None:
        question["required"] = list(dict.fromkeys([
            *question.get("required", []), "candidate_refs",
        ]))
        question["properties"]["candidate_refs"]["description"] = _CANDIDATE_REFS_DESCRIPTION
        # Wire copy only: an omitted source_result_id is derived from the captured Task, so
        # the model must not see a null default to echo back.
        question["properties"]["source_result_id"].pop("default", None)
    return schema


async def _ask_with_context(planner, pass_context, engine, *, payload, budget, **kwargs):
    """Optional reads share the client's normal/repair budget, ledger and guard."""
    current = payload
    while True:
        if context_bytes(current) > 131072:
            raise DecisionFailure("hypothesis_planning", "format", budget.used,
                                  diagnostic=DecisionDiagnostic(
                code="planner_required_context_too_large", stage="hypothesis_planning",
                kind="validation", summary="Mandatory current context exceeds the bounded "
                "request envelope. Contracts were not sliced or silently omitted.", retryable=False,
            ))
        try:
            async def request_context(captured: dict[str, Any] = current) -> dict[str, Any]:
                # Refresh observational accounting for each actual attempt,
                # including repairs. Canonical subjects stay on this pass's
                # captured snapshot and still undergo the original related CAS.
                state = planner.deps.central_state
                usage = await state.request_ledger().snapshot(state.run_id)
                remaining = await state.transaction(
                    lambda tx: tx.policy_budget_remaining(), write=False,
                )
                data = {
                    "model_usage": {key: usage[key] for key in (
                        "policy", "spent", "reserved", "known_tokens", "unknown_usage",
                        "unknown_dispatch", "usage", "by_stage", "provider_cooldown",
                    )},
                    "decision_budget": {
                        "used": budget.used, "remaining": budget.remaining,
                        "normal_used": budget.normal_used, "repair_used": budget.repair_used,
                        "normal_limit": budget.normal_limit, "repair_limit": budget.repair_limit,
                    },
                    "remaining_budget": remaining if remaining is not None
                        else payload["remaining_budget"],
                }
                if context_bytes({**captured, **data}) > 131072:
                    attempts = max(0, budget.used - 1)
                    raise DecisionFailure("hypothesis_planning", "format", attempts,
                                          diagnostic=DecisionDiagnostic(
                        code="planner_required_context_too_large", stage="hypothesis_planning",
                        kind="validation", attempts=attempts,
                        summary="Current request accounting exceeds the "
                        "bounded envelope. No judgment was committed.", retryable=False,
                    ))
                # This request contains only code-owned numeric accounting. Preserve
                # metric keys such as input_tokens/cache_read_tokens; the default
                # secret-key filter would mistake those counters for credentials.
                return redact_secret_material(data, drop_secret_keys=False)

            raw = await planner.client.ask(
                payload=current, budget=budget, request_context=request_context, **kwargs,
            )
        except DecisionFailure as exc:
            if (exc.diagnostic.code == "decision_normal_budget_exhausted"
                    and pass_context.last_request is not None):
                raise DecisionFailure("hypothesis_planning", "budget", budget.used,
                                      diagnostic=exc.diagnostic.model_copy(update={
                    "code": "planner_lookup_budget_exhausted",
                    "summary": "Context lookup remained unresolved when the shared judgment "
                    "budget was exhausted. No candidate/task/cursor was committed.",
                })) from exc
            raise
        request = raw.get("context_request")
        if request is None:
            return raw
        parsed = PlannerContextRequest.model_validate(request)
        await kwargs["before_call"]()
        consumed_before = {kind: set(values) for kind, values in pass_context.consumed.items()}
        evidence_before = set(pass_context.used_evidence)
        cache_before = dict(pass_context.cache)
        result, visible = await pass_context.lookup(parsed, before_each=kwargs["before_call"])
        pass_context.last_request = parsed.model_dump(mode="json")
        if not pass_context.retain_lookup_window(
                _planner_base_payload(current),
                pass_context.last_request, result, visible):
            pass_context.consumed = consumed_before
            pass_context.used_evidence = evidence_before
            pass_context.cache = cache_before
            result = pass_context.batch_budget_result(parsed)
            visible = set()
            if not pass_context.retain_lookup_window(
                    payload, pass_context.last_request, result, visible):
                raise DecisionFailure("hypothesis_planning", "format", budget.used,
                                      diagnostic=DecisionDiagnostic(
                    code="context_batch_budget_exceeded", stage="hypothesis_planning",
                    kind="validation", summary="Even the bounded lookup diagnostic cannot fit "
                    "the remaining request envelope. No lookup references were admitted.",
                    retryable=False, cause_scope="runtime_contract", owner="planner_context"))
        current = {**payload, **pass_context._retained_lookup_payload(current, parsed)}


def _planner_base_payload(current):
    """Strip only lookup-owned fields before recomputing retained-window capacity."""
    return {key: value for key, value in current.items()
            if key not in {"context_lookup_result", "context_lookup_history",
                           "context_lookup_results"}}


async def _advance_split(planner, runtime, schema, snapshot, engine, budget, payload,
                         targets, principals, roles, records, pass_context, request_guard):
    """Two asks on ONE DecisionBudget: questions first, then probe tasks, one CAS commit.

    Empty/held/update-only plans skip the atomizer (no second request). A
    stage-handoff failure writes no Planner update or Task; canonical state committed
    before this pass (including review-applied candidates and Findings) is unaffected.
    Requests already consumed stay charged to the shared Run ledger.
    """
    deps = planner.deps
    issued: dict[str, str] = {}

    def validate_existing_work(task_ids, versions):
        return _existing_work_witnesses(
            task_ids, snapshot, eligible_roles=roles,
            require_probe_limits=planner.policy.probe_limits is not None,
            candidate_revisions=versions,
        )

    def bind_stage_one(raw):
        validate_candidate_status_presence(raw)
        validate_planner_candidate_authority(raw)
        selection = HypothesisPlanSelection.model_validate(raw)
        if lookup_only(selection):
            return selection, None, [], {}
        pass_context.validate_reviewed(selection)
        bound = bind_plan_selection(
            selection, snapshot, issued=issued, targets=targets, principals=principals,
            max_tasks=planner.policy.max_tasks_per_strategy,
            existing_work_validator=validate_existing_work,
        )
        return bound

    def validate_plan(raw):
        try:
            bind_stage_one(raw)
        except (ValueError, SpecialistProposalError) as exc:
            raise DecisionValidationError(_proposal_validation_diagnostic(
                exc, stage="hypothesis_planning",
                summary=_proposal_repair_summary(
                    exc,
                    "Use captured subjects, candidate revisions and original "
                    "source results. A new candidate key is local to this proposal; "
                    "questions carry "
                    "purpose only, never worker tasks. Empty judgments need a termination reason "
                    "and resume_conditions.",
                ),
                schema=_split_plan_response_schema(planner_candidate_wire_schema(
                    HypothesisPlanSelection.model_json_schema()
                )),
            )) from exc

    with llm_request_scope(str(runtime.run_id), "hypothesis_planning"):
        plan_raw = await _ask_with_context(planner, pass_context, engine,
            stage="hypothesis_planning", payload=payload, budget=budget,
            response_schema=_split_plan_response_schema(
                planner_candidate_wire_schema(schema(HypothesisPlanSelection.model_json_schema()))
            ),
            validate_response=validate_plan, before_call=request_guard,
            system=_SPLIT_PLAN_SYSTEM,
        )
    draft, updates, questions, versions = bind_stage_one(plan_raw)
    identity = new_id("planner_pass")
    if updates is not None and needs_atomization(draft, questions):
        request_guard.include(*related_reads(
            draft, HypothesisProbeProposal(updates=updates, tasks=[]) if updates else None,
            snapshot, records,
            questions=questions, issued=issued, eligible_roles=roles,
            require_probe_limits=planner.policy.probe_limits is not None,
        ))
        try:
            handoff = pass_context.handoff(questions, updates)
            atom_payload = redact_secret_material(build_atomizer_input(
                {**payload, **handoff}, plan=draft, updates=updates, questions=questions,
                issued=issued, versions=versions,
            ))
            if context_bytes(atom_payload) > 131072:
                from redteam.orchestration.investigation_context import ContextReadError

                raise ContextReadError("atomizer_required_context_too_large")
        except ValueError as exc:
            raise DecisionFailure("task_atomization", "format", budget.used,
                                  diagnostic=_proposal_validation_diagnostic(
                exc, stage="task_atomization", summary="Required source material exceeds the "
                "bounded handoff. No Planner candidate update or question Task was committed; "
                "canonical review-applied candidates and Findings remain unchanged.",
                schema=TaskAtomizationDraft.model_json_schema(),
            )) from exc

        def validate_tasks(raw):
            try:
                parsed = TaskAtomizationDraft.model_validate(raw)
                hydrated = resolve_atomized_tasks(parsed.tasks, questions=questions)
                validate_atomized_tasks(
                    hydrated, questions=questions, issued=issued, versions=versions,
                    targets=targets, principals=principals, roles=roles,
                    limits=planner.policy.probe_limits, snapshot=snapshot,
                    max_tasks=planner.policy.max_tasks_per_strategy,
                )
            except (ValueError, SpecialistProposalError) as exc:
                raise DecisionValidationError(_proposal_validation_diagnostic(
                    exc, stage="task_atomization",
                    summary="Cover every planned question with at least one "
                    "probe task. Preserve each question's subjects, completion criteria, "
                    "candidate revisions, required meaning and source refs; keep a "
                    "paired-comparison criterion as one condition on one task.",
                    schema=TaskAtomizationDraft.model_json_schema(),
                )) from exc

        with llm_request_scope(str(runtime.run_id), "hypothesis_planning"):
            tasks_raw = await planner.client.ask(
                stage="task_atomization", payload=atom_payload, budget=budget,
                response_schema=schema(TaskAtomizationDraft.model_json_schema()),
                validate_response=validate_tasks, before_call=request_guard,
                system=_SPLIT_ATOMIZE_SYSTEM,
            )
        parsed = TaskAtomizationDraft.model_validate(tasks_raw)
        hydrated = resolve_atomized_tasks(parsed.tasks, questions=questions)
        atom_tasks = validate_atomized_tasks(
            hydrated, questions=questions, issued=issued, versions=versions,
            targets=targets, principals=principals, roles=roles,
            limits=planner.policy.probe_limits, snapshot=snapshot,
            max_tasks=planner.policy.max_tasks_per_strategy,
        )
        decision = HypothesisDecision(
            updates=draft.updates, tasks=atom_tasks,
            reviewed_task_ids=draft.reviewed_task_ids,
            reviewed_hypothesis_ids=draft.reviewed_hypothesis_ids,
            reason=draft.reason, termination=draft.termination,
            resume_conditions=draft.resume_conditions,
        )
        proposal = bind_decision(
            decision, snapshot, issued=issued, targets=targets, principals=principals,
            roles=roles, max_tasks=planner.policy.max_tasks_per_strategy,
            limits=planner.policy.probe_limits,
        )
        coverage = {
            question.question_id: [task.task_key for task in parsed.tasks
                                   if task.question_id == question.question_id]
            for question in questions
        }
        atomization = {"question_ids": [question.question_id for question in questions],
                       "coverage": coverage, "skipped": None}
    else:
        decision = draft
        proposal = (HypothesisProbeProposal(updates=updates, tasks=[])
                    if updates is not None else None)
        atomization = {"question_ids": [question.question_id for question in questions],
                       "coverage": {},
                       "skipped": ("update_only_no_questions" if updates is not None
                                   else "held_or_empty")}
    graph_read, task_read = related_reads(
        decision, proposal, snapshot, records, questions=questions, issued=issued,
        eligible_roles=roles,
        require_probe_limits=planner.policy.probe_limits is not None,
    )
    lookup_graph, lookup_tasks = pass_context.read_dependencies()
    graph_read.update(lookup_graph)
    task_read.update(lookup_tasks)
    judgment = {**decision.model_dump(mode="json"), "sequence": snapshot.sequence,
                "issued_candidate_ids": issued, "graph_read": graph_read, "task_read": task_read,
                "candidate_inventory": sorted(snapshot.candidates),
                "task_inventory": sorted(snapshot.tasks),
                "questions": [question.model_dump(mode="json") for question in questions],
                "atomization": atomization, **pass_context.auxiliary_reads(),
                **_judgment_controls(planner, roles)}
    return await _finalize_hypothesis_pass(
        deps, snapshot, budget, identity=identity, proposal=proposal, judgment=judgment,
        graph_read=graph_read, task_read=task_read, targets=targets, principals=principals,
        max_tasks=planner.policy.max_tasks_per_strategy,
    )
