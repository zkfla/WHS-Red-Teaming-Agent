"""Budgeted strategy -> atomization -> canonical Task admission.

No provider tools are exposed to these judgments. They propose investigation
work, never execution permission, raw Tool calls, or a terminal goal result.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import new_id
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    DecisionValidationError,
    StructuredDecisionClient,
)
from redteam.orchestration.coordination import (
    ChangeSet,
    StateConflict,
    content_hash,
    coordinator_for,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.knowledge_validation import known_principals, known_target_ids
from redteam.orchestration.metered_llm import llm_request_scope
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.specialist_tasks import (
    AtomicTaskProposal,
    InvestigationQuestion,
    SpecialistProposalError,
    StrategyTasks,
    register_strategy_tasks,
    validate_strategy_questions,
    validate_strategy_tasks,
)
from redteam.orchestration.task_activation import TaskActivation


class StrategyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[InvestigationQuestion]
    reason: str = Field(min_length=1)


class AtomizationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[AtomicTaskProposal] = Field(min_length=1)


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
        self.coordinator = deps.coordinator or coordinator_for(
            deps.task_graph_store, deps.path_store,
        )
        self.admission = PlanningAdmission(self.coordinator)
        self.client = StructuredDecisionClient(deps.llm_client)

    async def advance(self, runtime) -> list[str]:
        """Once per meaningful input, with a finite strategy ceiling per Run."""
        if runtime.mission_id is None:
            raise ValueError("specialist_planning_requires_mission")
        from redteam.orchestration.evidence_validity import ValidityProjection, refresh_validity

        await refresh_validity(self.deps, runtime)
        await self.admission.bind_inputs(str(runtime.run_id), specialist_planning_inputs(self.deps))
        # Capture BEFORE store reads/model I/O. The graph writer fence and
        # Task read set are checked in the final adoption transaction.
        source = await TaskActivation(self.coordinator, self.deps.agents).source_task_id(runtime)
        runtime = runtime.model_copy(update={"active_task_id": source})
        task_ids = [str(source)] if source else []
        stamp = await self.admission.capture(
            str(runtime.run_id), str(runtime.mission_id), task_ids=task_ids,
            reference_keys=("graph", "evidence"),
        )
        if stamp.strategy_revision >= self.policy.max_strategy_revisions:
            return []
        engine = InvestigationEngine(self.deps)
        # A planning judgment sees eligible execution roles, while the next
        # native worker turn keeps only its own role's tool view.
        context = await engine.context(runtime.model_copy(update={"current_agent": None}), None)
        # Refresh is event bookkeeping, not this judgment's input snapshot.
        # Bind expiry to the fresh context actually sent to the model, inside
        # the already captured reference read set. Changes after capture fail
        # the final CAS; already-stale context may propose revalidation.
        validity = [ValidityProjection.model_validate(item)
                    for item in context["evidence_validity"]]
        stamp = replace(stamp, valid_until=min(
            (item.valid_until for item in validity
             if item.status == "valid" and item.valid_until is not None), default=None,
        ))
        material = await engine.execution_material(runtime)
        targets, principals = known_target_ids(material), known_principals(material)
        hypotheses = await AttackGraphService(self.deps.graph_store).list_hypotheses()
        hypothesis_ids = frozenset(h.hypothesis_id for h in hypotheses)
        from redteam.orchestration.agents.tool_access import role_readiness

        payload = redact_secret_material({
            "goal": context["goal"], "success_criteria": context["success_criteria"],
            "constraints": context.get("engagement_constraints"),
            "knowledge": context["knowledge"], "roles": context["roles"],
            "tools_by_role": context["tools_by_role"], "tool_cards": context["tool_cards"],
            "role_readiness": await role_readiness(self.deps),
            "evidence_index": context["evidence_index"], "evidence": context["evidence"],
            "evidence_validity": context["evidence_validity"],
            "related_task": context.get("active_task"),
            "unresolved_results": context["unresolved_results"],
            "registered_principal_refs": sorted(principals),
            "known_target_ids": sorted(targets),
            "strategy_revision": stamp.strategy_revision,
            "max_tasks": self.policy.max_tasks_per_strategy,
        })
        digest = content_hash({key: value for key, value in payload.items()
                               if key != "strategy_revision"})
        attempt_id = new_id("strategy")

        async def reserve(tx: ChangeSet) -> bool:
            tx.admit(str(runtime.run_id), str(runtime.mission_id))
            key = ("specialist_planning_attempt", f"{runtime.run_id}:{digest}")
            if key in tx.records:
                return False
            tx.records[key] = {"attempt_id": attempt_id, "state": "reserved"}
            return True

        if not await self.coordinator.change(reserve):
            return []
        budget = DecisionBudget(
            normal_limit=self.deps.settings.decision_normal_limit,
            repair_limit=self.deps.settings.decision_max_attempts,
        )
        def check_questions(raw):
            try:
                validate_strategy_questions(
                    StrategyDraft.model_validate(raw).questions, known_targets=targets,
                    known_principals=principals, known_hypotheses=hypothesis_ids,
                )
            except SpecialistProposalError as exc:
                raise self._rejection(exc, "attack_path_proposal") from exc

        try:
            return await self._decide_and_admit(
                runtime, stamp, digest, attempt_id, payload, budget, targets, principals,
                hypothesis_ids, check_questions,
            )
        except DecisionFailure as exc:
            await self._record(runtime, digest, "failed", exc.diagnostic.feedback(),
                               diagnostic=exc.diagnostic)
            raise
        except StateConflict:
            await self._record(runtime, digest, "stale", "Current planning inputs changed.")
            raise
        except asyncio.CancelledError:
            await self._record(runtime, digest, "interrupted",
                               "Planning was interrupted; dispatched requests stay charged.")
            raise

    async def _decide_and_admit(
        self, runtime, stamp, digest, attempt_id, payload, budget, targets, principals,
        hypothesis_ids, check_questions,
    ):
        # Bind the accepted question list into the pure atomization validator.
        def validate_tasks(raw):
            try:
                validate_strategy_tasks(
                    StrategyTasks(questions=strategy.questions,
                                  tasks=AtomizationDraft.model_validate(raw).tasks),
                    agents=self.deps.agents, known_targets=targets, known_principals=principals,
                    known_hypotheses=hypothesis_ids, max_tasks=self.policy.max_tasks_per_strategy,
                )
            except SpecialistProposalError as exc:
                raise self._rejection(exc, "task_atomization") from exc

        with llm_request_scope(str(runtime.run_id), "specialist_planning"):
            strategy = StrategyDraft.model_validate(await self.client.ask(
                stage="attack_path_proposal", payload=payload,
                response_schema=StrategyDraft.model_json_schema(), budget=budget,
                system="Propose investigation questions, not tool calls. Only known subjects. "
                       "Preserve explicit evidence conditions. Empty questions means held, "
                       "never success. Do not repeat completed/held work without new grounds.",
                validate_response=check_questions,
            ))
            if not strategy.questions:
                await self._record(runtime, digest, "held", strategy.reason)
                return []
            atomization = AtomizationDraft.model_validate(await self.client.ask(
                stage="task_atomization",
                payload={**payload, "questions": [q.model_dump(mode="json")
                                                  for q in strategy.questions]},
                response_schema=AtomizationDraft.model_json_schema(), budget=budget,
                system="Decompose the supplied questions into atomic Tasks. Preserve every "
                       "original completion criterion and dependency; do not invent principals. "
                       "Assign only registered executable roles with available tools.",
                validate_response=validate_tasks,
            ))
        task_ids = await register_strategy_tasks(
            self.admission, stamp, proposal_id=attempt_id,
            proposal=StrategyTasks(questions=strategy.questions, tasks=atomization.tasks),
            agents=self.deps.agents, known_targets=targets, known_principals=principals,
            known_hypotheses=hypothesis_ids,
            max_tasks=self.policy.max_tasks_per_strategy, now=datetime.now(timezone.utc),
        )
        await self._record(runtime, digest, "admitted", strategy.reason)
        await deliver_pending(
            self.coordinator, run_id=str(runtime.run_id),
            engagement_id=str(runtime.engagement_id), event_bus=self.deps.event_bus,
        )
        return task_ids

    @staticmethod
    def _rejection(error, stage):
        return DecisionValidationError(DecisionDiagnostic(
            code=error.code, stage=stage, kind="validation", retryable=True,
            summary="The proposal failed the captured subject, dependency or condition contract.",
            location="$.questions" if stage == "attack_path_proposal" else "$.tasks",
        ))

    async def _record(self, runtime, digest, state, reason, *, diagnostic=None):
        async def record(tx: ChangeSet):
            row = tx.records[("specialist_planning_attempt", f"{runtime.run_id}:{digest}")]
            row.update(state=state, reason=safe_feedback(reason))
            if diagnostic is not None:
                row["diagnostic"] = diagnostic.model_dump(mode="json")
        await self.coordinator.change(record)
