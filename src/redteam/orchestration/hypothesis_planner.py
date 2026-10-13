"""One candidate/probe judgment over captured originals and existing central admission."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.predicates import facts_of
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import GraphNode, Hypothesis, HypothesisUpdate
from redteam.core.contracts.mission import MissionPlan, MissionTask, redact_secret_material
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId, new_id
from redteam.orchestration.agents.tool_access import role_readiness
from redteam.orchestration.central_results import RESULT_UPDATED
from redteam.orchestration.central_runtime import event, process_controls, runtime_token
from redteam.orchestration.cognition import DecisionFailure, DecisionValidationError
from redteam.orchestration.coordination import PlanningInputsChanged, StateConflict, content_hash
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.knowledge_validation import known_principals, known_target_ids
from redteam.orchestration.metered_llm import llm_request_scope
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
    termination: Literal["continue", "held", "exhausted", "goal_achieved"] = "continue"
    resume_conditions: list[str] = Field(default_factory=list)


class HypothesisProposalError(ValueError):
    """Safe, code-owned repair reason; never contains a proposed body or secret."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class PlanningSnapshot:
    mission: MissionPlan
    tasks: dict[str, MissionTask]
    nodes: dict[str, GraphNode]
    candidates: dict[str, Hypothesis]
    binding: str
    sequence: int
    consumed: int | None


def capture_snapshot(tx):
    mission = tx.admit()
    nodes = {}
    for node in tx.graph.list_nodes():
        identity = str(node.id).removeprefix(tx.engagement_id + "\x1f")
        nodes[identity] = node.model_copy(update={"id": GraphNodeId(identity)})
    candidates = {facts_of(node.properties)["hypothesis_id"]:
                  Hypothesis.model_validate(facts_of(node.properties))
                  for node in nodes.values() if node.type is GraphNodeType.HYPOTHESIS}
    return PlanningSnapshot(
        mission, {str(task.task_id): task for task in tx.list_tasks()}, nodes, candidates,
        tx.binding()[1], tx.latest_event_sequence(tuple(PROBE_WAKE_EVENTS)),
        tx.hypothesis_planner_cursor(),
    )


def bind_decision(decision, snapshot, *, issued, targets, principals, roles, max_tasks, limits):
    """Resolve local references and validate purpose inputs without mutating originals."""
    updates, tasks = [], []
    names = [update.hypothesis.hypothesis_id for update in decision.updates]
    if not decision.reason.strip() or any(
        not value.strip() for value in decision.resume_conditions
    ):
        raise HypothesisProposalError("judgment_reason_or_resume_condition_blank")
    if decision.tasks and decision.termination != "continue":
        raise HypothesisProposalError("new_work_requires_continue_judgment")
    if len(set(names)) != len(names) or len(decision.tasks) > max_tasks:
        raise HypothesisProposalError("candidate_identity_or_task_count_invalid")
    for update in decision.updates:
        draft = update.hypothesis
        identity = draft.hypothesis_id
        current = snapshot.candidates.get(identity)
        if update.expected_revision is None:
            if current is not None or draft.status in {"confirmed", "rejected"}:
                raise HypothesisProposalError("new_candidate_requires_local_key_and_open_judgment")
            if identity not in issued:
                issued[identity] = new_id("hyp")
            identity = issued[identity]
        elif current is None or current.revision != update.expected_revision:
            raise HypothesisProposalError("candidate_expected_revision_not_in_snapshot")
        if set(draft.target_node_ids) - targets or set(draft.principal_refs) - principals:
            raise HypothesisProposalError("candidate_subject_not_registered")
        if set(draft.source_hypothesis_ids) - snapshot.candidates.keys():
            raise HypothesisProposalError("candidate_sources_must_reference_existing_snapshots")
        if draft.status == "held" and not decision.resume_conditions:
            raise HypothesisProposalError("held_candidate_requires_resume_conditions")
        updates.append(update.model_copy(update={"hypothesis": draft.model_copy(update={
            "hypothesis_id": identity,
            "metadata": {**draft.metadata, **({"resume_conditions": decision.resume_conditions}
                                              if draft.status == "held" else {})},
        })}))
    versions = {identity: candidate.revision for identity, candidate in snapshot.candidates.items()}
    versions.update({update.hypothesis.hypothesis_id: (update.expected_revision or 0) + 1
                     for update in updates})
    for draft in decision.tasks:
        if draft.role_id not in roles:
            raise HypothesisProposalError("probe_role_not_ready")
        revisions = {issued.get(identity, identity): revision
                     for identity, revision in draft.hypothesis_revisions.items()}
        if any(versions.get(identity) != revision for identity, revision in revisions.items()):
            raise HypothesisProposalError("probe_candidate_revision_not_in_proposal")
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
    if not (updates or tasks):
        if decision.termination == "continue" or not decision.resume_conditions:
            raise HypothesisProposalError("empty_judgment_needs_termination_and_resume_conditions")
        return None
    proposal = HypothesisProbeProposal(updates=updates, tasks=tasks)
    validate_probe_subjects(proposal, known_targets=targets, known_principals=principals,
                            known_hypotheses=versions.keys())
    return proposal


def related_reads(decision, proposal, snapshot, records):
    hypotheses = set(decision.reviewed_hypothesis_ids)
    tasks = set(decision.reviewed_task_ids)
    subjects = set()
    evidence = set()
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
        new = {update.hypothesis.hypothesis_id for update in proposal.updates
               if update.expected_revision is None}
        for task in proposal.tasks:
            hypotheses.update(set(task.hypothesis_revisions) - new)
            subjects.update(task.target_node_ids)
            evidence.update(task.evidence_ids)
            if task.source_task_id:
                tasks.add(task.source_task_id)
    if decision.termination != "continue":
        # A global stop/exhaustion judgment consumed all current work, not one candidate.
        hypotheses.update(snapshot.candidates)
        tasks.update(snapshot.tasks)
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


async def advance_hypotheses(planner, runtime, schema):
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
    payload = redact_secret_material({
        "mission": snapshot.mission.model_dump(mode="json"),
        "candidates": [candidate.model_dump(mode="json")
                       for candidate in snapshot.candidates.values()],
        "investigations": [task.model_dump(mode="json") for task in snapshot.tasks.values()],
        "knowledge": context["knowledge"], "constraints": context["engagement_constraints"],
        "operator_instructions": context["operator_instructions"],
        "role_readiness": readiness, "tools_by_role": context["tools_by_role"],
        "tool_cards": context["tool_cards"], "known_target_ids": sorted(targets),
        "registered_principal_refs": sorted(principals),
        "evidence": context["evidence"], "evidence_index": context["evidence_index"],
        "evidence_validity": context["evidence_validity"],
        "review_receipts": [{"evidence_id": str(record.evidence_id), "metadata": record.metadata}
                            for record in records if record.metadata.get("public") is True
                            and record.metadata.get("record_kind") == "review_receipt"],
        "remaining_budget": context["remaining_budget"],
        "model_usage": await state.request_ledger().snapshot(state.run_id),
        "probe_limits": planner.policy.probe_limits.model_dump(),
        "max_tasks": planner.policy.max_tasks_per_strategy,
        "input_sequence": snapshot.sequence,
    })
    payload["knowledge"].pop("hypotheses", None)  # Captured candidate objects above own this input.
    issued = {}

    def bind(raw):
        decision = HypothesisDecision.model_validate(raw)
        proposal = bind_decision(
            decision, snapshot, issued=issued, targets=targets, principals=principals,
            roles=roles, max_tasks=planner.policy.max_tasks_per_strategy,
            limits=planner.policy.probe_limits,
        )
        return decision, proposal, related_reads(decision, proposal, snapshot, records)

    def validate(raw):
        try:
            bind(raw)
        except (ValueError, SpecialistProposalError) as exc:
            raise DecisionValidationError(DecisionDiagnostic(
                code=getattr(exc, "code", "hypothesis_proposal_invalid"),
                stage="hypothesis_planning", kind="validation",
                retryable=True, summary="Use captured subjects, ready roles, candidate revisions "
                "and original source results. A new candidate key is local to this proposal. "
                "Empty judgments need a termination reason and resume_conditions.",
            )) from exc

    with llm_request_scope(str(runtime.run_id), "hypothesis_planning"):
        raw = await planner.client.ask(
            stage="hypothesis_planning", payload=payload, budget=budget,
            response_schema=schema(HypothesisDecision.model_json_schema()),
            validate_response=validate, before_call=engine.guard_reasoning,
            system="Propose candidates and bounded verification purposes in one judgment. "
            "New updates use a proposal-local hypothesis_id and expected_revision=null; code "
            "issues the persistent ID. Tasks may reference that key with revision=1. Existing "
            "updates use captured IDs and expected revisions; tasks use the resulting revision. "
            "Identify reviewed_task_ids/reviewed_hypothesis_ids actually consumed. Source "
            "followups cite the captured Task/result pair, without inventing a question. "
            "Observations and Task outcome are not candidate verdicts. Final verdicts require "
            "an independent public review receipt for that exact candidate revision/purpose. "
            "No candidates initially means a useful scoped observation or reasoned hold. "
            "Same evidence can support another useful probe. Never prescribe tool-call order, "
            "invent subjects or overwrite active work. Held/exhausted judgments must explain "
            "reconsideration conditions; goal success remains a separate completion gate.",
        )
    decision, proposal, (graph_read, task_read) = bind(raw)
    identity = new_id("planner_pass")
    judgment = {**decision.model_dump(mode="json"), "sequence": snapshot.sequence,
                "issued_candidate_ids": issued, "graph_read": graph_read, "task_read": task_read,
                "candidate_inventory": sorted(snapshot.candidates),
                "task_inventory": sorted(snapshot.tasks)}
    try:
        if proposal is not None:
            receipt = await register_probe_tasks(
                deps, proposal_id=identity, proposal=proposal,
                expected_revision=snapshot.mission.revision, expected_binding_hash=snapshot.binding,
                graph_read=graph_read, task_read=task_read, known_targets=targets,
                known_principals=principals, max_tasks=planner.policy.max_tasks_per_strategy,
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
    except PlanningInputsChanged:
        await state.transaction(lambda tx: tx.queue(event(
            state, PLANNING_INVALIDATED,
            {"pass_id": identity, "reason_code": "related_planning_inputs_changed"},
        )))
        identities = []
    except (StateConflict, ValueError) as exc:
        raise DecisionFailure("hypothesis_planning", "format", budget.used,
                              diagnostic=DecisionDiagnostic(
            code="hypothesis_admission_rejected", stage="hypothesis_planning", kind="validation",
            summary="The candidate/probe proposal failed original evidence, verdict, scope "
                    "or source admission. No candidate/task change was committed.",
            retryable=False,
        )) from exc
    await deps.event_bus.flush()
    return identities
