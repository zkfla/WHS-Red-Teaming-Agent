from __future__ import annotations

from dataclasses import dataclass, field

from cicd_redteam_agent.domain.graph_frontier import (
    DEFAULT_MAX_EXPANSIONS,
    DEFAULT_MAX_FRONTIERS,
    FrontierQuery,
    build_attack_frontiers,
)
from cicd_redteam_agent.domain.investigation import (
    InvestigationTaskStatus,
    RedTeamSession,
)
from cicd_redteam_agent.domain.investigation_policy import (
    InvestigationPolicy,
    InvestigationRuntimeState,
    InvestigationTransition,
    decide_investigation_policy,
    select_frontier_portfolio,
)
from cicd_redteam_agent.domain.models import AttackerProfile
from cicd_redteam_agent.framework.capabilities import CapabilityContext
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.environment_observer import EnvironmentObserver
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.investigation_runtime import (
    plan_next_task,
    run_observation_once,
    synchronize_analysis_state,
)
from cicd_redteam_agent.framework.runtime_support import CheckpointWriter, EventJournal
from cicd_redteam_agent.framework.working_memory import reconcile_working_memory
from cicd_redteam_agent.security.redaction import redact

MAX_EXTERNAL_OBSERVATIONS_BEFORE_MODEL = 3


@dataclass
class CICDUseCase:
    agent: CICDChatAgent
    context: CapabilityContext
    limits: object
    checkpoint_writer: CheckpointWriter | None = None
    event_journal: EventJournal | None = None
    investigation_session: RedTeamSession | None = None
    environment_observer: EnvironmentObserver | None = None
    attacker_profile: AttackerProfile | None = None
    warnings: list[str] = field(default_factory=list)
    _checkpoint_failed: bool = field(default=False, init=False)
    _planned_observation_task_id: str | None = field(default=None, init=False)
    _resolver_metric_last: dict[str, int | None] = field(default_factory=dict, init=False)

    def _sync_resolver_metrics(self) -> None:
        if not isinstance(self.environment_observer, ResolverRegistry):
            return
        runtime_metrics = getattr(self.agent, "runtime_metrics", None)
        if runtime_metrics is None:
            return
        snapshot = self.environment_observer.runtime_metrics_snapshot()
        counters = {
            "network_requests": "external_network_requests",
            "cache_hits": "external_cache_hits",
            "not_modified_hits": "external_not_modified_hits",
            "rate_limit_errors": "external_rate_limit_errors",
            "retries": "external_retries",
        }
        for source_name, target_name in counters.items():
            current = snapshot.get(source_name)
            previous = self._resolver_metric_last.get(source_name, 0)
            if isinstance(current, int) and isinstance(previous, int):
                setattr(
                    runtime_metrics,
                    target_name,
                    getattr(runtime_metrics, target_name) + max(0, current - previous),
                )
        remaining = snapshot.get("rate_limit_remaining")
        if isinstance(remaining, int):
            runtime_metrics.external_rate_limit_remaining = remaining
        self._resolver_metric_last = snapshot

    async def _status(self, message: str) -> None:
        logger = getattr(self.agent, "log", None)
        status_message = getattr(logger, "status_message", None)
        if status_message is not None:
            await status_message(message)

    def _checkpoint(self, reason: str) -> None:
        if self.checkpoint_writer is None or self._checkpoint_failed:
            return
        try:
            self.checkpoint_writer.write(
                rounds=self.limits.rounds,
                tokens=self.limits.tokens,
                cost=self.limits.cost,
                reason=reason,
                active_duration_seconds=(
                    self.limits.duration.total_seconds() if self.limits.duration else 0.0
                ),
            )
        except OSError as exc:
            self._checkpoint_failed = True
            self.warnings.append(f"Checkpoint disabled after {type(exc).__name__}: {exc}")

    async def _run_pre_model_observations(self) -> int:
        if (
            self.investigation_session is None
            or self.environment_observer is None
            or self.attacker_profile is None
        ):
            return 0

        session = self.investigation_session
        session.analysis_state = self.context.state.model_copy(deep=True)
        if self._planned_observation_task_id is not None:
            task = session.investigation_state.tasks.get(self._planned_observation_task_id)
        else:
            task = plan_next_task(session)
        self._planned_observation_task_id = None
        if task is None:
            return 0
        completed = 0
        if self.event_journal is not None:
            self.event_journal.append(
                "INVESTIGATION_TASK_STARTED",
                round=self.limits.rounds,
                payload={"task_id": task.task_id, "kind": task.kind.value},
            )
        await self._status(f"Observing environment for {task.kind.value}.")
        try:
            observation = await run_observation_once(
                session,
                self.environment_observer,
                boundary=self.context.boundary,
                profile=self.attacker_profile,
                task=task,
            )
        except Exception as exc:
            current = session.investigation_state.tasks.get(task.task_id)
            if current is not None:
                current.status = InvestigationTaskStatus.BLOCKED
            detail = f"{type(exc).__name__}: {redact(str(exc))}"
            self.warnings.append(
                f"Read-only environment observation failed; preserving UNKNOWN: {detail}"
            )
            if self.event_journal is not None:
                self.event_journal.append(
                    "INVESTIGATION_TASK_BLOCKED",
                    round=self.limits.rounds,
                    payload={"task_id": task.task_id, "error_type": type(exc).__name__},
                )
            self._sync_resolver_metrics()
            return 0
        if observation is None:
            return 0

        synchronize_analysis_state(self.context.state, session.analysis_state)
        self.context.frontier_cache.clear()
        completed = 1
        runtime_metrics = getattr(self.agent, "runtime_metrics", None)
        if runtime_metrics is not None:
            runtime_metrics.investigation_tasks_completed += 1
            runtime_metrics.observations_committed += 1
            runtime_metrics.semantic_refreshes += 1
        self._sync_resolver_metrics()
        if self.event_journal is not None:
            self.event_journal.append(
                "OBSERVATION_COMMITTED",
                round=self.limits.rounds,
                payload={
                    "task_id": task.task_id,
                    "observation_id": observation.observation_id,
                    "kind": observation.kind.value,
                    "knowledge_revision": session.knowledge_state.revision,
                },
            )
        self._checkpoint("observation_committed")
        return completed

    def _runtime_state(self, no_progress_rounds: int) -> InvestigationRuntimeState:
        limit_reached = bool(self.limits.reached())
        tokens_remaining = getattr(self.limits, "tokens_remaining", None)
        remaining = tokens_remaining() if callable(tokens_remaining) else None
        return InvestigationRuntimeState(
            limit_reached=limit_reached,
            limit_reason=(getattr(self.limits, "reason", None) if limit_reached else None),
            tokens_remaining=remaining,
            predicted_next_round_tokens=int(getattr(self.agent, "last_round_tokens", 0)),
            repeated_calls=max(self.context.state.repeated_calls.values(), default=0),
            no_progress_rounds=no_progress_rounds,
        )

    def _refresh_investigation_policy(
        self,
        *,
        runtime: InvestigationRuntimeState | None = None,
    ) -> InvestigationPolicy:
        goal = self.context.objective_goal
        sink_hints = goal.sink_hints if goal else ()
        if "cloud_identity" in sink_hints:
            sink_hints = tuple(item for item in sink_hints if item != "oidc_token")
        query = FrontierQuery(
            source_hints=goal.source_hints if goal else (),
            mechanism_hints=goal.mechanism_hints if goal else (),
            sink_hints=sink_hints,
            max_frontiers=DEFAULT_MAX_FRONTIERS,
            max_expansions=DEFAULT_MAX_EXPANSIONS,
        )
        result = build_attack_frontiers(self.context.state, query)
        runtime_metrics = getattr(self.agent, "runtime_metrics", None)
        if runtime_metrics is not None:
            runtime_metrics.frontier_searches += 1
        portfolio = select_frontier_portfolio(result, self.context.state)
        reconciliation = reconcile_working_memory(self.context.state, result)
        planned_task = None
        if (
            self.investigation_session is not None
            and self.environment_observer is not None
            and self.attacker_profile is not None
        ):
            self.investigation_session.analysis_state = self.context.state.model_copy(deep=True)
            registry = (
                self.environment_observer
                if isinstance(self.environment_observer, ResolverRegistry)
                else None
            )
            planned_task = plan_next_task(
                self.investigation_session,
                portfolio=portfolio if registry is not None else (),
                resolver_registry=registry,
            )
        self._planned_observation_task_id = (
            planned_task.task_id if planned_task is not None else None
        )
        policy = decide_investigation_policy(
            result,
            self.context.state,
            observation_ready=planned_task is not None,
            runtime=runtime,
            selected_portfolio=portfolio,
        )
        self.context.investigation_policy = policy
        if hasattr(self.agent, "investigation_policy"):
            self.agent.investigation_policy = policy.as_dict()
        if reconciliation.changed:
            self.context.frontier_cache.clear()
            if self.event_journal is not None:
                self.event_journal.append(
                    "HYPOTHESES_RECONCILED",
                    round=self.limits.rounds,
                    payload={
                        "working_memory_revision": reconciliation.revision,
                        "ready": len(reconciliation.ready_hypothesis_ids),
                        "discarded": len(reconciliation.discarded_hypothesis_ids),
                    },
                )
            self._checkpoint("hypotheses_reconciled")
        if self.event_journal is not None:
            self.event_journal.append(
                "INVESTIGATION_POLICY_SELECTED",
                round=self.limits.rounds,
                payload={
                    "transition": policy.transition.value,
                    "portfolio_size": len(policy.portfolio),
                    "empty_submission_allowed": policy.empty_submission_allowed,
                    "stop_reason": policy.stop_reason,
                },
            )
        return policy

    async def run(self) -> str:
        self.limits.start()
        await self.agent.before_run(self.limits)
        runtime_metrics = getattr(self.agent, "runtime_metrics", None)
        no_progress = runtime_metrics.no_progress_rounds if runtime_metrics is not None else 0
        known_resources = len(self.context.state.inspected_resources)
        known_call_signatures = len(self.context.state.repeated_calls)
        known_memory_revision = self.context.state.working_memory.revision
        known_knowledge_revision = (
            self.investigation_session.knowledge_state.revision
            if self.investigation_session is not None
            else 0
        )
        observations_before_model = 0
        while not self.context.submitted:
            runtime_state = self._runtime_state(no_progress)
            policy = self._refresh_investigation_policy(runtime=runtime_state)
            if policy.transition is InvestigationTransition.STOP_PARTIAL:
                reason = policy.stop_reason or "stop_partial"
                if reason == "insufficient_token_reserve":
                    self.warnings.append(
                        "Agent stopped before starting a model round that was likely to "
                        "exceed the remaining token budget."
                    )
                elif reason == "no_progress":
                    self.warnings.append("Agent stopped after repeated calls or no progress.")
                self._checkpoint(reason)
                return reason
            if (
                policy.transition is InvestigationTransition.OBSERVE
                and observations_before_model < MAX_EXTERNAL_OBSERVATIONS_BEFORE_MODEL
            ):
                observations_before_model += await self._run_pre_model_observations()
                continue
            round_number = self.limits.rounds + 1
            self.agent.active_round = round_number
            if self.event_journal is not None:
                self.event_journal.append(
                    "ROUND_STARTED",
                    round=round_number,
                    payload={"working_memory_revision": known_memory_revision},
                )
            await self._status(
                f"Agent round {self.limits.rounds + 1}/"
                f"{getattr(self.limits, 'max_rounds', 'unknown')}; "
                f"tokens={self.limits.tokens}; cost={self.limits.cost:.6f}."
            )
            state_before_round = self.context.state.model_dump(mode="json", by_alias=True)
            submitted_before_round = self.context.submitted
            prompt_history = getattr(self.agent, "_prompt_history", None)
            prompt_history_before_round = (
                list(prompt_history) if prompt_history is not None else None
            )
            try:
                await self.agent.perform_round(self.limits)
            except Exception as exc:
                restored = type(self.context.state).model_validate(state_before_round)
                for field_name in type(self.context.state).model_fields:
                    setattr(
                        self.context.state,
                        field_name,
                        getattr(restored, field_name),
                    )
                self.context.submitted = submitted_before_round
                if prompt_history_before_round is not None:
                    self.agent._prompt_history[:] = prompt_history_before_round
                if self.event_journal is not None:
                    self.event_journal.append(
                        "ROUND_ABORTED",
                        round=round_number,
                        payload={"error_type": type(exc).__name__},
                    )
                self.warnings.append(f"Model round failed: {type(exc).__name__}: {exc}")
                self._checkpoint("model_error")
                return "model_error"
            observations_before_model = 0
            if self.event_journal is not None:
                self.event_journal.append("ROUND_COMPLETED", round=self.limits.rounds)
            self._checkpoint("round_complete")
            await self._status(
                f"Completed round {self.limits.rounds}; "
                f"last_capability={getattr(self.agent, 'last_capability', None) or 'unknown'}; "
                f"tokens={self.limits.tokens}; cost={self.limits.cost:.6f}."
            )
            current_resources = len(self.context.state.inspected_resources)
            current_call_signatures = len(self.context.state.repeated_calls)
            current_memory_revision = self.context.state.working_memory.revision
            current_knowledge_revision = (
                self.investigation_session.knowledge_state.revision
                if self.investigation_session is not None
                else 0
            )
            if (
                current_resources == known_resources
                and current_call_signatures == known_call_signatures
                and current_memory_revision == known_memory_revision
                and current_knowledge_revision == known_knowledge_revision
            ):
                no_progress += 1
            else:
                no_progress = 0
                known_resources = current_resources
                known_call_signatures = current_call_signatures
                known_memory_revision = current_memory_revision
                known_knowledge_revision = current_knowledge_revision
            if runtime_metrics is not None:
                runtime_metrics.no_progress_rounds = no_progress
        if self.context.submitted:
            self.limits.complete()
            if runtime_metrics is not None:
                runtime_metrics.no_progress_rounds = no_progress
            self._checkpoint("proposal_validated")
            return "proposal_validated"
        reason = self.limits.reason or "limit_reached"
        self._checkpoint(reason)
        return reason
