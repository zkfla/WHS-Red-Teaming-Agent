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
import time
from collections.abc import Callable
from typing import Any, Literal

from jsonschema import ValidationError
from jsonschema import validate as _validate_schema
from pydantic import ValidationError as ModelValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ports.llm import LLMProviderFailure
from redteam.orchestration.diagnostics import (
    DecisionDiagnostic,
    JsonNormalizationContext,
    decision_context,
    diagnostic_for_exception,
    error_type_for,
    evidence_request_shape,
    exception_chain_for,
    json_parse_context,
    safe_feedback,
    schema_diagnostic,
    shallow_response_shape,
)

DECISION_KINDS = ("transport", "format", "budget", "unknown")

_MAX_FEEDBACK_CHARS = 500
_EMPTY_OBJECT_TEXT = "{}"


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


class DecisionValidationError(Exception):
    """A caller-owned, safe semantic rejection of an otherwise valid reply.

    Optional response validators are pure checks against captured inputs. They
    do not admit writes or grant permission. Correction consumes the SAME
    finite repair budget as schema errors, not a second outer retry pool.
    """

    def __init__(self, diagnostic: DecisionDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.feedback())


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

    def __init__(
        self,
        max_attempts: int = 3,
        *,
        normal_limit: int | None = None,
        repair_limit: int | None = None,
        total_limit: int | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = int(max_attempts)
        if (normal_limit is None) != (repair_limit is None):
            raise ValueError("normal_limit and repair_limit must be supplied together")
        repair_value = repair_limit if repair_limit is not None else 0
        if normal_limit is not None and (normal_limit < 1 or repair_value < 0):
            raise ValueError("normal_limit must be positive and repair_limit non-negative")
        self.normal_limit = normal_limit
        self.repair_limit = repair_limit
        if normal_limit is not None:
            self._max_attempts = normal_limit + repair_value
        if total_limit is not None:
            if total_limit < 1:
                raise ValueError("total_limit must be positive")
            self._max_attempts = min(self._max_attempts, total_limit)
        self.normal_used = 0
        self.repair_used = 0
        self._used = 0
        self._tokens_used = 0
        self._tokens_unknown = 0
        self.last_failure: DecisionDiagnostic | None = None
        self.trace: list[dict[str, Any]] = []
        self.stage_attempts: dict[str, int] = {}
        self.failure_counts: dict[str, int] = {}

    def diagnostic_details(self) -> dict[str, Any]:
        return {
            "trace": list(self.trace),
            "stage_attempts": dict(self.stage_attempts),
            "attempt_limit": self.max_attempts,
            "normal_limit": self.normal_limit,
            "repair_limit": self.repair_limit,
            "normal_used": self.normal_used,
            "repair_used": self.repair_used,
            "failure_counts": dict(self.failure_counts),
        }

    @classmethod
    def reasoning_chain(
        cls, normal_limit: int, repair_limit: int, *, total_limit: int | None = None
    ) -> DecisionBudget:
        """Bound normal planning separately from correction, without multiplying caps."""
        return cls(
            normal_limit=normal_limit, repair_limit=repair_limit, total_limit=total_limit
        )

    def permits(self, *, repair: bool = False) -> bool:
        if self.remaining <= 0:
            return False
        if self.normal_limit is None:
            return repair or self.normal_used < self.max_attempts
        if repair:
            return self.repair_used < (self.repair_limit or 0)
        return self.normal_used < self.normal_limit

    def exhaustion_code(self, *, repair: bool = False) -> str:
        if self.normal_limit is None or self.remaining <= 0:
            return "decision_budget_exhausted"
        return "decision_repair_budget_exhausted" if repair else "decision_normal_budget_exhausted"

    def record_progress(self, *, stage: str) -> None:
        """Count a provider decision that made no model request, never invent usage."""
        if self.normal_limit is None:
            # Legacy procedure chains still need a finite progress cap.
            limit = self.max_attempts
        else:
            limit = self.normal_limit
        if self.normal_used >= limit:
            raise RuntimeError("normal decision budget exhausted")
        self.normal_used += 1
        self.record(stage, "provider_decision_without_request")

    def record(self, stage: str, outcome: str, **details: Any) -> None:
        self.trace.append(
            redact_secret_material(
                {
                    "attempt": self.used,
                    "remaining": self.remaining,
                    "stage": stage,
                    "outcome": outcome,
                    **details,
                }
            )
        )
        self.trace = self.trace[-20:]

    def record_failure(self, code: str) -> None:
        """Count one failure code outside the trimmed trace window."""
        key = str(code)[:100]
        if not key:
            return
        self.failure_counts[key] = self.failure_counts.get(key, 0) + 1

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

    @property
    def tokens_used(self) -> int:
        """Known model tokens consumed so far (requests with reported usage)."""
        return self._tokens_used

    @property
    def unknown_usage_count(self) -> int:
        """Requests made without reported usage (unknown token cost)."""
        return self._tokens_unknown

    @property
    def tokens_fully_known(self) -> bool:
        """Whether every request so far reported its token usage."""
        return self._tokens_unknown == 0

    def add_usage(self, total_tokens: int | None) -> None:
        """Record one request's token usage on this assignment's budget.

        None (or any non-integer/negative value) counts as unknown usage
        instead of zero: without provider telemetry the cost cannot be
        proven, so a configured token budget must halt rather than assume
        nothing was spent.
        """
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
            self._tokens_unknown += 1
            return
        if total_tokens < 0:
            self._tokens_unknown += 1
            return
        self._tokens_used += total_tokens

    def token_halt_required(self, max_tokens: int | None) -> bool:
        """Whether further model investigation must stop under a token cap.

        No cap (None) never halts. With a cap, halt when known usage
        already reached it or when any request's usage is unknown, since
        staying under the cap can no longer be proven.
        """
        if max_tokens is None:
            return False
        if self._tokens_unknown > 0:
            return True
        return self._tokens_used >= max_tokens

    def consume(self, *, stage: str = "decision", repair: bool = False) -> int:
        """Consume one budgeted LLM request and return remaining.

        Raises RuntimeError when no budget remains; the client checks
        remaining first and raises DecisionFailure(kind="budget")
        instead, so this error only signals a programming bug.
        """
        if not self.permits(repair=repair):
            raise RuntimeError("decision budget exhausted")
        self._used += 1
        if repair:
            self.repair_used += 1
        else:
            self.normal_used += 1
        self.stage_attempts[stage] = self.stage_attempts.get(stage, 0) + 1
        return self.remaining

    def __repr__(self) -> str:
        return (
            f"DecisionBudget(max_attempts={self._max_attempts}, "
            f"used={self._used}, remaining={self.remaining})"
        )


class ReasoningHalted(Exception):
    """A before-call guard refused another model request.

    Carries only a safe reason code (for example a stop, pause, or
    deadline marker) -- never prompt text, model output, or secrets.
    The client converts this into DecisionFailure(kind="budget") so no
    new failure kind is introduced.
    """

    def __init__(
        self, reason_code: str = "reasoning_not_allowed", *,
        termination_reason: Literal["budget_exhausted"] | None = None,
    ) -> None:
        if not isinstance(reason_code, str) or not reason_code:
            reason_code = "reasoning_not_allowed"
        self.reason_code = reason_code
        self.diagnostic = DecisionDiagnostic(
            code="reasoning_guard_blocked", stage="reasoning_guard", kind="budget",
            summary=reason_code, retryable=False, termination_reason=termination_reason,
        )
        super().__init__(self.reason_code)


def should_halt_for_token_budget(budget: DecisionBudget, max_tokens: int | None) -> bool:
    """Public token-guard check for delegation owners (root/E).

    Pass the assignment's shared DecisionBudget and the delegation's
    configured token cap (WorkBudget.max_tokens). True means further
    model calls must stop: known usage reached the cap, or some
    request's usage is unknown so the cap can no longer be proven.
    """
    return budget.token_halt_required(max_tokens)


class _RetryableDecisionError(Exception):
    def __init__(
        self, diagnostic: DecisionDiagnostic, proposal_context: dict | None = None,
        *,
        elapsed_ms: int = 0,
        response_shape: dict[str, Any] | None = None,
        normalization: JsonNormalizationContext | None = None,
    ) -> None:
        self.diagnostic = diagnostic
        self.proposal_context = proposal_context or {
            "proposal_shape": {"observed": False, "reason": "unavailable"}
        }
        if normalization is not None:
            self.proposal_context = dict(self.proposal_context)
            self.proposal_context["normalization"] = normalization.model_dump(mode="json")
        self.kind = diagnostic.kind
        self.feedback = diagnostic.feedback()
        self.elapsed_ms = max(0, int(elapsed_ms))
        self.response_shape = dict(response_shape) if isinstance(response_shape, dict) else None
        super().__init__(self.feedback)


class _NonObjectDecision(ValueError):
    """The model returned a non-object JSON value."""

    def __init__(self, value: Any) -> None:
        self.proposal_context: dict[str, Any] = {"proposal_shape": evidence_request_shape(value)}
        super().__init__("Return a JSON object.")


def normalize_json_text(raw: str) -> tuple[str, JsonNormalizationContext]:
    """Parser-owned strip/fence normalization; lengths and flags only, never raw."""
    received_length = len(raw)
    outer_stripped = raw.strip()
    stripped = outer_stripped != raw
    text = outer_stripped
    fence = chr(96) * 3
    fence_stripped = False
    if text.startswith(fence):
        lines = text.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
            fence_stripped = True
        if lines and lines[-1].strip().startswith(fence):
            lines = lines[:-1]
            fence_stripped = True
        if fence_stripped:
            text = chr(10).join(lines).strip()
    return text, JsonNormalizationContext(
        received_type="string",
        received_length=received_length,
        parser_input_length=len(text or _EMPTY_OBJECT_TEXT),
        stripped=stripped,
        fence_stripped=fence_stripped,
        empty=text == "",
    )


def _parse_text_result(
    raw: Any, *, normalization_sink: list[JsonNormalizationContext] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text, normalization = normalize_json_text(raw)
        if normalization_sink is not None:
            normalization_sink.append(normalization)
        parsed = json.loads(text or _EMPTY_OBJECT_TEXT)
        if not isinstance(parsed, dict):
            raise _NonObjectDecision(parsed)
        return parsed
    raise _NonObjectDecision(raw)


def _metered_guard_scope(llm, guard):
    """Run the caller's guard inside the metered facade's post-wait hook.

    The metered facade re-checks time/mission admission after its shared
    semaphore/cooldown wait; routing the existing guard through the same
    ContextVar keeps one time policy for planner, repair, native and
    code-role requests. Non-metered clients keep the caller's pre-call
    guard, so fakes and direct transports are unchanged.
    """
    from contextlib import nullcontext

    from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_guard

    if guard is not None and isinstance(llm, RunMeteredLLM):
        return llm_request_guard(guard)
    return nullcontext()


def _is_metered(llm) -> bool:
    from redteam.orchestration.metered_llm import RunMeteredLLM

    return isinstance(llm, RunMeteredLLM)


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

    Request guard: before_call is an optional zero-argument callable
    (sync or async) awaited before every actual LLM request, including
    retries. A denied guard raises DecisionFailure(kind="budget") with a
    safe fixed reason instead of spending the attempt budget. None (the
    default) keeps existing callers unchanged.

    Usage accounting: when the provider offers
    complete_structured_with_usage, each request's total_tokens is
    recorded on the shared budget; providers without it record unknown
    usage. No shared last_usage state is kept: usage lives on the
    request-local budget the caller already holds.

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
        before_call: Callable[..., Any] | None = None,
        validate_response: Callable[[dict[str, Any]], None] | None = None,
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
        from redteam.orchestration.prompts import instruction_scope

        with instruction_scope(stage):
            return await self._ask_with_tenacity(
                stage, working, response_schema, system, budget, before_call, validate_response
            )

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

    async def ask_native(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str = "",
        budget: DecisionBudget | None = None,
        before_call: Callable[..., Any] | None = None,
        role_id: str | None = None,
    ) -> Any:
        """One native model response, with the existing request budget owner.

        Tool input failures belong to individual calls, not a giant decision
        schema. Only provider failures retry here; execution never does.
        """
        request = getattr(self._llm, "complete_native", None)
        if not callable(request):
            raise DecisionFailure(
                "native_action", "transport", 0,
                diagnostic=DecisionDiagnostic(
                    code="native_tool_calling_unavailable", stage="native_action",
                    kind="transport", retryable=False,
                    summary="The configured model adapter does not support native tool calls.",
                ),
            )
        active = budget or DecisionBudget(self._max_attempts)
        repair = False
        while active.permits(repair=repair):
            if before_call is not None and not _is_metered(self._llm):
                guarded = before_call()
                if inspect.isawaitable(guarded):
                    await guarded
            active.consume(stage="native_action", repair=repair)
            try:
                from redteam.orchestration.prompts import instruction_scope

                with instruction_scope("native_action", role_id), _metered_guard_scope(
                    self._llm, before_call
                ):
                    response = await request(
                        messages=messages, tools=tools, system=system, num_retries=0
                    )
            except ReasoningHalted as halted:
                raise DecisionFailure(
                    "native_action", "budget", active.used,
                    diagnostic=halted.diagnostic.model_copy(update={"stage": "native_action"}),
                ) from halted
            except Exception as exc:
                active.add_usage(None)
                diagnostic = diagnostic_for_exception(exc, stage="native_action")
                if isinstance(exc, LLMProviderFailure):
                    diagnostic = DecisionDiagnostic(
                        code="llm_provider_" + exc.kind, stage="native_action",
                        kind="transport", retryable=exc.retryable,
                        summary=str(exc), **exc.diagnostic_fields(),
                    )
                active.last_failure = diagnostic
                active.record_failure(diagnostic.code)
                if not diagnostic.retryable:
                    raise DecisionFailure(
                        "native_action", diagnostic.kind, active.used,
                        diagnostic=diagnostic,
                    ) from exc
                repair = True
                continue
            active.add_usage(getattr(response, "total_tokens", None))
            active.record(
                "native_action", "native_response",
                tool_call_count=len(getattr(response, "tool_calls", ())),
            )
            return response
        raise DecisionFailure(
            "native_action", "budget", active.used,
            diagnostic=DecisionDiagnostic(
                code=active.exhaustion_code(repair=repair), stage="native_action",
                kind="budget", retryable=False,
                summary="The native model request allowance is exhausted.",
                last_cause=active.last_failure,
                **active.diagnostic_details(),
            ),
        )

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
        budget: DecisionBudget,
        guard: Callable[..., Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        messages = [{"role": "user", "content": self._prompt_for(working, feedback, kind)}]
        use_metered = callable(getattr(self._llm, "complete_structured_with_usage", None))
        attempt_start = time.perf_counter()
        try:
            with _metered_guard_scope(self._llm, guard):
                if use_metered:
                    completion = await self._llm.complete_structured_with_usage(
                        messages=messages,
                        response_schema=response_schema,
                        system=system,
                        num_retries=0,
                    )
                    budget.add_usage(getattr(completion, "total_tokens", None))
                    raw = getattr(completion, "value", None)
                else:
                    raw = await self._llm.complete_structured(
                        messages=messages,
                        response_schema=response_schema,
                        system=system,
                        num_retries=0,
                    )
                    # Legacy dict-only providers never report usage: the cost of
                    # this request is unknown, never zero.
                    budget.add_usage(None)
        except ReasoningHalted as halted:
            raise DecisionFailure(
                stage, "budget", budget.used,
                diagnostic=halted.diagnostic.model_copy(update={"stage": stage}),
            ) from halted
        except Exception as exc:
            # A request was attempted but no usage was reported: unknown.
            budget.add_usage(None)
            elapsed_ms = max(0, int((time.perf_counter() - attempt_start) * 1000))
            response_shape: dict[str, Any] = {"type": "absent", "observed": False}
            parse_context = None
            if isinstance(exc, ValidationError):
                diagnostic = schema_diagnostic(exc, response_schema, stage=stage)
            elif isinstance(exc, ModelValidationError):
                diagnostic = diagnostic_for_exception(exc, stage=stage)
            elif isinstance(exc, json.JSONDecodeError):
                diagnostic = diagnostic_for_exception(exc, stage=stage)
                parse_context = json_parse_context(exc)
            elif isinstance(exc, UnicodeDecodeError):
                diagnostic = DecisionDiagnostic(
                    code="invalid_json",
                    stage=stage,
                    kind="format",
                    summary="Return a valid JSON object.",
                    retryable=True,
                    error_type=error_type_for(exc),
                    exception_chain=exception_chain_for(exc),
                )
            elif isinstance(exc, LLMProviderFailure):
                diagnostic = DecisionDiagnostic(
                    code="llm_provider_" + exc.kind,
                    stage=stage,
                    kind="transport",
                    summary=str(exc),
                    retryable=exc.retryable,
                    error_type=error_type_for(exc),
                    exception_chain=exception_chain_for(exc),
                    **exc.diagnostic_fields(),
                )
            else:
                diagnostic = DecisionDiagnostic(
                    code="llm_transport_failed",
                    stage=stage,
                    kind="transport",
                    summary="The LLM request failed before a validated response was received.",
                    retryable=True,
                    error_type=error_type_for(exc),
                    exception_chain=exception_chain_for(exc),
                )
            raise _RetryableDecisionError(
                diagnostic, parse_context, elapsed_ms=elapsed_ms, response_shape=response_shape,
            ) from exc
        proposal_context = {"proposal_shape": {"observed": False, "reason": "unparsed"}}
        normalization_sink: list[JsonNormalizationContext] = []
        try:
            candidate = _parse_text_result(raw, normalization_sink=normalization_sink)
            normalization = normalization_sink[0] if normalization_sink else None
            base_context = decision_context(
                working, candidate, response_schema=response_schema, validated=False,
            )
            if normalization is not None:
                proposal_context = dict(base_context)
                proposal_context["normalization"] = normalization.model_dump(mode="json")
            else:
                proposal_context = base_context
            _validate_schema(instance=candidate, schema=response_schema)
        except ValidationError as exc:
            elapsed_ms = max(0, int((time.perf_counter() - attempt_start) * 1000))
            raise _RetryableDecisionError(
                schema_diagnostic(exc, response_schema, stage=stage), proposal_context,
                elapsed_ms=elapsed_ms, response_shape=shallow_response_shape(raw),
                normalization=normalization_sink[0] if normalization_sink else None,
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError, _NonObjectDecision) as exc:
            elapsed_ms = max(0, int((time.perf_counter() - attempt_start) * 1000))
            response_shape = shallow_response_shape(raw)
            normalization = normalization_sink[0] if normalization_sink else None
            if isinstance(exc, json.JSONDecodeError):
                parse_ctx = json_parse_context(exc, normalization=normalization)
                raise _RetryableDecisionError(
                    diagnostic_for_exception(exc, stage=stage), parse_ctx,
                    elapsed_ms=elapsed_ms, response_shape=response_shape,
                    normalization=normalization,
                ) from exc
            raise _RetryableDecisionError(
                DecisionDiagnostic(
                    code="invalid_json",
                    stage=stage,
                    kind="format",
                    summary="Return a valid JSON object.",
                    retryable=True,
                    error_type=error_type_for(exc),
                    exception_chain=exception_chain_for(exc),
                ),
                exc.proposal_context if isinstance(exc, _NonObjectDecision) else proposal_context,
                elapsed_ms=elapsed_ms, response_shape=response_shape, normalization=normalization,
            ) from exc
        metrics = {
            "elapsed_ms": max(0, int((time.perf_counter() - attempt_start) * 1000)),
            "response_shape": shallow_response_shape(raw),
        }
        if normalization_sink:
            metrics["normalization"] = normalization_sink[0].model_dump(mode="json")
        return candidate, metrics

    async def _ask_with_tenacity(
        self,
        stage: str,
        working: dict[str, Any],
        response_schema: dict[str, Any],
        system: str,
        budget: DecisionBudget,
        before_call: Callable[..., Any] | None = None,
        validate_response: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        is_repair = False

        def exhausted() -> DecisionFailure:
            diagnostic = DecisionDiagnostic(
                code=budget.exhaustion_code(repair=is_repair),
                stage=stage,
                kind="budget",
                attempts=budget.used,
                summary="The shared decision attempt budget is exhausted.",
                retryable=False,
                last_cause=budget.last_failure,
                **budget.diagnostic_details(),
                location=budget.last_failure.location if budget.last_failure else "",
            )
            return DecisionFailure(stage, "budget", budget.used, diagnostic=diagnostic)

        async def run_guard() -> None:
            if before_call is None:
                return
            try:
                guard_result = before_call()
                if inspect.isawaitable(guard_result):
                    await guard_result
            except DecisionFailure:
                raise
            except ReasoningHalted as halted:
                diagnostic = halted.diagnostic.model_copy(update={
                    "stage": stage, "attempts": budget.used, "last_cause": budget.last_failure,
                    "error_type": error_type_for(halted),
                    "exception_chain": exception_chain_for(halted),
                })
                budget.last_failure = diagnostic
                raise DecisionFailure(
                    stage, "budget", budget.used, diagnostic=diagnostic
                ) from halted
            except Exception as exc:
                diagnostic = DecisionDiagnostic(
                    code="reasoning_guard_blocked",
                    stage=stage,
                    kind="budget",
                    attempts=budget.used,
                    summary="Model reasoning is not allowed at this time.",
                    retryable=False,
                    error_type=error_type_for(exc),
                    exception_chain=exception_chain_for(exc),
                )
                budget.last_failure = diagnostic
                raise DecisionFailure(stage, "budget", budget.used, diagnostic=diagnostic) from exc

        if not budget.permits():
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
                if not budget.permits(repair=is_repair):
                    raise exhausted()
                # Non-metered transports check the guard here; metered ones
                # also re-check it AFTER their semaphore/cooldown wait (see
                # ``_metered_guard_scope``), so exhaustion while waiting for a
                # slot still stops with a structured budget error.
                await run_guard()
                budget.consume(stage=stage, repair=is_repair)
                budget.record(stage, "request", feedback=safe_feedback(feedback))
                with attempt:
                    try:
                        result, metrics = await self._attempt_once(
                            stage=stage,
                            working=working,
                            response_schema=response_schema,
                            system=system,
                            feedback=feedback,
                            kind=kind,
                            budget=budget,
                            guard=before_call,
                        )
                        if validate_response is not None:
                            try:
                                validate_response(result)
                            except DecisionValidationError as rejected:
                                raise _RetryableDecisionError(
                                    rejected.diagnostic.model_copy(update={"stage": stage}),
                                ) from rejected
                        budget.record(
                            stage, "response_validated",
                            **metrics,
                            **decision_context(working, result, response_schema=response_schema),
                        )
                        return result
                    except _RetryableDecisionError as exc:
                        budget.record_failure(exc.diagnostic.code)
                        budget.last_failure = exc.diagnostic.model_copy(
                            update={"attempts": budget.used}
                        )
                        feedback = exc.feedback
                        kind = exc.kind
                        budget.record(
                            stage, "response_rejected", reason=exc.diagnostic.feedback(),
                            validation_issues=exc.diagnostic.validation_issues,
                            elapsed_ms=exc.elapsed_ms,
                            response_shape=exc.response_shape,
                            **exc.proposal_context,
                        )
                        if not exc.diagnostic.retryable:
                            raise DecisionFailure(
                                stage, exc.kind, budget.used, diagnostic=budget.last_failure
                            ) from exc
                        is_repair = True
                        raise
        except _RetryableDecisionError as exc:
            raise exhausted() from exc
        raise exhausted()
