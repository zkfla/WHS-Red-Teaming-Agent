"""Replaceable inference procedures which return proposals, never apply state."""

from __future__ import annotations

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
    """Every internal model request shares the parent's existing attempt budget."""

    def __init__(self, client: StructuredDecisionClient, budget: DecisionBudget):
        self._client = client
        self._budget = budget

    async def infer(self, *, stage, payload, response_schema, system=""):
        return await self._client.ask(
            stage=stage,
            payload=payload,
            response_schema=response_schema,
            system=system,
            budget=self._budget,
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
