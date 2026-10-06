"""Serial Planner -> Atomizer procedure using the existing shared budget.

Only proposals are returned. Path registration, evidence review, delegation,
work leases, Control admission and execution remain with their existing owners.
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from redteam.orchestration.paths.models import PathStep, validate_path_structure
from redteam.orchestration.paths.writing import validate_written_criteria
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
    completion_criteria: list[dict[str, Any]] = Field(min_length=1)


class AttackPathProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)
    questions: list[InvestigationQuestion] = Field(min_length=1, max_length=8)


def validate_strategy(proposal: AttackPathProposal, context: dict[str, Any]) -> None:
    """No speculative subject/session references or cyclic question dependencies."""
    known = {
        str(item.get("node_id", item.get("id", ""))) if isinstance(item, dict) else str(item)
        for item in context.get("knowledge", {}).get("known_targets", [])
    } - {""}
    # This public context may be capped. Later engine registration checks fresh
    # internal material; do not invent refs to compensate for a capped view.
    from redteam.orchestration.engine import principal_bindings

    principals = set(principal_bindings(context.get("knowledge", {})))
    steps = []
    for question in proposal.questions:
        if set(question.target_node_ids) - known:
            raise ValueError("specialist_strategy_unknown_target")
        if set(question.principal_refs) - principals:
            raise ValueError("specialist_strategy_unknown_principal")
        validate_written_criteria({"completion_criteria": question.completion_criteria})
        steps.append(PathStep.model_validate({
            "step_id": question.question_id, "objective": question.objective,
            "depends_on": question.depends_on, "completion_criteria": question.completion_criteria,
        }))
    validate_path_structure(steps)


def validate_atomization(
    path: dict[str, Any], strategy: AttackPathProposal, origins: dict[str, str],
    *, max_tasks_per_question: int,
) -> None:
    """A decomposition cannot silently drop subjects, dependencies or questions."""
    steps = [PathStep.model_validate(item) for item in path.get("steps", [])]
    validate_path_structure(steps)
    questions = {item.question_id: item for item in strategy.questions}
    if set(origins) != {item.step_id for item in steps}:
        raise ValueError("specialist_atomization_origins_mismatch")
    if set(origins.values()) != set(questions):
        raise ValueError("specialist_atomization_question_coverage")
    by_question = {
        identity: [item for item in steps if origins[item.step_id] == identity]
        for identity in questions
    }
    for identity, tasks in by_question.items():
        if len(tasks) > max_tasks_per_question:
            raise ValueError("specialist_atomization_limit")
        question = questions[identity]
        if {ref for item in tasks for ref in item.target_node_ids} != set(question.target_node_ids):
            raise ValueError("specialist_atomization_subjects_changed")
        if {ref for item in tasks for ref in item.principal_refs} != set(question.principal_refs):
            raise ValueError("specialist_atomization_principals_changed")
        for item in tasks:
            if set(item.principal_refs) - set(question.principal_refs):
                raise ValueError("specialist_atomization_principals_changed")
            validate_written_criteria({"completion_criteria": [
                criterion.model_dump(mode="json") for criterion in item.completion_criteria
            ]})
            if not item.completion_criteria or not item.agent_id or not item.target_node_ids:
                raise ValueError("specialist_atomic_task_contract_missing")
            # Every task in a dependent question waits for all parent tasks.
            required = {
                parent.step_id for dependency in question.depends_on
                for parent in by_question[dependency]
            }
            if required - set(item.depends_on):
                raise ValueError("specialist_atomization_dependencies_dropped")


class SerialSpecialistPlanningProvider:
    def __init__(self, prompts: PromptSnapshot, *, max_tasks_per_question: int = 6) -> None:
        self.prompts = prompts
        self.max_tasks_per_question = max_tasks_per_question

    async def _infer(self, request, *, stage, schema, payload, contract, role_id=None):
        return await request.inference.infer(
            stage=stage, response_schema=schema,
            payload={**payload, "decision_role": self.prompts.binding(
                stage, role_id=role_id
            ).model_dump(mode="json")},
            system=self.prompts.compose(stage, contract, role_id=role_id),
        )

    async def propose(self, request: PlanningRequest) -> dict[str, Any]:
        if request.stage == "path_draft":
            strategy = AttackPathProposal.model_validate(await self._infer(
                request, stage="attack_path_proposal",
                schema=AttackPathProposal.model_json_schema(),
                payload=request.payload,
                contract="Return investigation questions, subjects, dependencies and criteria. "
                "Do not select executable tools or assert permission. Use only known refs.",
            ))
            validate_strategy(strategy, request.payload)
            schema: dict[str, Any] = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "path": deepcopy(request.response_schema),
                    "origins": {"type": "object", "additionalProperties": {"type": "string"}},
                }, "required": ["path", "origins"],
            }
            # Hoist definitions to keep existing PathPlan $refs resolvable.
            if "$defs" in schema["properties"]["path"]:
                schema["$defs"] = schema["properties"]["path"].pop("$defs")
            result = await self._infer(
                request, stage="task_atomization", schema=schema,
                payload={**request.payload, "strategy": strategy.model_dump(mode="json"),
                         "max_tasks_per_question": self.max_tasks_per_question},
                contract="Return path and origins (step_id -> question_id). Each atomic step "
                "has an executable registered role, known subjects, dependencies and criteria. "
                "Preserve every question/subject. Every task of a dependent question must "
                "depend on all tasks of its parent questions. Tools are chosen by the executor.",
            )
            path = result["path"]
            validate_atomization(path, strategy, result["origins"],
                                 max_tasks_per_question=self.max_tasks_per_question)
            path["planning_artifacts"] = {"specialist_decomposition": {
                "version": "1", "strategy": strategy.model_dump(mode="json"),
                "origins": result["origins"],
                "planner": self.prompts.binding("attack_path_proposal").model_dump(mode="json"),
                "atomizer": self.prompts.binding("task_atomization").model_dump(mode="json"),
            }}
            return path
        schema = deepcopy(request.response_schema)
        if request.stage == "next_action":
            # Planner delegates purposes; target methods are executor-owned.
            modes = schema["properties"]["mode"]["enum"]
            schema["properties"]["mode"]["enum"] = [
                mode for mode in modes if mode not in {"actions", "revise_path", "switch_path"}
            ]
        if request.stage == "path_review":
            # A replacement must go through Planner and Atomizer, not a reviewer.
            schema["properties"]["decision"]["enum"] = ["accept", "hold", "reject"]
        role_id = (
            request.payload.get("specialist", {}).get("agent_id")
            if request.stage == "worker_next_action" else None
        )
        result = await self._infer(
            request, stage=request.stage, schema=schema, payload=request.payload,
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
                    "Read evidence or hold if prerequisites are absent."
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
            }[request.stage], role_id=role_id,
        )
        if request.stage == "next_action" and (result.get("actions") or result.get("path")):
            raise ValueError("specialist_planner_cannot_execute_or_replace_path")
        if request.stage == "path_review" and result.get("path"):
            raise ValueError("specialist_reviewer_cannot_replace_path")
        return result
