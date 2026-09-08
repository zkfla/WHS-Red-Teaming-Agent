from __future__ import annotations

from datetime import datetime

from redteam_core.knowledge.state import CoreSession
from redteam_core.models.runtime import (
    RuntimeBudgetLimits,
    RuntimeUsage,
    TerminationDecision,
    TerminationKind,
)


def _budget_reason(limits: RuntimeBudgetLimits, usage: RuntimeUsage) -> str | None:
    checks = (
        (limits.max_rounds, usage.rounds, "max_rounds"),
        (limits.max_tokens, usage.tokens, "max_tokens"),
        (limits.max_requests, usage.requests, "max_requests"),
        (
            limits.max_active_duration_seconds,
            usage.active_duration_seconds,
            "max_active_duration_seconds",
        ),
    )
    for ceiling, current, name in checks:
        if ceiling is not None and current >= ceiling:
            return f"{name}:{current}>={ceiling}"
    return None


def decide_termination(
    session: CoreSession,
    *,
    limits: RuntimeBudgetLimits,
    usage: RuntimeUsage,
    evaluation_time: datetime,
    internal_error: str | None = None,
    handled_external_failure: str | None = None,
    normal_completion: bool = False,
    llm_finish_reason: str | None = None,
) -> TerminationDecision | None:
    """Deterministic authority. Policy and budgets outrank model-requested finish."""

    if session.execution.stop_reason:
        return TerminationDecision.create(
            kind=TerminationKind.POLICY_STOP,
            reason=session.execution.stop_reason,
            evaluation_time=evaluation_time,
        )
    budget_reason = _budget_reason(limits, usage)
    if budget_reason is not None:
        return TerminationDecision.create(
            kind=TerminationKind.BUDGET_STOP,
            reason=budget_reason,
            evaluation_time=evaluation_time,
        )
    if internal_error:
        return TerminationDecision.create(
            kind=TerminationKind.INTERNAL_ERROR,
            reason=internal_error,
            evaluation_time=evaluation_time,
        )
    if handled_external_failure:
        return TerminationDecision.create(
            kind=TerminationKind.HANDLED_EXTERNAL_FAILURE,
            reason=handled_external_failure,
            evaluation_time=evaluation_time,
        )
    if normal_completion:
        return TerminationDecision.create(
            kind=TerminationKind.NORMAL_COMPLETION,
            reason="normal_completion",
            evaluation_time=evaluation_time,
        )
    if llm_finish_reason:
        return TerminationDecision.create(
            kind=TerminationKind.LLM_FINISH,
            reason=llm_finish_reason,
            evaluation_time=evaluation_time,
        )
    return None
