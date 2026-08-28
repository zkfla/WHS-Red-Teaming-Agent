"""Optional AI explanation writer for already-redacted reports."""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from whs_redteam.ai.models import ProviderUsage
from whs_redteam.ai.provider import AIProviderError, _extract_json, _nonnegative_int
from whs_redteam.audit import AuditLogger
from whs_redteam.config import AIConfig


class AIReportExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(default="", max_length=1_500)
    limitations: str = Field(default="", max_length=1_500)
    next_steps: str = Field(default="", max_length=1_500)


class OpenAICompatibleReportWriter:
    provider_name = "openai_compatible"

    def __init__(
        self,
        config: AIConfig,
        audit_logger: AuditLogger | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.provider != "openai_compatible":
            raise ValueError("AI report writer requires the openai_compatible provider")
        if not config.model:
            raise ValueError("AI model name is required for the report writer")
        self.config = config
        self.audit_logger = audit_logger
        self.transport = transport
        self.last_usage = ProviderUsage()

    async def explain(self, report: dict[str, Any]) -> dict[str, str]:
        self.last_usage = ProviderUsage()
        variable = self.config.api_key_env
        api_key = os.environ.get(variable) if variable else None
        if variable and api_key is None:
            raise AIProviderError(f"AI API key environment variable is missing: {variable}")
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": self.config.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only JSON with summary, limitations, and next_steps. "
                        "Explain the redacted security report conservatively. Never claim "
                        "an exploit beyond the machine verdict and never include payloads."
                    ),
                },
                {"role": "user", "content": _redacted_report(report)},
            ],
        }
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        request_attempted = False
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.config.timeout_seconds,
            ) as client:
                request_attempted = True
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
            if not isinstance(body, dict):
                raise ValueError("AI report response is not an object")
            usage = body.get("usage")
            if isinstance(usage, dict):
                self.last_usage = ProviderUsage(
                    api_calls=1,
                    input_tokens=_nonnegative_int(usage.get("prompt_tokens")),
                    output_tokens=_nonnegative_int(usage.get("completion_tokens")),
                )
            else:
                self.last_usage = ProviderUsage(api_calls=1)
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("AI report response content is not text")
            explanation = AIReportExplanation.model_validate_json(_extract_json(content))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            if request_attempted and self.last_usage.api_calls == 0:
                self.last_usage = ProviderUsage(api_calls=1)
            raise AIProviderError("AI report writer returned invalid JSON") from exc
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "ai_report_explanation_succeeded",
                provider=self.provider_name,
                model=self.config.model,
                api_call_count=self.last_usage.api_calls,
                provider_usage_reported=bool(
                    self.last_usage.input_tokens or self.last_usage.output_tokens
                ),
            )
        return explanation.model_dump(mode="json")


def _redacted_report(report: dict[str, Any]) -> str:
    """Send only report metadata and finding summaries to the model."""

    decision = report.get("exploitability_decision") or {}
    findings = report.get("findings") or []
    safe = {
        "verdict": decision.get("verdict"),
        "confirmed_findings": decision.get("confirmed_findings", 0),
        "inconclusive_findings": decision.get("inconclusive_findings", 0),
        "rationale": decision.get("rationale", ""),
        "findings": [
            {
                "category": item.get("category"),
                "status": item.get("status"),
                "severity": item.get("severity"),
                "summary": item.get("summary", ""),
            }
            for item in findings
            if isinstance(item, dict)
        ],
    }
    import json

    return json.dumps(safe, ensure_ascii=False, sort_keys=True)
