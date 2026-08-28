"""OpenAI-compatible planning provider; it never receives raw request values."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Protocol

import httpx

from whs_redteam.ai.models import PlannerDecision, PlanningContext, ProviderUsage
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
        self.last_usage = ProviderUsage()

    async def plan(
        self,
        context: PlanningContext,
        *,
        max_attempts: int | None = None,
    ) -> PlannerDecision:
        self.last_usage = ProviderUsage()
        if context.serialized_size > self.config.max_input_characters:
            raise AIProviderError("AI planner context exceeds the configured size limit")
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
                max_attempts=(
                    max_attempts
                    if max_attempts is not None
                    else self.config.retry_attempts + 1
                ),
            )
        body: dict[str, object] | None = None
        attempts = 0
        attempt_limit = (
            max_attempts if max_attempts is not None else self.config.retry_attempts + 1
        )
        if attempt_limit < 1:
            raise ValueError("max_attempts must be at least 1")
        for attempt in range(attempt_limit):
            attempts = attempt + 1
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.config.timeout_seconds,
                ) as client:
                    response = await client.post(endpoint, json=payload, headers=headers)
                    response.raise_for_status()
                    parsed_body = response.json()
                if not isinstance(parsed_body, dict):
                    raise ValueError("AI provider response is not an object")
                body = parsed_body
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                retryable = status == 429 or status >= 500
                if not retryable or attempt >= attempt_limit - 1:
                    self.last_usage = ProviderUsage(api_calls=attempts)
                    raise AIProviderError(
                        f"AI provider request failed: HTTP_{status}"
                    ) from exc
                self._emit_retry(attempts, f"HTTP_{status}")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= attempt_limit - 1:
                    self.last_usage = ProviderUsage(api_calls=attempts)
                    raise AIProviderError(
                        f"AI provider request failed: {type(exc).__name__}"
                    ) from exc
                self._emit_retry(attempts, type(exc).__name__)
            except ValueError as exc:
                self.last_usage = ProviderUsage(api_calls=attempts)
                raise AIProviderError(
                    "AI provider returned an invalid response object"
                ) from exc
            delay = self.config.retry_backoff_seconds * (2**attempt)
            if delay:
                await asyncio.sleep(delay)
        if body is None:
            raise AIProviderError("AI provider returned no response")
        usage = body.get("usage")
        if isinstance(usage, dict):
            self.last_usage = ProviderUsage(
                api_calls=attempts,
                input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
                output_tokens=_nonnegative_int(usage.get("completion_tokens")),
            )
        else:
            self.last_usage = ProviderUsage(api_calls=attempts)
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
                api_call_count=self.last_usage.api_calls,
                provider_usage_reported=bool(
                    self.last_usage.input_tokens or self.last_usage.output_tokens
                ),
            )
        return decision

    def _emit_retry(self, attempt: int, reason: str) -> None:
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "ai_planner_retry",
                provider=self.provider_name,
                model=self.model_name,
                attempt=attempt,
                reason=reason,
            )


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


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
