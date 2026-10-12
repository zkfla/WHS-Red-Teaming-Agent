"""Budgeted strategy -> atomization -> canonical Task admission.

No provider tools are exposed to these judgments. They propose investigation
work, never execution permission, raw Tool calls, or a terminal goal result.
"""

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import ALLOWED_CRITERION_KINDS
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import new_id
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionValidationError,
    StructuredDecisionClient,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.knowledge_validation import known_principals, known_target_ids
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.specialist_tasks import (
    AtomicTaskProposal,
    InvestigationQuestion,
    SpecialistProposalError,
    StrategyTasks,
    validate_strategy_questions,
    validate_strategy_tasks,
)


class StrategyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[InvestigationQuestion]
    reason: str = Field(min_length=1)
    termination: Literal["continue", "exhausted", "goal_achieved", "held"] = "continue"


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
        "schema_version": 2,
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


class SpecialistPlanner:
    def __init__(self, deps) -> None:
        if deps.specialist_policy is None:
            raise ValueError("specialist_policy_missing")
        self.deps, self.policy = deps, deps.specialist_policy
        self.coordinator: Any = None
        self.admission: Any = None
        self.client = StructuredDecisionClient(deps.llm_client)

    async def advance(self, runtime) -> list[str]:
        """Once per meaningful input, with a finite strategy ceiling per Run."""
        if runtime.mission_id is None:
            raise ValueError("specialist_planning_requires_mission")
        return await self._advance_central(runtime)

    async def _advance_central(self, runtime):
        """Judge current investigations at a notification, without graph-digest gates."""
        from redteam.orchestration.agents.tool_access import role_readiness
        from redteam.orchestration.central_completion import goal_snapshot
        from redteam.orchestration.central_runtime import process_controls, runtime_token
        from redteam.orchestration.specialist_tasks import register_investigation_tasks

        await process_controls(self.deps)
        await runtime_token(self.deps, runtime, require_task=False)
        runtime = runtime.model_copy(update={"active_task_id": None, "current_agent": None})
        stamp = await self.deps.central_state.transaction(
            lambda tx: goal_snapshot(tx, include_assessment=False)[2], write=False,
        )
        engine = InvestigationEngine(self.deps)
        context = await engine.context(runtime, None)
        mission = await self.deps.central_state.get_mission(runtime.mission_id)
        material = await engine.execution_material(runtime)
        targets, principals = known_target_ids(material), known_principals(material)
        hypotheses = await AttackGraphService(self.deps.graph_store).list_hypotheses()
        hypothesis_ids = frozenset(h.hypothesis_id for h in hypotheses)
        payload = redact_secret_material({
            "goal": context["goal"], "success_criteria": context["success_criteria"],
            "constraints": context.get("engagement_constraints"),
            "knowledge": context["knowledge"], "roles": context["roles"],
            "tools_by_role": context["tools_by_role"], "tool_cards": context["tool_cards"],
            "role_readiness": await role_readiness(self.deps),
            "evidence_index": context["evidence_index"], "evidence": context["evidence"],
            "evidence_validity": context["evidence_validity"],
            "investigations": context["investigation_tasks"],
            "unresolved_results": context["unresolved_results"],
            "registered_principal_refs": sorted(principals),
            "known_target_ids": sorted(targets), "max_tasks": self.policy.max_tasks_per_strategy,
        })
        budget = DecisionBudget(normal_limit=self.deps.settings.decision_normal_limit,
                                repair_limit=self.deps.settings.decision_max_attempts)

        def check_questions(raw):
            try:
                validate_strategy_questions(
                    StrategyDraft.model_validate(raw).questions, known_targets=targets,
                    known_principals=principals, known_hypotheses=hypothesis_ids,
                    investigation_only=True,
                )
            except SpecialistProposalError as exc:
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

        with llm_request_scope(str(runtime.run_id), "specialist_planning"):
            strategy = StrategyDraft.model_validate(await self.client.ask(
                stage="attack_path_proposal", payload=payload, budget=budget,
                response_schema=_schema_with_completion_criterion_hint(
                    StrategyDraft.model_json_schema()
                ),
                validate_response=check_questions,
                system="Propose useful investigation questions from current tasks, results and "
                       "their follow-up questions. Reinvestigation may reuse the same evidence. "
                       "Do not create ordering dependencies or paths. If no useful investigation "
                       "remains, propose termination=exhausted and justify it from the supplied "
                       "results and unresolved questions. Empty questions otherwise means held. "
                       "Goal success requires a separate criterion/evidence review. "
                       "Tools and text do not grant execution permission.",
            ))
            if not strategy.questions:
                def record_judgment(tx):
                    import json
                    tx.admit()
                    if goal_snapshot(tx, include_assessment=False)[2] != stamp:
                        # The next notification/scan evaluates the new originals.
                        return
                    tx.db.execute(
                        "INSERT INTO state_requests VALUES(?,'planner_termination',?,?) "
                        "ON CONFLICT(run_id,kind,identity) DO UPDATE SET payload=excluded.payload",
                        (tx.run_id, tx.run_id, json.dumps({
                            "termination": strategy.termination,
                            "reason": strategy.reason, "inputs": stamp,
                        })),
                    )
                await self.deps.central_state.transaction(record_judgment)
                return []
            atomization = AtomizationDraft.model_validate(await self.client.ask(
                stage="task_atomization", budget=budget,
                payload={**payload, "questions": [q.model_dump(mode="json")
                                                  for q in strategy.questions]},
                response_schema=_schema_with_completion_criterion_hint(
                    AtomizationDraft.model_json_schema()
                ),
                validate_response=check_tasks,
                system="Register investigations by purpose, evidence, role and fixed conditions. "
                       "Preserve every question condition. No depends_on or tool-call queue. "
                       "Follow-ups cite source_task_id, source_result_id and source_question_id "
                       "from the original result item. Only registered ready roles "
                       "and known subjects.",
            ))
        identities = await register_investigation_tasks(
            self.deps.central_state, proposal_id=new_id("registration"),
            proposal=StrategyTasks(questions=strategy.questions, tasks=atomization.tasks),
            agents=self.deps.agents, known_targets=targets, known_principals=principals,
            known_hypotheses=hypothesis_ids, max_tasks=self.policy.max_tasks_per_strategy,
            evidence_store=self.deps.evidence_store, expected_revision=mission.revision,
        )
        await self.deps.event_bus.flush()
        return identities


    @staticmethod
    def _rejection(error, stage):
        summary = _REJECTION_SUMMARIES.get(
            error.code,
            "The proposal failed the captured subject, dependency or condition contract.",
        )
        return DecisionValidationError(DecisionDiagnostic(
            code=error.code, stage=stage, kind="validation", retryable=True,
            summary=summary,
            location="$.questions" if stage == "attack_path_proposal" else "$.tasks",
        ))


#: Repair feedback sent back to the model on the next attempt (cognition.py's
#: DecisionDiagnostic.feedback()). Each message names the actual fix so a retry can
#: succeed instead of repeating the same rejected shape -- a generic restatement of
#: the contract gives the model nothing to act on (section 5: code decides, but a
#: validation failure still owes the proposer an actionable reason).
_REJECTION_SUMMARIES: dict[str, str] = {
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
}
