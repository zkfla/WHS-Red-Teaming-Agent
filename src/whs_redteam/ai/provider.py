"""OpenAI-compatible planning provider; it never receives raw request values."""

from __future__ import annotations

import json
import os
from typing import Protocol

import httpx

from whs_redteam.ai.models import PlannerDecision, PlanningContext
from whs_redteam.audit import AuditLogger
from whs_redteam.config import AIConfig


class AIProviderError(RuntimeError):
    """Raised when an AI provider cannot return a valid structured decision."""


class PlannerProvider(Protocol):
    provider_name: str
    model_name: str

    async def plan(self, context: PlanningContext) -> PlannerDecision: ...


class OpenAICompatiblePlanner:
    provider_name = "openai_compatible"

    def __init__(
        self,
        config: AIConfig,
        audit_logger: AuditLogger | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.provider != "openai_compatible":
            raise ValueError("OpenAICompatiblePlanner requires the openai_compatible provider")
        if not config.model:
            raise ValueError("AI model name is required for the openai_compatible provider")
        self.config = config
        self.audit_logger = audit_logger
        self.transport = transport
        self.model_name = config.model

    async def plan(self, context: PlanningContext) -> PlannerDecision:
        variable = self.config.api_key_env
        api_key = os.environ.get(variable) if variable else None
        if variable and api_key is None:
            raise AIProviderError(f"AI API key environment variable is missing: {variable}")
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        system_prompt = (
            "You are a safety-constrained web security planning assistant. "
            "Return only JSON matching planner-decision-v1. Select candidate_id values "
            "from the supplied list. Never invent URLs, parameters, payloads, or methods. "
            "Choose run_sqli_safe only; the deterministic probe creates all test values."
        )
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": context.model_dump_json(),
                },
            ],
        }
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "ai_planner_requested",
                provider=self.provider_name,
                model=self.model_name,
                candidate_count=len(context.candidates),
                planning_round=context.planning_round,
            )
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
            ) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AIProviderError(f"AI provider request failed: {type(exc).__name__}") from exc
        try:
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            if not isinstance(content, str):
                raise ValueError("AI response content is not text")
            decision = PlannerDecision.model_validate_json(_extract_json(content))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIProviderError("AI provider returned invalid planner JSON") from exc
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "ai_planner_succeeded",
                provider=self.provider_name,
                model=self.model_name,
                selected_action_count=len(decision.selected_actions),
                planning_round=context.planning_round,
            )
        return decision


def _extract_json(content: str) -> str:
    """Accept a JSON object wrapped in a markdown fence without logging it."""

    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].lstrip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("planner response contains no JSON object")
    json.loads(stripped[start : end + 1])
    return stripped[start : end + 1]
