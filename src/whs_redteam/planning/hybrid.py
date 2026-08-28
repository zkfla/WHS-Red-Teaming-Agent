"""Hybrid SQLi candidate planner with fail-closed validation and rules fallback."""

from __future__ import annotations

from dataclasses import dataclass, field

from whs_redteam.ai.models import (
    PlannedAction,
    PlannerDecision,
    PlanningCandidate,
    PlanningContext,
    ProviderUsage,
)
from whs_redteam.ai.provider import AIProviderError, OpenAICompatiblePlanner, PlannerProvider
from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig, InjectionFixture


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    decision: PlannerDecision
    provider: str
    model: str
    fallback_used: bool
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    fallback_reason: str | None = None


class RulesPlanningProvider:
    provider_name = "rules"
    model_name = "deterministic"

    async def plan(self, context: PlanningContext) -> PlannerDecision:
        return PlannerDecision(
            selected_actions=tuple(
                PlannedAction(
                    candidate_id=candidate.candidate_id,
                    action="run_sqli_safe",
                    reason="Selected by the deterministic SQLi candidate planner.",
                    confidence=1.0,
                )
                for candidate in context.candidates
            ),
            finish=not context.candidates,
            finish_reason=("No SQLi candidates remain." if not context.candidates else None),
        )


def planning_context(
    fixtures: tuple[InjectionFixture, ...],
    *,
    remaining_requests: int,
    planning_round: int,
    prior_results: tuple[dict[str, object], ...] = (),
) -> PlanningContext:
    candidates = tuple(
        PlanningCandidate(
            candidate_id=fixture.fixture_id,
            endpoint_template=fixture.target_url,
            parameter=fixture.parameter,
            parameter_kind=fixture.parameter_kind,
        )
        for fixture in fixtures
        if "sqli" in fixture.checks
    )
    return PlanningContext(
        candidates=candidates,
        remaining_requests=remaining_requests,
        planning_round=planning_round,
        prior_results=prior_results,
    )


class HybridPlanner:
    def __init__(
        self,
        config: EngagementConfig,
        audit_logger: AuditLogger,
        *,
        provider: PlannerProvider | None = None,
    ) -> None:
        self.config = config
        self.audit_logger = audit_logger
        self._provider_init_error: Exception | None = None
        if provider is not None:
            self.provider = provider
        else:
            try:
                self.provider = self._provider_from_config()
            except (ValueError, TypeError) as exc:
                # A malformed optional AI configuration must still leave the
                # bounded deterministic planner usable when fallback is enabled.
                self.provider = RulesPlanningProvider()
                self._provider_init_error = exc

    def _provider_from_config(self) -> PlannerProvider:
        if self.config.ai.provider == "openai_compatible":
            return OpenAICompatiblePlanner(self.config.ai, self.audit_logger)
        if self.config.planner_mode == "hybrid":
            raise ValueError(
                "hybrid planner requires ai.provider=openai_compatible"
            )
        return RulesPlanningProvider()

    async def plan(
        self,
        context: PlanningContext,
        *,
        api_calls_used: int = 0,
    ) -> PlanningOutcome:
        if self.config.planner_mode == "rules":
            return PlanningOutcome(
                await RulesPlanningProvider().plan(context),
                "rules",
                "deterministic",
                False,
            )
        if api_calls_used >= self.config.ai.max_api_calls_per_run:
            reason = "api_call_budget_exhausted"
            self.audit_logger.emit(
                "ai_planner_fallback",
                reason=reason,
                planning_round=context.planning_round,
            )
            return PlanningOutcome(
                await RulesPlanningProvider().plan(context),
                "rules",
                "deterministic",
                True,
                ProviderUsage(),
                reason,
            )
        if self._provider_init_error is not None:
            if not self.config.ai.fallback_to_rules:
                raise self._provider_init_error
            reason = type(self._provider_init_error).__name__
            self.audit_logger.emit(
                "ai_planner_fallback",
                reason=reason,
                planning_round=context.planning_round,
            )
            return PlanningOutcome(
                await RulesPlanningProvider().plan(context),
                "rules",
                "deterministic",
                True,
                getattr(self.provider, "last_usage", ProviderUsage()),
                reason,
            )
        try:
            remaining_api_calls = max(
                1,
                self.config.ai.max_api_calls_per_run - api_calls_used,
            )
            if isinstance(self.provider, OpenAICompatiblePlanner):
                decision = await self.provider.plan(
                    context,
                    max_attempts=remaining_api_calls,
                )
            else:
                decision = await self.provider.plan(context)
            self._validate_decision(decision, context)
            return PlanningOutcome(
                decision,
                getattr(self.provider, "provider_name", "custom"),
                getattr(self.provider, "model_name", "unknown"),
                False,
                getattr(self.provider, "last_usage", ProviderUsage()),
            )
        except (AIProviderError, ValueError, TypeError) as exc:
            if not self.config.ai.fallback_to_rules:
                raise
            reason = type(exc).__name__
            self.audit_logger.emit(
                "ai_planner_fallback",
                reason=reason,
                planning_round=context.planning_round,
            )
            return PlanningOutcome(
                await RulesPlanningProvider().plan(context),
                "rules",
                "deterministic",
                True,
                getattr(self.provider, "last_usage", ProviderUsage()),
                reason,
            )

    @staticmethod
    def _validate_decision(
        decision: PlannerDecision,
        context: PlanningContext,
    ) -> None:
        known = {candidate.candidate_id for candidate in context.candidates}
        seen: set[str] = set()
        for action in decision.selected_actions:
            if action.action != "run_sqli_safe":
                raise ValueError("only run_sqli_safe actions are allowed in the SQLi planner")
            if action.candidate_id not in known:
                raise ValueError("planner selected an unknown candidate")
            if action.candidate_id in seen:
                raise ValueError("planner selected a candidate more than once")
            seen.add(action.candidate_id)
