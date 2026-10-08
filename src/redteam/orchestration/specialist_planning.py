"""Serial Planner -> Atomizer procedure using the existing shared budget.

Only proposals are returned. Path registration, evidence review, delegation,
work leases, Control admission and execution remain with their existing owners.
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.knowledge_validation import principal_bindings
from redteam.orchestration.paths.models import (
    CompletionCriterion,
    PathPlan,
    PathStep,
    PathValidationError,
    changed_steps_and_descendants,
    step_definition_equal,
    validate_path_structure,
)
from redteam.orchestration.paths.writing import (
    CriterionWritingError,
    normalize_path_proposals,
    validate_written_criteria,
    writing_schema,
)
from redteam.orchestration.planning import PlanningRequest
from redteam.orchestration.prompts import PromptSnapshot
from redteam.orchestration.result_claims import RESULT_REVIEW_INSTRUCTION


class InvestigationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_node_ids: list[str] = Field(min_length=1)
    principal_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    completion_criteria: list[CompletionCriterion] = Field(min_length=1)

    @field_validator("completion_criteria", mode="before")
    @classmethod
    def written_criteria(cls, value: Any) -> Any:
        validate_written_criteria({"completion_criteria": value})
        # Use the writing owner's existing semantic defaults, not the more
        # permissive storage model's unknown/empty defaults.
        return normalize_path_proposals({"steps": [{"completion_criteria": value}]})[
            "steps"
        ][0]["completion_criteria"]


class AttackPathProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)
    questions: list[InvestigationQuestion] = Field(min_length=1, max_length=8)


class SpecialistValidationError(ValueError):
    """Code-owned correction metadata, without rejected subjects or model text."""

    def __init__(
        self, code: str, *, location: str = "$.steps", rule: str, stage: str | None = None,
    ) -> None:
        super().__init__(code)
        self.diagnostic = DecisionDiagnostic(
            code=code,
            stage=stage or ("attack_path_proposal" if code.startswith("specialist_strategy_")
                            else "task_atomization"),
            kind="format", location=location, summary=rule, retryable=True,
        )


def validate_strategy(proposal: AttackPathProposal, context: dict[str, Any]) -> None:
    """No speculative subject/session references or cyclic question dependencies."""
    known = {
        str(item.get("node_id", item.get("id", ""))) if isinstance(item, dict) else str(item)
        for item in context.get("knowledge", {}).get("known_targets", [])
    } - {""}
    # This public context may be capped. Later engine registration checks fresh
    # internal material; do not invent refs to compensate for a capped view.
    principals = set(principal_bindings(context.get("knowledge", {})))
    steps = []
    for question in proposal.questions:
        if set(question.target_node_ids) - known:
            raise SpecialistValidationError(
                "specialist_strategy_unknown_target", location="$.questions.target_node_ids",
                rule="Use only subjects in the supplied known target catalog.",
            )
        if set(question.principal_refs) - principals:
            raise SpecialistValidationError(
                "specialist_strategy_unknown_principal", location="$.questions.principal_refs",
                rule="Use only registered principal references, or an anonymous investigation.",
            )
        validate_written_criteria({"completion_criteria": question.completion_criteria})
        steps.append(
            PathStep.model_validate(
                {
                    "step_id": question.question_id,
                    "objective": question.objective,
                    "depends_on": question.depends_on,
                    "completion_criteria": question.completion_criteria,
                }
            )
        )
    validate_path_structure(steps)


def validate_atomization(
    path: dict[str, Any],
    strategy: AttackPathProposal,
    origins: dict[str, str],
    *,
    max_tasks_per_question: int,
) -> None:
    """A decomposition cannot silently drop subjects, dependencies or questions."""
    steps = [PathStep.model_validate(item) for item in path.get("steps", [])]
    validate_path_structure(steps)
    questions = {item.question_id: item for item in strategy.questions}
    if set(origins) != {item.step_id for item in steps}:
        raise SpecialistValidationError("specialist_atomization_origins_mismatch",
            location="$.origins", rule="Provide exactly one question origin for every step.")
    if set(origins.values()) != set(questions):
        raise SpecialistValidationError("specialist_atomization_question_coverage",
            location="$.origins", rule="Cover all and only the proposed questions.")
    by_question = {
        identity: [item for item in steps if origins[item.step_id] == identity]
        for identity in questions
    }
    for identity, tasks in by_question.items():
        if len(tasks) > max_tasks_per_question:
            raise SpecialistValidationError("specialist_atomization_limit",
                rule="Respect the supplied maximum tasks per question.")
        question = questions[identity]
        if {ref for item in tasks for ref in item.target_node_ids} != set(question.target_node_ids):
            raise SpecialistValidationError("specialist_atomization_subjects_changed",
                location="$.steps.target_node_ids", rule="Preserve each question's subjects.")
        if {ref for item in tasks for ref in item.principal_refs} != set(question.principal_refs):
            raise SpecialistValidationError("specialist_atomization_principals_changed",
                location="$.steps.principal_refs", rule="Preserve each question's principals.")
        for item in tasks:
            if set(item.principal_refs) - set(question.principal_refs):
                raise SpecialistValidationError("specialist_atomization_principals_changed",
                    location="$.steps.principal_refs", rule="Use only the question's principals.")
            validate_written_criteria(
                {
                    "completion_criteria": [
                        criterion.model_dump(mode="json") for criterion in item.completion_criteria
                    ]
                }
            )
            if not item.completion_criteria or not item.agent_id or not item.target_node_ids:
                raise SpecialistValidationError("specialist_atomic_task_contract_missing",
                    rule="Every task needs criteria, an executable role and known subjects.")
            # Every task in a dependent question waits for all parent tasks.
            required = {
                parent.step_id
                for dependency in question.depends_on
                for parent in by_question[dependency]
            }
            if required - set(item.depends_on):
                raise SpecialistValidationError("specialist_atomization_dependencies_dropped",
                    location="$.steps.depends_on", rule="Retain all parent-question tasks.")


class SerialSpecialistPlanningProvider:
    def __init__(
        self,
        prompts: PromptSnapshot,
        *,
        max_tasks_per_question: int = 6,
        enable_replanning: bool = False,
    ) -> None:
        self.prompts = prompts
        self.max_tasks_per_question = max_tasks_per_question
        self.enable_replanning = enable_replanning

    async def _infer(
        self, request, *, stage, schema, payload, contract, role_id=None, validate_response=None,
    ):
        return await request.inference.infer(
            stage=stage,
            response_schema=schema,
            payload={
                **payload,
                "decision_role": self.prompts.binding(stage, role_id=role_id).model_dump(
                    mode="json"
                ),
            },
            system=self.prompts.compose(stage, contract, role_id=role_id),
            validate_response=validate_response,
        )

    async def _decompose(
        self,
        request: PlanningRequest,
        *,
        previous: dict[str, Any] | None = None,
        switch: bool = False,
    ) -> dict[str, Any]:
        def check_strategy(value):
            try:
                validate_strategy(AttackPathProposal.model_validate(value), request.payload)
            except CriterionWritingError as exc:
                raise SpecialistValidationError(
                    exc.code, stage="attack_path_proposal", location=exc.location,
                    rule=exc.description,
                ) from exc
            except PathValidationError as exc:
                raise SpecialistValidationError("specialist_strategy_dependencies_invalid",
                    location="$.questions.depends_on",
                    rule="Use a question DAG with unique IDs and existing dependencies.") from exc

        strategy = AttackPathProposal.model_validate(
            await self._infer(
                request,
                stage="attack_path_proposal",
                schema=writing_schema(AttackPathProposal),
                payload=request.payload,
                contract="Return investigation questions, subjects, dependencies and criteria. "
                "Do not select executable tools or assert permission. Use only known refs. "
                "Questions about a discovered endpoint/object must name its exact known ID, "
                "not a parent host. No subject ID or permission may be invented.",
                validate_response=check_strategy,
            )
        )
        validate_strategy(strategy, request.payload)
        path_schema = deepcopy(request.response_schema)
        if request.stage != "path_draft":
            path_schema = deepcopy(request.response_schema["$defs"]["PathPlan"])
            path_schema["$defs"] = deepcopy(request.response_schema["$defs"])
        schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": path_schema,
                "origins": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["path", "origins"],
        }
        # Hoist definitions to keep existing PathPlan $refs resolvable.
        if "$defs" in schema["properties"]["path"]:
            schema["$defs"] = schema["properties"]["path"].pop("$defs")

        def check_atomization(value):
            try:
                validate_written_criteria(value["path"])
                normalized = normalize_path_proposals(value["path"])
                validate_atomization(
                    normalized, strategy, value["origins"],
                    max_tasks_per_question=self.max_tasks_per_question,
                )
                if previous is not None and not switch:
                    validate_revision(normalized, strategy, value["origins"], previous)
                elif previous is not None:
                    artifact = PathPlan.model_validate(previous).planning_artifacts.get(
                        "specialist_decomposition", {}
                    )
                    if artifact.get("version") != "1":
                        raise SpecialistValidationError("specialist_revision_provenance_missing",
                            rule="Require stored specialist decomposition provenance.")
                    if AttackPathProposal.model_validate(artifact["strategy"]).questions == (
                        strategy.questions
                    ):
                        raise SpecialistValidationError("specialist_switch_has_no_question_change",
                            rule="Switch only when investigation questions have changed.")
            except CriterionWritingError as exc:
                raise SpecialistValidationError(
                    exc.code, location=exc.location, rule=exc.description,
                ) from exc
            except PathValidationError as exc:
                raise SpecialistValidationError("specialist_atomization_dependencies_invalid",
                    location="$.steps.depends_on",
                    rule="Use unique step IDs and acyclic existing dependencies.") from exc

        result = await self._infer(
            request,
            stage="task_atomization",
            schema=schema,
            payload={
                **request.payload,
                "strategy": strategy.model_dump(mode="json"),
                "max_tasks_per_question": self.max_tasks_per_question,
                "preserve_unaffected_steps": previous if not switch else None,
            },
            contract="Return path and origins (step_id -> question_id). Each atomic step "
            "has an executable registered role, known subjects, dependencies and criteria. "
            "Preserve every question/subject. Every task of a dependent question must "
            "depend on all tasks of its parent questions. Tools are chosen by the executor. "
            "Atomic execution subjects must match the question's exact subjects; a new "
            "endpoint/object needs a revised question, not implicit host equivalence. "
            "For revisions retain IDs and definitions of unaffected questions and steps. "
            "Existing completion progress and evidence are runtime-owned; never forge them.",
            validate_response=check_atomization,
        )
        path = normalize_path_proposals(result["path"])
        path["planning_artifacts"] = {
            "specialist_decomposition": {
                "version": "1",
                "strategy": strategy.model_dump(mode="json"),
                "origins": result["origins"],
                "planner": self.prompts.binding("attack_path_proposal").model_dump(mode="json"),
                "atomizer": self.prompts.binding("task_atomization").model_dump(mode="json"),
            }
        }
        return path

    async def propose(self, request: PlanningRequest) -> dict[str, Any]:
        if request.stage == "path_draft":
            return await self._decompose(request)
        schema = deepcopy(request.response_schema)
        if request.stage == "next_action":
            # Planner delegates purposes; target methods are executor-owned.
            modes = schema["properties"]["mode"]["enum"]
            schema["properties"]["mode"]["enum"] = [
                mode
                for mode in modes
                if mode
                not in (
                    {"actions"}
                    if self.enable_replanning
                    else {"actions", "revise_path", "switch_path"}
                )
            ]
            # Planner decides *whether* to revise; only the two-stage procedure
            # below may produce the replacement. No direct path-writing shortcut.
            schema["properties"]["path"] = {"type": "null"}
        if request.stage == "path_review":
            # A replacement must go through Planner and Atomizer, not a reviewer.
            schema["properties"]["decision"]["enum"] = ["accept", "hold", "reject"]
        role_id = (
            request.payload.get("specialist", {}).get("agent_id")
            if request.stage == "worker_next_action"
            else None
        )
        result = await self._infer(
            request,
            stage=request.stage,
            schema=schema,
            payload=request.payload,
            contract={
                "next_action": (
                    "Route accepted active_path purposes with mode delegate to an executable role. "
                    "Do not choose target tools yourself. Use only current pending/running steps "
                    "and stored dependencies. Completion is a proposal, never automatic success. "
                    "For complete_step provide path_step_id and "
                    "completion outcome/evidence_ids/reason. "
                    "Use complete_task to settle the current mission task before moving "
                    "to another required task; complete_goal settles the original mission "
                    "only after required tasks have supported results. "
                    "Read evidence or hold if prerequisites are absent. "
                    "When revision is enabled, use revise_path/switch_path with path=null "
                    "and a bounded change reason. Planner and Atomizer will produce the path. "
                    "Do not revise on duplicate evidence or unchanged conditions."
                ),
                "path_review": (
                    "Review the strategy, atomic question coverage, subjects, dependencies, "
                    "criteria and current readiness. Accept, hold or reject. Do not rewrite a path."
                ),
                "worker_next_action": (
                    "Choose methods within this assigned purpose and live role catalog. "
                    "Return propose_actions, read_evidence, tool_calls, hold or finish per schema. "
                    "Preserve failure and partial results. Finish never settles the whole goal. "
                    "Use existing evidence_read_contract for reads; known references only."
                    " Select the assigned step's exact target_node_ids. If a new subject is "
                    "needed, hold with the known reference and request path revision; do not "
                    "execute it under a parent-host step or rewrite source evidence."
                ),
                "completion_review": (
                    "Review only the supplied step's objective and criteria against original "
                    "evidence and context. Other unfinished steps, a running mission task or "
                    "an unmet goal do not reject this step unless its own criteria require them. "
                    "Accepting a step does not complete its task or mission. Reject unsupported "
                    "success; failures or mock evidence cannot support vulnerability success."
                ),
                "task_completion_review": (
                    "Judge the delegated task purpose against the related original results. "
                    "Do not require unrelated steps or the entire mission to be completed. "
                    "A failed result can support a bounded investigation claim, never a "
                    "claim that a vulnerability was successfully verified. "
                    + RESULT_REVIEW_INSTRUCTION
                ),
                "goal_completion_review": (
                    "Review original goal conditions against verified task evidence and unresolved "
                    "gaps. Cover each original criterion exactly, with supported source claims. "
                    "Proven origin IDs without shown contents are references, not read evidence. "
                    "Tool success and completed tasks do not by themselves prove the goal. "
                    + RESULT_REVIEW_INSTRUCTION
                ),
            }[request.stage],
            role_id=role_id,
        )
        if request.stage == "next_action" and (result.get("actions") or result.get("path")):
            raise SpecialistValidationError("specialist_planner_cannot_execute_or_replace_path",
                stage="next_action", location="$.path",
                rule="Delegate execution; replacement paths require Planner and Atomizer.")
        if request.stage == "next_action" and result.get("mode") in {"revise_path", "switch_path"}:
            if not self.enable_replanning:
                raise SpecialistValidationError("specialist_replanning_disabled",
                    stage="next_action", location="$.mode",
                    rule="The selected profile does not enable path replacement.")
            previous = request.payload.get("active_path")
            if not isinstance(previous, dict):
                raise SpecialistValidationError("specialist_revision_requires_active_path",
                    stage="next_action", location="$.path",
                    rule="Path replacement requires an active path in the supplied context.")
            revision_request = PlanningRequest(
                stage=request.stage,
                payload={
                    **request.payload,
                    "revision_request": {
                        "mode": result["mode"],
                        "reason": result["reason"],
                    },
                },
                response_schema=request.response_schema,
                inference=request.inference,
            )
            result["path"] = await self._decompose(
                revision_request,
                previous=previous,
                switch=result["mode"] == "switch_path",
            )
        if request.stage == "path_review" and result.get("path"):
            raise SpecialistValidationError("specialist_reviewer_cannot_replace_path",
                stage="path_review", location="$.path",
                rule="Reviewers accept, hold or reject; they cannot write replacement paths.")
        return result


def validate_revision(
    path: dict[str, Any],
    strategy: AttackPathProposal,
    origins: dict[str, str],
    previous: dict[str, Any],
) -> None:
    """Preserve independent work; a changed question invalidates its descendants.

    This checks proposals, not progress or execution permission. The existing
    PathStore owns carry-forward and CAS after the engine's semantic review.
    """
    older = PathPlan.model_validate(previous)
    artifact = older.planning_artifacts.get("specialist_decomposition", {})
    if artifact.get("version") != "1":
        raise SpecialistValidationError("specialist_revision_provenance_missing",
            rule="Revise only a path with stored specialist decomposition provenance.")
    old_strategy = AttackPathProposal.model_validate(artifact["strategy"])
    old_questions = {q.question_id: q for q in old_strategy.questions}
    new_questions = {q.question_id: q for q in strategy.questions}
    changed = set(old_questions) ^ set(new_questions)
    changed.update(
        identity
        for identity in old_questions.keys() & new_questions.keys()
        if old_questions[identity] != new_questions[identity]
    )
    if not changed:
        raise SpecialistValidationError("specialist_revision_has_no_question_change",
            rule="Revise only when investigation questions have changed.")
    old_origins = artifact.get("origins", {})
    atomized = list(map(PathStep.model_validate, path["steps"]))
    # A changed question must be reflected in its atomic contract; changing
    # prose only cannot reset or carry forward completed execution silently.
    for identity in changed & old_questions.keys() & new_questions.keys():
        old_tasks = {s.step_id: s for s in older.steps if old_origins.get(s.step_id) == identity}
        new_tasks = {s.step_id: s for s in atomized if origins[s.step_id] == identity}
        if old_tasks.keys() == new_tasks.keys() and all(
            step_definition_equal(task, new_tasks[key]) for key, task in old_tasks.items()
        ):
            raise SpecialistValidationError("specialist_revision_question_not_reflected_in_tasks",
                rule="Reflect changed questions in their atomic task contracts.")
    old_dag = [
        PathStep(step_id=q.question_id, objective=q.objective, depends_on=q.depends_on)
        for q in old_strategy.questions
    ]
    new_dag = [
        PathStep(step_id=q.question_id, objective=q.objective, depends_on=q.depends_on)
        for q in strategy.questions
    ]
    affected = set(changed_steps_and_descendants(old_dag, new_dag, sorted(changed)))
    old_steps = {s.step_id: s for s in older.steps if old_origins.get(s.step_id) not in affected}
    new_steps = {s.step_id: s for s in atomized if origins[s.step_id] not in affected}
    if old_steps.keys() != new_steps.keys() or any(
        not step_definition_equal(s, new_steps[identity])
        or s.agent_id != new_steps[identity].agent_id
        or old_origins.get(identity) != origins[identity]
        for identity, s in old_steps.items()
    ):
        raise SpecialistValidationError("specialist_revision_changed_unaffected_work",
            rule="Preserve IDs, definitions, roles and origins of unaffected tasks.")
