"""Budgeted strategy -> atomization -> canonical Task admission.

No provider tools are exposed to these judgments. They propose investigation
work, never execution permission, raw Tool calls, or a terminal goal result.
"""

import asyncio
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import ALLOWED_CRITERION_KINDS
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import new_id
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionValidationError,
    StructuredDecisionClient,
)
from redteam.orchestration.coordination import PlanningInputsChanged
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.knowledge_validation import known_principals, known_target_ids
from redteam.orchestration.metered_llm import llm_request_guard, llm_request_scope
from redteam.orchestration.reasoning_time import planning_request_guard
from redteam.orchestration.specialist_tasks import (
    AtomicTaskProposal,
    InvestigationQuestion,
    SpecialistProposalError,
    StrategyTasks,
    TaskNecessityProposal,
    validate_necessity_proposals,
    validate_strategy_questions,
    validate_strategy_tasks,
)


class StrategyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[InvestigationQuestion]
    reason: str = Field(min_length=1)
    termination: Literal["continue", "replan", "exhausted", "goal_achieved", "held"] = "continue"
    reviewed_task_ids: list[str] = Field(default_factory=list)
    resume_conditions: list[str] = Field(default_factory=list)
    task_adjustments: list[TaskNecessityProposal] = Field(default_factory=list)

    @model_validator(mode="after")
    def _judgment_shape(self) -> "StrategyDraft":
        if not self.reason.strip():
            raise ValueError("planner_reason_blank")
        if len(set(self.reviewed_task_ids)) != len(self.reviewed_task_ids):
            raise ValueError("planner_reviewed_task_ids_duplicate")
        if any(not item.strip() for item in self.reviewed_task_ids):
            raise ValueError("planner_reviewed_task_id_blank")
        if self.termination != "continue" and (self.questions or self.reviewed_task_ids):
            raise ValueError("planner_terminal_judgment_has_work")
        if self.termination in {"held", "exhausted"} and not any(
            item.strip() for item in self.resume_conditions
        ):
            raise ValueError("planner_resume_condition_required")
        if any(not item.strip() for item in self.resume_conditions):
            raise ValueError("planner_resume_condition_blank")
        return self


class AtomizationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[AtomicTaskProposal] = Field(min_length=1)


def _schema_with_completion_criterion_hint(schema: dict[str, Any]) -> dict[str, Any]:
    """Declare the allowed ``completion_criteria.kind`` values in the proposed schema.

    ``CompletionCriterion.kind`` is a permissive ``str`` on the shared contract (other
    consumers, e.g. ``PathPlan``'s raw storage schema, must stay enum-free -- see
    ``orchestration/paths/writing.py``'s own narrower patch for that surface). This
    copy is local to the schema handed to the model for this decision only; it does
    not touch the contract or any other schema the model, or anything else, sees.
    Without it the model has no way to learn the allowed set, and a response that
    picks an unlisted value is normalized to "unknown" and rejected -- previously with
    no indication of what the real set was, so a retry had nothing to correct.
    """
    schema = deepcopy(schema)
    criterion = schema.get("$defs", {}).get("CompletionCriterion")
    if criterion is not None:
        criterion["properties"]["kind"]["enum"] = sorted(ALLOWED_CRITERION_KINDS)
    return schema


def specialist_planning_inputs(deps) -> dict:
    """Assembler-owned input identity, also checked before reservation recovery."""
    inputs = {
        # Schema 3 is the candidate-planner/common-worker meaning. Schema 4 adds
        # bounded progression to the legacy investigation planner; prior snapshots
        # keep their original identity and are not reinterpreted on resume.
        "schema_version": 3 if deps.specialist_policy.probe_limits is not None else 4,
        # Assembly assigns a fresh created_at on every CLI rebuild. It is not
        # scope/policy and must not stale an otherwise identical saved Run.
        # All substantive fields, including the original engagement ID, remain.
        "engagement": deps.engagement.model_dump(mode="json", exclude={"created_at"})
        if deps.engagement else None,
        "components": deps.component_binding.model_dump(mode="json")
        if deps.component_binding else None,
        "specialists": deps.specialist_policy.model_dump(mode="json")
        if deps.specialist_policy else None,
    }
    # Preserve legacy input identity when this optional mechanism is absent.
    fixture_runner = getattr(deps, "code_fixture_runner", None)
    if fixture_runner is not None:
        inputs["code_fixture_runner"] = fixture_runner.fingerprint
    return inputs


_MAX_PLANNER_INTENT_HISTORY = 5


def operator_planner_context(mission, intents, *, run_id=None) -> dict:
    from redteam.core.enums import OperatorIntentType
    from redteam.orchestration.operator_intents.models import IntentStatus
    applied = [r for r in intents if r.status is IntentStatus.APPLIED]
    if run_id is not None:
        applied = [
            r for r in applied
            if r.run_id is None or str(r.run_id) == str(run_id)
        ]
    applied.sort(key=lambda r: (r.intent.created_at, str(r.intent_id)))
    prefs = mission.operator_preferences or {}
    guidance = None
    for record in reversed(applied):
        if record.intent.intent_type is OperatorIntentType.GUIDANCE:
            guidance = record.intent.instruction[:500]
            break
    history = applied[-_MAX_PLANNER_INTENT_HISTORY:]
    ledger = [
        {
            "intent_id": str(record.intent_id),
            "intent_type": record.intent.intent_type.value,
            "created_at": record.intent.created_at.isoformat(),
            "outcome": record.outcome,
        }
        for record in history
    ]

    def _bounded_list(value, limit=8):
        return [str(i) for i in (value or []) if str(i).strip()][:limit]

    return {
        "mission_revision": mission.revision,
        "guidance": guidance,
        "focus_areas": _bounded_list(prefs.get("focus_areas")),
        "focus_endpoints": _bounded_list(prefs.get("focus_endpoints")),
        "prioritized_task_ids": _bounded_list(prefs.get("prioritized_task_ids"), 32),
        "excluded_areas": _bounded_list(prefs.get("excluded_areas")),
        "excluded_endpoints": _bounded_list(prefs.get("excluded_endpoints")),
        "excluded_task_ids": _bounded_list(prefs.get("excluded_task_ids"), 32),
        "applied_intents": ledger,
    }


class SpecialistPlanner:

    def __init__(self, deps) -> None:
        if deps.specialist_policy is None:
            raise ValueError("specialist_policy_missing")
        self.deps, self.policy = deps, deps.specialist_policy
        self.coordinator: Any = None
        self.admission: Any = None
        self.client = StructuredDecisionClient(
            deps.llm_client,
            max_attempts=deps.settings.decision_max_attempts,
            transport_limit=deps.settings.decision_transport_max_attempts,
            decision_timeout_seconds=deps.settings.decision_timeout_seconds,
            llm_attempt_timeout_seconds=deps.settings.llm_attempt_timeout_seconds,
        )
        self._hypothesis_lock = asyncio.Lock()

    def wake_events(self):
        """The selected planning contract owns meaningful decision wake sources."""
        if self.policy.probe_limits is not None:
            from redteam.orchestration.hypothesis_planner import PROBE_WAKE_EVENTS

            return PROBE_WAKE_EVENTS
        from redteam.core.contracts.events import EventType
        from redteam.orchestration.central_results import RESULT_UPDATED
        from redteam.orchestration.central_runtime import ACTOR_RETURNED
        from redteam.orchestration.planner_progress import PLANNING_INVALIDATED

        return frozenset({RESULT_UPDATED, ACTOR_RETURNED, EventType.GRAPH_UPDATED,
                          EventType.EVIDENCE_CREATED, EventType.EVIDENCE_VALIDITY_CHANGED,
                          EventType.TASK_COMPLETED,
                          EventType.OPERATOR_INTENT_APPLIED, PLANNING_INVALIDATED})

    async def advance(self, runtime) -> list[str]:
        """Propose current work under the selected planning contract and shared Run budget."""
        if runtime.mission_id is None:
            raise ValueError("specialist_planning_requires_mission")
        if self.policy.probe_limits is not None:
            from redteam.orchestration.hypothesis_planner import advance_hypotheses

            async with self._hypothesis_lock:
                return await advance_hypotheses(
                    self, runtime, _schema_with_completion_criterion_hint,
                )
        return await self._advance_central(runtime)

    async def _advance_central(self, runtime):
        """Judge current investigations at a notification, without graph-digest gates."""
        from redteam.orchestration.agents.tool_access import role_readiness
        from redteam.orchestration.central_completion import goal_snapshot
        from redteam.orchestration.central_runtime import (
            process_controls,
            runtime_token,
            task_is_claimable,
        )
        from redteam.orchestration.planner_progress import PLANNING_INVALIDATED
        from redteam.orchestration.specialist_tasks import register_investigation_tasks

        await process_controls(self.deps)
        await runtime_token(self.deps, runtime, require_task=False)
        runtime = runtime.model_copy(update={"active_task_id": None, "current_agent": None})
        wake_kinds = tuple(self.wake_events())
        sequence = await self.deps.central_state.transaction(
            lambda tx: tx.latest_event_sequence(wake_kinds), write=False,
        )
        consumed = await self.deps.central_state.transaction(
            lambda tx: tx.hypothesis_planner_cursor(), write=False,
        )
        if consumed is not None and sequence <= consumed:
            return []
        stamp = await self.deps.central_state.transaction(
            lambda tx: goal_snapshot(tx, include_assessment=False)[2], write=False,
        )
        engine = InvestigationEngine(self.deps)
        context = await engine.context(runtime, None)
        mission = await self.deps.central_state.get_mission(runtime.mission_id)
        intents = await self.deps.central_state.list_for_mission(runtime.mission_id)
        operator_context = operator_planner_context(mission, intents, run_id=runtime.run_id)
        central_tasks = await self.deps.central_state.list_tasks()
        existing_tasks = {str(task.task_id): task for task in central_tasks}
        admission_guard = planning_request_guard(self.deps, runtime)
        material = await engine.execution_material(runtime)
        targets, principals = known_target_ids(material), known_principals(material)
        hypotheses = await AttackGraphService(self.deps.graph_store).list_hypotheses()
        hypothesis_ids = frozenset(h.hypothesis_id for h in hypotheses)
        candidate_revisions = {str(item.hypothesis_id): item.revision for item in hypotheses}
        readiness = await role_readiness(self.deps)
        eligible_roles = {
            item["role_id"] for item in readiness if item["activation_eligible"]
        }
        payload = redact_secret_material({
            "goal": context["goal"], "success_criteria": context["success_criteria"],
            "constraints": context.get("engagement_constraints"),
            "knowledge": context["knowledge"], "roles": context["roles"],
            "tools_by_role": context["tools_by_role"], "tool_cards": context["tool_cards"],
            "role_readiness": readiness,
            "evidence_index": context["evidence_index"], "evidence": context["evidence"],
            "evidence_validity": context["evidence_validity"],
            "investigations": context["investigation_tasks"],
            "unresolved_results": context["unresolved_results"],
            "registered_principal_refs": sorted(principals),
            "known_target_ids": sorted(targets), "max_tasks": self.policy.max_tasks_per_strategy,
            "mission_revision": mission.revision,
            "operator_guidance": operator_context,
        })
        budget = DecisionBudget(normal_limit=self.deps.settings.decision_normal_limit,
                                repair_limit=self.deps.settings.decision_max_attempts,
                                transport_limit=self.deps.settings.decision_transport_max_attempts,
                                timeout_seconds=self.deps.settings.decision_timeout_seconds)

        def check_questions(raw):
            try:
                draft = StrategyDraft.model_validate(raw)
                validate_strategy_questions(
                    draft.questions, known_targets=targets,
                    known_principals=principals, known_hypotheses=hypothesis_ids,
                    investigation_only=True,
                )
                if set(draft.reviewed_task_ids) - existing_tasks.keys():
                    raise SpecialistProposalError("planner_reviewed_task_unknown")
                if draft.termination == "continue" and not draft.questions:
                    if not draft.reviewed_task_ids:
                        raise SpecialistProposalError("planner_continue_requires_progress")
                    if not any(
                        task_is_claimable(
                            existing_tasks[identity], eligible_roles=eligible_roles,
                            mission=mission, require_probe_limits=False,
                            candidate_revisions=candidate_revisions, allow_running=True,
                        )
                        for identity in draft.reviewed_task_ids
                    ):
                        raise SpecialistProposalError(
                            "planner_continue_without_claimable_work",
                        )
                validate_necessity_proposals(
                    draft.task_adjustments, mission=mission, tasks_by_id=existing_tasks,
                    evidence_ids=set(context["evidence_index"]),
                )
            except (ValueError, SpecialistProposalError) as caught:
                exc = (caught if isinstance(caught, SpecialistProposalError)
                       else SpecialistProposalError("specialist_invalid_proposal"))
                if exc.code.startswith("planner_adjustment_"):
                    raise DecisionValidationError(DecisionDiagnostic(
                        code=exc.code, stage="attack_path_proposal", kind="validation",
                        retryable=True,
                        summary=_REJECTION_SUMMARIES.get(
                            exc.code, "The task adjustment is not grounded."),
                        location="$.task_adjustments",
                    )) from exc
                raise self._rejection(exc, "attack_path_proposal") from exc

        def check_tasks(raw):
            try:
                validate_strategy_tasks(
                    StrategyTasks(questions=strategy.questions,
                                  tasks=AtomizationDraft.model_validate(raw).tasks),
                    agents=self.deps.agents, known_targets=targets, known_principals=principals,
                    known_hypotheses=hypothesis_ids, max_tasks=self.policy.max_tasks_per_strategy,
                    investigation_only=True,
                )
            except SpecialistProposalError as exc:
                raise self._rejection(exc, "task_atomization") from exc

        async def commit_pass(atomization):
            """One planner commit: admitted inputs, adjustments, Tasks and judgment together."""
            identity = new_id("planner_pass")
            judgment = {
                **strategy.model_dump(mode="json"),
                "sequence": sequence,
                "tasks": [] if atomization is None else [
                    item.task_key for item in atomization.tasks],
                "replan_max_passes": self.deps.settings.planner_replan_max_passes,
                "witness_inputs": {
                    "eligible_roles": sorted(eligible_roles),
                    "require_probe_limits": False,
                },
            }

            def guard(tx):
                tx.admit()
                # Any wake-kind event or goal change since the snapshot makes these inputs stale.
                return (tx.latest_event_sequence(wake_kinds) == sequence
                        and goal_snapshot(tx, include_assessment=False)[2] == stamp)

            registration = None if atomization is None else StrategyTasks(
                questions=strategy.questions, tasks=atomization.tasks)
            try:
                identities = await register_investigation_tasks(
                    self.deps.central_state, proposal_id=identity, proposal=registration,
                    agents=self.deps.agents, known_targets=targets, known_principals=principals,
                    known_hypotheses=hypothesis_ids, max_tasks=self.policy.max_tasks_per_strategy,
                    evidence_store=self.deps.evidence_store, expected_revision=mission.revision,
                    necessity=strategy.task_adjustments, planner_judgment=judgment,
                    planning_guard=guard,
                )
            except PlanningInputsChanged:
                from redteam.orchestration.central_runtime import event

                await self.deps.central_state.transaction(lambda tx: tx.queue(event(
                    self.deps.central_state, PLANNING_INVALIDATED,
                    {"pass_id": identity, "reason_code": "related_planning_inputs_changed"},
                )))
                identities = []
            await self.deps.event_bus.flush()
            return identities

        with llm_request_scope(str(runtime.run_id), "specialist_planning"), llm_request_guard(
            admission_guard
        ):
            strategy = StrategyDraft.model_validate(await self.client.ask(
                stage="attack_path_proposal", payload=payload, budget=budget,
                response_schema=_schema_with_completion_criterion_hint(
                    StrategyDraft.model_json_schema()
                ),
                validate_response=check_questions,
                before_call=admission_guard,
                system="Propose useful investigation questions from current tasks, results and "
                       "their follow-up questions. Reinvestigation may reuse the same evidence. "
                       "Do not create ordering dependencies or paths. Continue only with new "
                       "questions or reviewed_task_ids for currently claimable/running work. "
                       "Use replan with no questions to request one bounded follow-up judgment "
                       "when useful work may become clear; it spends the Run's finite allowance. "
                       "Use held with a precise resume_conditions entry when work needs an "
                       "external prerequisite. Use exhausted with resume_conditions when no "
                       "useful authorized investigation remains. goal_achieved is only a proposal "
                       "for the separate completion review. Do not prescribe tool order. "
                       "Prefer investigations named by operator_guidance. A registered task that "
                       "is no longer needed may be proposed under task_adjustments with "
                       "goal, criterion or result grounds. "
                       "Goal success requires a separate criterion/evidence review. "
                       "Tools and text do not grant execution permission.",
            ))
            if not strategy.questions:
                return await commit_pass(None)
            atomization = AtomizationDraft.model_validate(await self.client.ask(
                stage="task_atomization", budget=budget,
                payload={**payload, "questions": [q.model_dump(mode="json")
                                                  for q in strategy.questions]},
                response_schema=_schema_with_completion_criterion_hint(
                    AtomizationDraft.model_json_schema()
                ),
                validate_response=check_tasks,
                before_call=admission_guard,
                system="Register investigations by purpose, evidence, role and fixed conditions. "
                       "Preserve every question condition. No depends_on or tool-call queue. "
                       "Follow-ups cite source_task_id, source_result_id and source_question_id "
                       "from the original result item. Only registered ready roles "
                       "and known subjects.",
            ))
        return await commit_pass(atomization)


    @staticmethod
    def _rejection(error, stage):
        location = "$.questions" if stage == "attack_path_proposal" else "$.tasks"
        diagnostic = getattr(error, "diagnostic", None)
        if isinstance(diagnostic, DecisionDiagnostic):
            return DecisionValidationError(diagnostic.model_copy(update={
                "stage": stage,
                "location": location,
            }))
        summary = _REJECTION_SUMMARIES.get(
            error.code,
            "The proposal failed the captured subject, dependency or condition contract.",
        )
        return DecisionValidationError(DecisionDiagnostic(
            code=error.code, stage=stage, kind="validation", retryable=True,
            summary=summary,
            location=location,
        ))


#: Repair feedback sent back to the model on the next attempt (cognition.py's
#: DecisionDiagnostic.feedback()). Each message names the actual fix so a retry can
#: succeed instead of repeating the same rejected shape -- a generic restatement of
#: the contract gives the model nothing to act on (section 5: code decides, but a
#: validation failure still owes the proposer an actionable reason).
_REJECTION_SUMMARIES: dict[str, str] = {
    "specialist_invalid_proposal": "Return a judgment that matches the supplied schema and "
        "preserves the stated work and progress conditions.",
    "planner_reviewed_task_unknown": "reviewed_task_ids must name Tasks in this Run.",
    "planner_continue_requires_progress": "Continue requires proposed questions or a reviewed "
        "claimable/running Task; use replan for one bounded follow-up judgment, held for an "
        "external prerequisite, or exhausted when no useful work remains.",
    "planner_continue_without_claimable_work": "Continue requires a reviewed Task that remains "
        "claimable or running for an eligible role; otherwise choose a different disposition.",
    "specialist_duplicate_identity": "question_id/task_key values must be unique.",
    "specialist_blank_subject": "objective and every target_node_ids entry must be non-blank.",
    "specialist_blank_title": "title must be non-blank.",
    "specialist_unknown_target": "target_node_ids must only reference ids already listed in "
        "known_target_ids.",
    "specialist_unknown_principal": "principal_refs must only reference ids already listed in "
        "registered_principal_refs.",
    "specialist_duplicate_completion_condition": "completion_criteria entries must be distinct "
        "(same kind/description/hypothesis_id/principal_refs is a duplicate).",
    "specialist_unknown_completion_condition": "every completion_criteria.kind must be one of: "
        + ", ".join(sorted(ALLOWED_CRITERION_KINDS))
        + "; description must also be non-blank.",
    "specialist_condition_principal_outside_subjects": "a completion_criteria.principal_refs "
        "entry must also appear in the question/task's own principal_refs.",
    "specialist_unknown_hypothesis": "completion_criteria.hypothesis_id must reference an "
        "existing hypothesis id, or be omitted.",
    "specialist_contradictory_candidate_verdict": "a question/task cannot require both "
        "hypothesis_confirmed and hypothesis_refuted for the same hypothesis_id -- a candidate "
        "has one status at a time, so that AND can never be satisfied. A natural-language "
        "'if X then confirmed, else refuted' description does not turn completion_criteria "
        "into a branch; every entry is still required. Keep at most one verdict kind per "
        "candidate on this item.",
    "specialist_task_count_limit": "reduce the number of tasks to the stated max_tasks limit.",
    "specialist_question_coverage_mismatch": "every task's question_id must match exactly one "
        "proposed question, and every question needs at least one task.",
    "specialist_question_targets_changed": "a question's tasks must together cover exactly its "
        "own target_node_ids, no more and no fewer.",
    "specialist_question_principals_changed": "a question's tasks must together cover exactly "
        "its own principal_refs, no more and no fewer.",
    "specialist_question_completion_conditions_dropped": "every completion_criteria entry on a "
        "question must be retained by at least one of its tasks.",
    "specialist_role_not_executable": "role_id must be a registered, schedulable role that is "
        "currently available.",
    "planner_adjustment_duplicate_task": "each task_id may appear once in task_adjustments.",
    "planner_adjustment_unknown_task": "task_id must reference a task of this mission.",
    "planner_adjustment_stale_goal": "goal_revision must match the current mission revision.",
    "planner_adjustment_blank_reason": "reason must be non-blank.",
    "planner_adjustment_unknown_criterion": "criterion_refs must be current success criteria.",
    "planner_adjustment_blank_evidence_ref": "evidence_ids must be non-blank references.",
    "planner_adjustment_unknown_evidence": "evidence_ids must be known same-run evidence.",
    "planner_adjustment_bad_result_link": "source links must name a recorded result question.",
    "planner_adjustment_ungrounded": "name a goal criterion, evidence ref or result link.",
    "planner_adjustment_missing_required_flag": "set_required needs a required value.",
    "planner_adjustment_cancel_keeps_required": "cancel cannot keep required=true.",
    "planner_adjustment_stale_owner": "owner/version must match the holding worker.",
    "planner_adjustment_unowned_has_owner": "unowned tasks take no owner/version.",
}
