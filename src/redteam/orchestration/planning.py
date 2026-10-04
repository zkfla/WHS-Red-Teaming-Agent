"""Replaceable inference procedures which return proposals, never apply state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient


class Inference(Protocol):
    async def infer(
        self,
        *,
        stage: str,
        payload: dict[str, Any],
        response_schema: dict[str, Any],
        system: str = "",
    ) -> dict[str, Any]: ...


class BudgetedInference:
    """Every internal model request shares the parent's existing attempt budget.

    The shared DecisionBudget also carries request-local token usage: each
    ask records attempts, known tokens, and unknown-usage counts on it, so
    delegation owners read usage from the same budget instance they passed
    in. No shared last_usage state is kept on the client.

    before_call is an optional zero-argument guard (sync or async),
    forwarded to every ask -- including each retry's actual request -- so
    stop/deadline checks run on the same path as normal decisions. None
    keeps existing callers unchanged.
    """

    def __init__(
        self,
        client: StructuredDecisionClient,
        budget: DecisionBudget,
        before_call: Callable[[], Any] | None = None,
    ):
        self._client = client
        self._budget = budget
        self._before_call = before_call

    @property
    def budget(self) -> DecisionBudget:
        """The shared assignment budget carrying attempts and token usage."""
        return self._budget

    @property
    def tokens_used(self) -> int:
        """Known model tokens consumed through this inference scope."""
        return self._budget.tokens_used

    @property
    def unknown_usage_count(self) -> int:
        """Requests in this scope without reported usage."""
        return self._budget.unknown_usage_count

    @property
    def tokens_fully_known(self) -> bool:
        """Whether every scoped request reported its token usage."""
        return self._budget.tokens_fully_known

    def should_halt_for_tokens(self, max_tokens: int | None) -> bool:
        """Token-guard check against a configured cap (WorkBudget.max_tokens).

        True means further model calls in this scope must stop: known
        usage reached the cap, or some request's usage is unknown so the
        cap can no longer be proven.
        """
        return self._budget.token_halt_required(max_tokens)

    def token_usage(self) -> dict[str, Any]:
        """Request-local usage snapshot for aggregate counters and reports."""
        return {
            "attempts": self._budget.used,
            "tokens_used": self._budget.tokens_used,
            "unknown_usage_count": self._budget.unknown_usage_count,
            "tokens_fully_known": self._budget.tokens_fully_known,
        }

    async def infer(self, *, stage, payload, response_schema, system=""):
        return await self._client.ask(
            stage=stage,
            payload=payload,
            response_schema=response_schema,
            system=system,
            budget=self._budget,
            before_call=self._before_call,
        )


@dataclass(frozen=True)
class PlanningRequest:
    stage: str
    payload: dict[str, Any]
    response_schema: dict[str, Any]
    inference: Inference
    system: str = ""


class PlanningProvider(Protocol):
    async def propose(self, request: PlanningRequest) -> dict[str, Any]: ...


class StructuredLLMPlanningProvider:
    """Default procedure; alternate providers may use their own inner graph."""

    async def propose(self, request: PlanningRequest) -> dict[str, Any]:
        return await request.inference.infer(
            stage=request.stage,
            payload=request.payload,
            response_schema=request.response_schema,
            system=request.system,
        )
