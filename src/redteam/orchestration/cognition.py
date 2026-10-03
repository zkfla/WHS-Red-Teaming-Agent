"""Structured LLM decisions with schema enforcement and bounded retries.

All model judgments go through StructuredDecisionClient. The model always
decides; this module only enforces the JSON Schema, retries transport and
format failures inside one shared attempt budget, and raises instead of
returning an empty result when the budget is spent.

Contract notes for the orchestration loop:
- Every reply is validated with jsonschema against the caller schema.
- Native schema enforcement lives in the LLM adapter; this client always
  calls complete_structured and re-validates, so either path still lets
  the model decide.
- Transport and format/schema errors share one DecisionBudget
  (max_attempts). Each retry appends the previous validation message to
  the prompt. One decision uses one budget: pass the same DecisionBudget
  to several ask calls (draft, review, action) to share it; never nest an
  outer caller count with the client budget.
- Budget spent raises DecisionFailure(kind="budget"); empty results are
  never returned.
- SDK-level retries are disabled (num_retries=0); this client is the only
  retry layer.
- Chain-of-thought is never requested or stored; the system prompt is
  passed through unchanged and no reasoning prompt is added here.
- Extra evidence lookup is only via the optional context_provider hook.
  Without it the client proceeds with the given payload only.
- tenacity drives attempts through AsyncRetrying in the same budget.
  It is a required dependency declared in pyproject.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from jsonschema import ValidationError
from jsonschema import validate as _validate_schema
from pydantic import ValidationError as ModelValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    diagnostic_for_exception,
    safe_feedback,
    schema_diagnostic,
)

DECISION_KINDS = ("transport", "format", "budget", "unknown")

_MAX_FEEDBACK_CHARS = 500


class DecisionFailure(Exception):
    """Raised when the attempt budget is spent without a valid decision.

    Attributes are kept for the loop: stage names the calling step, kind
    is one of transport, format, budget, or unknown, attempts counts the
    budgeted LLM requests actually made, and detail carries the last
    truncated error message. kind == "budget" means the shared
    DecisionBudget was spent (see DecisionBudget); detail then names the
    last transport/format failure and the max/used counters.
    """

    def __init__(
        self,
        stage: str,
        kind: str,
        attempts: int,
        detail: str = "",
        *,
        diagnostic: DecisionDiagnostic | None = None,
    ) -> None:
        self.stage = stage
        self.kind = kind
        self.attempts = attempts
        self.diagnostic = diagnostic or DecisionDiagnostic(
            code="decision_budget_exhausted" if kind == "budget" else "decision_failed",
            stage=stage,
            kind=kind,
            attempts=attempts,
            summary=safe_feedback(detail or "The model decision failed."),
            retryable=kind in {"transport", "format"},
        )
        self.detail = self.diagnostic.feedback()
        super().__init__(
            f"{stage} decision failed (kind={kind}) after {attempts} attempts: {self.detail}"
        )


class DecisionBudget:
    """Shared attempt budget for one decision or a chain of decisions.

    One budgeted unit is consumed per actual LLM request. Transport
    failures, format/schema failures, and caller-side semantic retries
    that call ask again share the same instance, so an outer caller loop
    must not multiply its own limit with the client's max_attempts.
    A single decision uses a single budget: when ask receives one it is
    the sole limit and max_attempts is ignored (no nesting).

    Exposes remaining/used counters for the loop. When remaining reaches
    zero the next ask raises DecisionFailure with kind "budget" instead
    of returning an empty dict.
    """

    def __init__(self, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = int(max_attempts)
        self._used = 0
        self.last_failure: DecisionDiagnostic | None = None

    @property
    def max_attempts(self) -> int:
        """Total budgeted LLM requests for this instance."""
        return self._max_attempts

    @property
    def used(self) -> int:
        """LLM requests consumed so far."""
        return self._used

    @property
    def remaining(self) -> int:
        """LLM requests still available (max_attempts - used)."""
        return self._max_attempts - self._used

    def consume(self) -> int:
        """Consume one budgeted LLM request and return remaining.

        Raises RuntimeError when no budget remains; the client checks
        remaining first and raises DecisionFailure(kind="budget")
        instead, so this error only signals a programming bug.
        """
        if self._used >= self._max_attempts:
            raise RuntimeError("decision budget exhausted")
        self._used += 1
        return self.remaining

    def __repr__(self) -> str:
        return (
            f"DecisionBudget(max_attempts={self._max_attempts}, "
            f"used={self._used}, remaining={self.remaining})"
        )


class _RetryableDecisionError(Exception):
    def __init__(self, diagnostic: DecisionDiagnostic) -> None:
        self.diagnostic = diagnostic
        self.kind = diagnostic.kind
        self.feedback = diagnostic.feedback()
        super().__init__(self.feedback)


class _NonObjectDecision(ValueError):
    """The model returned a non-object JSON value."""


def _parse_text_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        fence = chr(96) * 3
        if text.startswith(fence):
            lines = text.splitlines()
            if lines and lines[0].startswith(fence):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith(fence):
                lines = lines[:-1]
            text = chr(10).join(lines).strip()
        parsed = json.loads(text or "{}")
        if not isinstance(parsed, dict):
            raise _NonObjectDecision("Return a JSON object.")
        return parsed
    raise _NonObjectDecision("Return a JSON object.")


class StructuredDecisionClient:
    """Ask the model for a JSON decision validated against a schema.

    The model always decides; schema enforcement never substitutes a
    rule-based answer. Transport failures and format/schema mismatches
    share one DecisionBudget. Each retry carries the previous failure
    message back into the user prompt. Budget spent raises
    DecisionFailure(kind="budget"), never an empty dict.

    No chain-of-thought is requested or stored; system is passed through
    unchanged.

    Budget sharing: pass the same DecisionBudget to several ask calls to
    share one limit across draft, review, and action decisions (and any
    caller-side semantic retry loop). When budget is None an internal
    DecisionBudget(self._max_attempts) is used, so existing callers keep
    working. When a budget is given it is the sole limit;
    self._max_attempts is ignored and never multiplied with it.

    Evidence hook: context_provider is an optional (stage, payload)
    callable, sync or async, injected by the root. When present it may
    return an extra dict merged into the payload without overwriting
    explicit keys. When absent (default) no extra lookup is attempted;
    a payload key named evidence_context is passed through as-is.
    """

    def __init__(
        self,
        llm: Any,
        max_attempts: int = 3,
        context_provider: Callable[..., Any] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._llm = llm
        self._max_attempts = max_attempts
        self._context_provider = context_provider

    async def ask(
        self,
        *,
        stage: str,
        payload: dict[str, Any],
        response_schema: dict[str, Any],
        system: str = "",
        budget: DecisionBudget | None = None,
    ) -> dict[str, Any]:
        if budget is None:
            budget = DecisionBudget(self._max_attempts)
        elif not isinstance(budget, DecisionBudget):
            raise TypeError("budget must be a DecisionBudget or None")
        working = dict(payload)
        extra = await self._resolve_extra_context(stage, payload)
        for key, value in extra.items():
            working.setdefault(key, value)
        json.dumps(working)
        return await self._ask_with_tenacity(stage, working, response_schema, system, budget)

    async def _resolve_extra_context(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._context_provider
        if provider is None:
            return {}
        result = provider(stage, dict(payload))
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise ValueError("context_provider must return a dict or None")
        return result

    def _prompt_for(self, working: dict[str, Any], feedback: str, kind: str) -> str:
        body = json.dumps(working)
        if not feedback:
            return body
        return (
            f"{body}\n\nPrevious attempt failed ({kind}): {feedback}\n"
            "Respond with a single JSON object only, matching the required schema."
        )

    async def _attempt_once(
        self,
        *,
        stage: str,
        working: dict[str, Any],
        response_schema: dict[str, Any],
        system: str,
        feedback: str,
        kind: str,
    ) -> dict[str, Any]:
        messages = [{"role": "user", "content": self._prompt_for(working, feedback, kind)}]
        try:
            raw = await self._llm.complete_structured(
                messages=messages,
                response_schema=response_schema,
                system=system,
                num_retries=0,
            )
        except Exception as exc:
            if isinstance(exc, ValidationError):
                diagnostic = schema_diagnostic(exc, response_schema, stage=stage)
            elif isinstance(exc, ModelValidationError):
                diagnostic = diagnostic_for_exception(exc, stage=stage)
            elif isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
                diagnostic = DecisionDiagnostic(
                    code="invalid_json",
                    stage=stage,
                    kind="format",
                    summary="Return a valid JSON object.",
                    retryable=True,
                )
            else:
                diagnostic = DecisionDiagnostic(
                    code="llm_transport_failed",
                    stage=stage,
                    kind="transport",
                    summary="The LLM request failed before a validated response was received.",
                    retryable=True,
                )
            raise _RetryableDecisionError(diagnostic) from exc
        try:
            candidate = _parse_text_result(raw)
            _validate_schema(instance=candidate, schema=response_schema)
        except ValidationError as exc:
            raise _RetryableDecisionError(
                schema_diagnostic(exc, response_schema, stage=stage)
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, _NonObjectDecision) as exc:
            raise _RetryableDecisionError(
                DecisionDiagnostic(
                    code="invalid_json",
                    stage=stage,
                    kind="format",
                    summary="Return a valid JSON object.",
                    retryable=True,
                )
            ) from exc
        return candidate

    async def _ask_with_tenacity(
        self,
        stage: str,
        working: dict[str, Any],
        response_schema: dict[str, Any],
        system: str,
        budget: DecisionBudget,
    ) -> dict[str, Any]:
        def exhausted() -> DecisionFailure:
            diagnostic = DecisionDiagnostic(
                code="decision_budget_exhausted",
                stage=stage,
                kind="budget",
                attempts=budget.used,
                summary="The shared decision attempt budget is exhausted.",
                retryable=False,
                last_cause=budget.last_failure,
                location=budget.last_failure.location if budget.last_failure else "",
            )
            return DecisionFailure(stage, "budget", budget.used, diagnostic=diagnostic)

        if budget.remaining <= 0:
            raise exhausted()
        allowed = budget.remaining
        feedback = ""
        kind = "unknown"
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(allowed),
                retry=retry_if_exception_type(_RetryableDecisionError),
                reraise=True,
            ):
                if budget.remaining <= 0:
                    raise exhausted()
                budget.consume()
                with attempt:
                    try:
                        return await self._attempt_once(
                            stage=stage,
                            working=working,
                            response_schema=response_schema,
                            system=system,
                            feedback=feedback,
                            kind=kind,
                        )
                    except _RetryableDecisionError as exc:
                        budget.last_failure = exc.diagnostic.model_copy(
                            update={"attempts": budget.used}
                        )
                        feedback = exc.feedback
                        kind = exc.kind
                        raise
        except _RetryableDecisionError as exc:
            raise exhausted() from exc
        raise exhausted()
