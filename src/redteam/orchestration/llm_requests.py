"""Run-wide request accounting in the existing atomic coordinator records.

Each facade-backed request may retain a bounded, redacted copy of the public
prompt and public model response for operator inspection. Provider reasoning,
continuation ciphertext and secrets are never retained. Reservations are not
time-expired: recovering an execution slot must never refund a possibly
dispatched request. A recovered unknown remains charged.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from dataclasses import asdict
from typing import Any, Literal, Protocol

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import new_id
from redteam.core.ports.llm import LLMProviderFailure, LLMUsage, StructuredCompletion
from redteam.core.request_scope import RequestAttemptKind, RequestPurpose
from redteam.orchestration.cognition import ReasoningHalted
from redteam.orchestration.coordination import CoordinationError, Coordinator
from redteam.orchestration.diagnostics import DecisionDiagnostic

_REQUEST_STATES = ("reserved", "dispatched", "settled", "unknown", "released")
_FACADE_OUTCOMES = ("cancelled", "timeout", "exception")
_PROVIDER_OUTCOME_KEYS = frozenset({
    "category", "source", "kind", "http_status", "provider_code", "retryable", "retry_after",
})
# Optional Unix UTC seconds from the ledger clock (float). Rows written before these
# fields existed have none; readers must not infer them.
_TIME_FIELDS = ("dispatched_at", "settled_at")


def outcome_for_exception(exc: BaseException) -> dict[str, Any]:
    """Safe settlement category for an unknown row: no type name, message or body."""
    if isinstance(exc, LLMProviderFailure):
        return {"category": "provider_failure", "source": "facade", "kind": exc.kind,
                "http_status": exc.http_status, "provider_code": exc.provider_code,
                "retryable": exc.retryable, "retry_after": exc.retry_after}
    if isinstance(exc, asyncio.CancelledError):
        return {"category": "cancelled", "source": "facade"}
    # Python 3.10 keeps asyncio.TimeoutError distinct from the builtin TimeoutError.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return {"category": "timeout", "source": "facade"}
    return {"category": "exception", "source": "facade"}


def _valid_outcome(outcome: Any, state: Any) -> bool:
    """Accept only the sanitized shapes written above; anything else is malformed."""
    if state != "unknown" or not isinstance(outcome, dict):
        return False
    category, source = outcome.get("category"), outcome.get("source")
    if category == "outcome_lost":
        return source == "recovery" and set(outcome) == {"category", "source"}
    if source != "facade":
        return False
    if category in _FACADE_OUTCOMES:
        return set(outcome) == {"category", "source"}
    if category != "provider_failure" or set(outcome) != _PROVIDER_OUTCOME_KEYS:
        return False
    if type(outcome["retryable"]) is not bool:
        return False
    try:
        failure = LLMProviderFailure(
            kind=outcome["kind"], http_status=outcome["http_status"],
            provider_code=outcome["provider_code"], retryable=outcome["retryable"],
            retry_after=outcome["retry_after"],
        )
    except Exception:
        return False
    return (failure.kind == outcome["kind"]
            and failure.http_status == outcome["http_status"]
            and failure.provider_code == outcome["provider_code"]
            and failure.retry_after == outcome["retry_after"])


def _unknown_outcome_counts(rows) -> dict[str, int]:
    """Unknown rows by stored category; rows written before this field stay unrecorded."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["state"] == "unknown":
            category = (row.get("outcome") or {}).get("category", "unrecorded")
            counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


_COOLDOWN_STATES = ("open", "cooldown", "half_open", "held")
_PROVIDER_OUTCOMES = (
    "success", "rate_limited", "auth", "invalid_request", "server", "network", "timeout",
    "unknown",
)
_PROVIDER_DETAIL_FIELDS = (
    "provider_status", "provider_code", "provider_retryable", "provider_retry_after",
)
_COOLDOWN_VERSION = 2
_REQUEST_PURPOSES = (
    "planning", "atomization", "worker_decision", "worker_external_tool_decision",
    "worker_local_read_decision", "worker_settlement", "worker_text_only_held",
    "review", "conversation", "other",
)
_ATTEMPT_KINDS = ("normal", "format_repair", "semantic_repair", "transport_retry")
_DECISION_OUTCOMES = (
    "accepted", "response_received", "format_rejected", "semantic_rejected",
    "protocol_rejected", "invalid_tool_arguments", "unknown_tool", "invalid_tool_call",
    "text_only_held", "transport_failure",
)

# Large enough to inspect the real planner/worker exchange while keeping one
# Run's durable request ledger bounded. These are display records, not replay
# inputs; truncation is explicit and never changes provider execution.
LLM_REQUEST_CONTENT_LIMIT = 65_536
LLM_RESPONSE_CONTENT_LIMIT = 32_768


def _exchange_preview(value: Any, *, limit: int) -> tuple[str, bool]:
    """Return deterministic, redacted operator-visible text and truncation state.

    JSON strings are parsed before redaction so secret-shaped nested keys are
    removed instead of being hidden inside an opaque string. Ordinary text is
    still passed through the shared free-text redactor by
    ``redact_secret_material``.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            cleaned: Any = redact_secret_material(value)
        else:
            cleaned = redact_secret_material(parsed)
    else:
        cleaned = redact_secret_material(value)
    if isinstance(cleaned, str):
        text = cleaned
    else:
        text = json.dumps(
            cleaned, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
        )
    truncated = len(text) > limit
    return (text[:limit] if truncated else text), truncated


def request_usage(rows) -> dict[str, Any]:
    """Known sums plus unknown counts; cache subsets never change token charges."""
    rows = list(rows)
    spent = [row for row in rows if row["state"] in {"settled", "unknown"}]
    fields = asdict(LLMUsage())
    usage = {}
    for name in fields:
        values = [(row.get("usage") or {}).get(name) for row in spent]
        if name == "total_tokens":
            values = [row.get("total_tokens") for row in spent]
        usage[name] = {"known": sum(value for value in values if value is not None),
                       "unknown": sum(value is None for value in values)}
    purposes = {name: 0 for name in _REQUEST_PURPOSES}
    attempts = {name: 0 for name in _ATTEMPT_KINDS}
    outcomes = {name: 0 for name in _DECISION_OUTCOMES}
    provider_failures = 0
    unclassified_purpose = 0
    unclassified_attempt = 0
    unclassified_outcome = 0
    for row in spent:
        purpose = row.get("purpose")
        if purpose in purposes:
            purposes[purpose] += 1
        else:
            unclassified_purpose += 1
        attempt = row.get("attempt_kind")
        if attempt in attempts:
            attempts[attempt] += 1
        else:
            unclassified_attempt += 1
        decision_outcome = row.get("decision_outcome")
        if decision_outcome in outcomes:
            outcomes[decision_outcome] += 1
        else:
            unclassified_outcome += 1
        provider = row.get("provider_outcome")
        if provider is not None and provider != "success":
            provider_failures += 1
    return {
        "spent": len(spent),
        "reserved": sum(row["state"] in {"reserved", "dispatched"} for row in rows),
        "known_tokens": sum(row.get("total_tokens") or 0 for row in spent),
        "unknown_usage": sum(row.get("total_tokens") is None for row in spent),
        "unknown_dispatch": sum(row["state"] == "unknown" for row in spent),
        "purposes": purposes,
        "unclassified_purpose": unclassified_purpose,
        "attempts": attempts,
        "unclassified_attempt": unclassified_attempt,
        "outcomes": outcomes,
        "unclassified_outcome": unclassified_outcome,
        "repairs": attempts["format_repair"] + attempts["semantic_repair"],
        "provider_failures": provider_failures,
        "external_tool_decisions": purposes["worker_external_tool_decision"],
        "local_reads": purposes["worker_local_read_decision"],
        "settlements": purposes["worker_settlement"],
        "text_only_held": purposes["worker_text_only_held"],
        "no_progress": sum(row.get("made_progress") is False for row in spent),
        "usage": usage,
    }


def _group_usage(rows, field: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get(field) or "unknown", []).append(row)
    return {key: request_usage(value) for key, value in sorted(grouped.items())}


def request_stage_usage(rows) -> dict[str, dict[str, Any]]:
    """Derived attribution from recorded stages, never inferred from owners/methods."""
    return _group_usage(rows, "stage")


class RequestSession(Protocol):
    """Only the existing request ledger's records and Run admission boundary."""

    @property
    def records(self) -> MutableMapping[tuple[str, str], Any]: ...

    def admit(self, run_id: str, mission_id: str, *, token: str | None = None) -> None: ...


RequestChange = Callable[[RequestSession], Coroutine[Any, Any, Any]]


class RequestAdmissionError(ReasoningHalted):
    """A fixed safe admission reason with an optional bounded retry condition."""

    def __init__(
        self,
        reason_code: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        provider_status: int | None = None,
        provider_code: str | None = None,
        repair_condition: str = "",
    ) -> None:
        termination_reason: Literal["budget_exhausted"] | None = (
            "budget_exhausted" if reason_code == "run_llm_request_limit_exhausted" else None
        )
        super().__init__(
            reason_code, termination_reason=termination_reason,
        )
        self.diagnostic = DecisionDiagnostic(
            code=reason_code,
            stage="llm_request_admission",
            kind="budget",
            summary=reason_code,
            retryable=retryable,
            retry_after=retry_after,
            provider_status=provider_status,
            provider_code=provider_code,
            cause_scope="policy_wait",
            owner="orchestration.llm_requests",
            repair_condition=repair_condition,
            termination_reason=termination_reason,
        )


class RunRequestLedger:
    def __init__(
        self, coordinator: Coordinator | None = None, *, clock=time.time,
        change: Callable[[str, RequestChange], Awaitable[Any]] | None = None,
    ) -> None:
        if (coordinator is None) == (change is None):
            raise ValueError("select one request accounting transaction boundary")
        self._coordinator = coordinator
        self._request_change = change
        self.clock = clock

    def shares_boundary(self, change: Callable) -> bool:
        return self._request_change == change

    @property
    def coordinator(self) -> Coordinator:
        if self._coordinator is None:
            raise RequestAdmissionError("central_request_ledger_requires_central_runtime")
        return self._coordinator

    async def _change(self, run_id: str, callback: RequestChange) -> Any:
        if self._request_change is not None:
            return await self._request_change(run_id, callback)

        async def legacy(tx):
            return await callback(tx)

        return await self.coordinator.change(legacy)

    @staticmethod
    def _safe_cooldown_number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        return number

    @classmethod
    def checked_cooldown(cls, saved: Any) -> dict[str, Any]:
        """Read legacy v1 rows and validate the durable v2 state machine.

        Legacy rows were ``{consecutive, until, held}``. They remain readable,
        but are written as v2 on the next transition. An expired legacy
        consecutive limit therefore becomes recoverable instead of permanent.
        """
        if saved is None:
            saved = {}
        if not isinstance(saved, dict):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        if saved.get("version") is None:
            consecutive = saved.get("consecutive", 0)
            if (isinstance(consecutive, bool) or not isinstance(consecutive, int)
                    or consecutive < 0):
                raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
            until = cls._safe_cooldown_number(saved.get("until", 0.0))
            held = saved.get("held", False)
            if not isinstance(held, bool):
                raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
            limited = bool(consecutive or until or held)
            return {
                "version": _COOLDOWN_VERSION,
                "state": "held" if held else (
                    "cooldown" if consecutive or until else "open"
                ),
                "generation": 0,
                "consecutive": consecutive,
                "until": until,
                "recovery_request_id": None,
                "last_failure": ({
                    "kind": "rate_limited",
                    "provider_status": 429,
                    "provider_code": "unknown",
                    "retryable": not held,
                    "retry_after": None,
                } if limited else None),
            }
        if saved.get("version") != _COOLDOWN_VERSION:
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        state = saved.get("state")
        generation = saved.get("generation")
        consecutive = saved.get("consecutive")
        recovery = saved.get("recovery_request_id")
        if state not in _COOLDOWN_STATES:
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        if (isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
                or isinstance(consecutive, bool) or not isinstance(consecutive, int)
                or consecutive < 0):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        until = cls._safe_cooldown_number(saved.get("until"))
        if recovery is not None and (not isinstance(recovery, str) or not recovery.strip()):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        if (state == "half_open") != (recovery is not None):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        last = saved.get("last_failure")
        if last is not None:
            if not isinstance(last, dict) or set(last) != {
                "kind", "provider_status", "provider_code", "retryable", "retry_after",
            }:
                raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
            checked = LLMProviderFailure(
                kind=last["kind"], http_status=last["provider_status"],
                provider_code=last["provider_code"], retryable=last["retryable"],
                retry_after=last["retry_after"],
            )
            if checked.diagnostic_fields() != {
                "provider_status": last["provider_status"],
                "provider_code": last["provider_code"],
                "retry_after": last["retry_after"],
            } or checked.kind != last["kind"] or checked.retryable is not last["retryable"]:
                raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        if state == "open" and (consecutive != 0 or until != 0 or last is not None):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        if state != "open" and (consecutive < 1 or last is None):
            raise RequestAdmissionError("llm_provider_cooldown_record_invalid")
        return {
            "version": _COOLDOWN_VERSION,
            "state": state,
            "generation": generation,
            "consecutive": consecutive,
            "until": until,
            "recovery_request_id": recovery,
            "last_failure": last,
        }

    @staticmethod
    def _provider_failure_payload(failure: LLMProviderFailure) -> dict[str, Any]:
        """Safe per-request diagnostics; never provider bodies, headers or messages."""
        return {
            "provider_status": failure.provider_status,
            "provider_code": failure.provider_code,
            "provider_retryable": failure.retryable,
            "provider_retry_after": failure.retry_after,
        }

    @classmethod
    def _checked_provider_failure_details(
        cls, row: dict[str, Any], outcome: Any,
    ) -> dict[str, Any] | None:
        present = {field for field in _PROVIDER_DETAIL_FIELDS if field in row}
        if not present:
            return None  # additive compatibility with historical request rows
        if present != set(_PROVIDER_DETAIL_FIELDS) or outcome in {None, "success"}:
            raise RequestAdmissionError("llm_request_record_invalid")
        if type(row["provider_retryable"]) is not bool:
            raise RequestAdmissionError("llm_request_record_invalid")
        checked = LLMProviderFailure(
            kind=outcome,
            http_status=row["provider_status"],
            provider_code=row["provider_code"],
            retryable=row["provider_retryable"],
            retry_after=row["provider_retry_after"],
        )
        payload = cls._provider_failure_payload(checked)
        if payload != {field: row[field] for field in _PROVIDER_DETAIL_FIELDS}:
            raise RequestAdmissionError("llm_request_record_invalid")
        return payload

    @classmethod
    def _cooldown_failure_payload(cls, failure: LLMProviderFailure) -> dict[str, Any]:
        details = cls._provider_failure_payload(failure)
        return {
            "kind": failure.kind,
            "provider_status": details["provider_status"],
            "provider_code": details["provider_code"],
            "retryable": details["provider_retryable"],
            "retry_after": details["provider_retry_after"],
        }

    @staticmethod
    def _cooldown_error(
        reason: str, state: dict[str, Any], *, retryable: bool, retry_after: float | None,
    ) -> RequestAdmissionError:
        failure = state.get("last_failure") or {}
        condition = (
            "Wait for the provider cooldown and retry this Run."
            if retryable else
            "Review provider availability and explicitly start a new authorized Run."
        )
        return RequestAdmissionError(
            reason,
            retryable=retryable,
            retry_after=retry_after,
            provider_status=failure.get("provider_status"),
            provider_code=failure.get("provider_code"),
            repair_condition=condition,
        )

    def _cooldown(
        self,
        tx: RequestSession,
        run_id: str,
        policy: LLMRequestPolicy,
        *,
        claim_request_id: str | None = None,
        active_request_id: str | None = None,
    ) -> tuple[float, dict[str, Any]]:
        key = ("llm_provider_cooldown", run_id)
        state = self.checked_cooldown(tx.records.get(key))
        if state["state"] == "held":
            raise self._cooldown_error(
                "llm_rate_limit_requires_operator_review", state,
                retryable=False, retry_after=None,
            )
        remaining = max(0.0, state["until"] - self.clock())
        if remaining:
            return remaining, state
        if state["state"] == "half_open":
            if active_request_id == state["recovery_request_id"]:
                return 0.0, state
            raise self._cooldown_error(
                "llm_rate_limit_recovery_in_progress", state,
                retryable=True, retry_after=0.0,
            )
        if state["consecutive"] >= policy.max_consecutive_rate_limits:
            if claim_request_id is not None:
                state = {
                    **state,
                    "state": "half_open",
                    "recovery_request_id": claim_request_id,
                }
                tx.records[key] = state
            return 0.0, state
        return 0.0, state

    async def wait_seconds(self, run_id: str) -> float:
        async def read(tx: RequestSession) -> float:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            return self._cooldown(
                tx, run_id, LLMRequestPolicy.model_validate(saved),
            )[0]
        return await self._change(run_id, read)

    @classmethod
    def _request_generation(cls, row: dict[str, Any]) -> int | None:
        value = row.get("provider_generation")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RequestAdmissionError("llm_request_record_invalid")
        return value

    def _apply_provider_outcome(
        self,
        tx: RequestSession,
        run_id: str,
        request_id: str,
        failure: LLMProviderFailure | None,
    ) -> None:
        row = self._get(tx, run_id, request_id)
        outcome = "success" if failure is None else failure.kind
        previous_outcome = row.get("provider_outcome")
        if previous_outcome is not None:
            if previous_outcome == outcome:
                if failure is not None:
                    expected = self._provider_failure_payload(failure)
                    saved = self._checked_provider_failure_details(row, previous_outcome)
                    if saved is not None and saved != expected:
                        raise RequestAdmissionError("llm_provider_outcome_conflict")
                return
            raise RequestAdmissionError("llm_provider_outcome_conflict")
        row["provider_outcome"] = outcome
        if failure is not None:
            row.update(self._provider_failure_payload(failure))
        if failure is None:
            row.setdefault("decision_outcome", "response_received")
        if failure is not None and failure.kind != "rate_limited":
            row.setdefault("decision_outcome", "transport_failure")
            row.setdefault("decision_valid", False)
            key = ("llm_provider_cooldown", run_id)
            state = self.checked_cooldown(tx.records.get(key))
            if (state["state"] == "half_open"
                    and state["recovery_request_id"] == request_id):
                tx.records[key] = {
                    **state,
                    "state": "held",
                    "recovery_request_id": None,
                }
            return
        key = ("llm_provider_cooldown", run_id)
        state = self.checked_cooldown(tx.records.get(key))
        request_generation = self._request_generation(row)
        if failure is None:
            # Only a request dispatched in the latest provider generation may
            # clear it. A sibling dispatched before a newer 429 is a late
            # success, not proof that the provider recovered.
            if request_generation is None or request_generation != state["generation"]:
                return
            if (state["state"] == "half_open"
                    and state["recovery_request_id"] != request_id):
                return
            if state["state"] == "held":
                return
            tx.records[key] = {
                **state,
                "state": "open",
                "consecutive": 0,
                "until": 0.0,
                "recovery_request_id": None,
                "last_failure": None,
            }
            return
        policy = LLMRequestPolicy.model_validate(tx.records[("llm_request_policy", run_id)])
        row.setdefault("decision_outcome", "transport_failure")
        row.setdefault("decision_valid", False)
        count = state["consecutive"] + 1
        delay = failure.retry_after
        if delay is None:
            delay = min(
                policy.max_rate_limit_wait_seconds,
                policy.rate_limit_base_seconds * 2 ** min(count - 1, 30),
            )
        held = not failure.retryable or delay > policy.max_rate_limit_wait_seconds
        tx.records[key] = {
            "version": _COOLDOWN_VERSION,
            "state": "held" if held else "cooldown",
            "generation": state["generation"] + 1,
            "consecutive": count,
            "until": max(state["until"], self.clock() + delay),
            "recovery_request_id": None,
            "last_failure": self._cooldown_failure_payload(failure),
        }

    async def record_provider_outcome(
        self, run_id: str, request_id: str, failure: LLMProviderFailure | None = None,
    ) -> None:
        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            expected = "settled" if failure is None else "unknown"
            if row["state"] != expected:
                raise RequestAdmissionError("llm_provider_outcome_before_settlement")
            self._apply_provider_outcome(tx, run_id, request_id, failure)
        await self._change(run_id, change)

    async def bind(self, run_id: str, policy: LLMRequestPolicy) -> None:
        async def change(tx: RequestSession) -> None:
            key = ("llm_request_policy", run_id)
            payload = policy.model_dump(mode="json")
            saved = tx.records.get(key)
            if saved is not None and saved != payload:
                raise RequestAdmissionError("llm_request_policy_changed_new_run_required")
            tx.records[key] = payload
        await self._change(run_id, change)

    @staticmethod
    def checked_record(request_id: str, row: Any) -> dict[str, Any]:
        """Validate the persisted identity/state before accounting or mutation.

        No row repair, inferred usage or refund: malformed history is held.
        ``kind`` remains an open, nonblank method label, not a role allowlist.
        """
        if (not isinstance(row, dict)
                or any(not isinstance(row.get(key), str) or not row[key].strip()
                       for key in ("request_id", "run_id", "owner", "kind"))
                or row["request_id"] != request_id
                or not isinstance(row.get("state"), str) or row["state"] not in _REQUEST_STATES
                or "total_tokens" not in row):
            raise RequestAdmissionError("llm_request_record_invalid")
        tokens = row["total_tokens"]
        for field in ("stage", "task_id"):
            if row.get(field) is not None and (
                not isinstance(row[field], str) or not row[field].strip()
            ):
                raise RequestAdmissionError("llm_request_record_invalid")
        if (tokens is not None and (type(tokens) is not int or tokens < 0
                                    or row["state"] not in {"settled", "unknown"})):
            raise RequestAdmissionError("llm_request_record_invalid")
        if "outcome" in row and not _valid_outcome(row["outcome"], row["state"]):
            raise RequestAdmissionError("llm_request_record_invalid")
        if any(key in row and (type(row[key]) not in (int, float)
                               or not math.isfinite(row[key]) or row[key] < 0)
               for key in _TIME_FIELDS):
            raise RequestAdmissionError("llm_request_record_invalid")
        if row.get("usage") is not None:
            try:
                if not isinstance(row["usage"], dict) or row["state"] not in {"settled", "unknown"}:
                    raise ValueError("usage outside settlement")
                usage = LLMUsage(**row["usage"])
                if usage.total_tokens is not None and usage.total_tokens != tokens:
                    raise ValueError("usage total mismatch")
            except (TypeError, ValueError) as exc:
                raise RequestAdmissionError("llm_request_record_invalid") from exc
        generation = row.get("provider_generation")
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 0
        ):
            raise RequestAdmissionError("llm_request_record_invalid")
        outcome = row.get("provider_outcome")
        if outcome is not None and outcome not in _PROVIDER_OUTCOMES:
            raise RequestAdmissionError("llm_request_record_invalid")
        RunRequestLedger._checked_provider_failure_details(row, outcome)
        purpose = row.get("purpose")
        attempt_kind = row.get("attempt_kind")
        decision_outcome = row.get("decision_outcome")
        if purpose is not None and purpose not in _REQUEST_PURPOSES:
            raise RequestAdmissionError("llm_request_record_invalid")
        if attempt_kind is not None and attempt_kind not in _ATTEMPT_KINDS:
            raise RequestAdmissionError("llm_request_record_invalid")
        if decision_outcome is not None and decision_outcome not in _DECISION_OUTCOMES:
            raise RequestAdmissionError("llm_request_record_invalid")
        for field in ("decision_valid", "made_progress"):
            if row.get(field) is not None and not isinstance(row[field], bool):
                raise RequestAdmissionError("llm_request_record_invalid")
        for prefix, limit in (
            ("request", LLM_REQUEST_CONTENT_LIMIT),
            ("response", LLM_RESPONSE_CONTENT_LIMIT),
        ):
            content_key, truncated_key = f"{prefix}_content", f"{prefix}_truncated"
            present = {key for key in (content_key, truncated_key) if key in row}
            if present and present != {content_key, truncated_key}:
                raise RequestAdmissionError("llm_request_record_invalid")
            if present and (
                not isinstance(row[content_key], str)
                or len(row[content_key]) > limit
                or type(row[truncated_key]) is not bool
            ):
                raise RequestAdmissionError("llm_request_record_invalid")
        sequence = row.get("request_sequence")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
        ):
            raise RequestAdmissionError("llm_request_record_invalid")
        circuit_key = row.get("task_circuit_key")
        if circuit_key is not None and (
            not isinstance(circuit_key, str) or not circuit_key.strip()
        ):
            raise RequestAdmissionError("llm_request_record_invalid")
        return row

    @classmethod
    def _rows(cls, tx: RequestSession, run_id: str) -> list[dict[str, Any]]:
        rows = []
        for (kind, request_id), row in tx.records.items():
            if kind != "llm_request":
                continue
            # Without a Run identity the row cannot safely be excluded from
            # this inventory. A well-scoped other Run stays isolated.
            if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                    or not row["run_id"].strip()):
                raise RequestAdmissionError("llm_request_record_invalid")
            if row["run_id"] == run_id:
                rows.append(cls.checked_record(request_id, row))
        return rows

    @classmethod
    def owner_has_activity(cls, tx: RequestSession, run_id: str, owner: str) -> bool:
        """Even a released reservation contradicts a never-entered actor proof.

        Read-only: recovery never refunds requests or resets the shared policy.
        """
        return any(row["owner"] == owner for row in cls._rows(tx, run_id))

    @classmethod
    def request_states(
        cls, tx: RequestSession, run_id: str, *, owner: str | None = None,
    ) -> dict[str, int]:
        """Safe read-only projection, not refunds or unknown settlement.

        Reserved/dispatched/unknown remain distinct. A zero count is only an
        observation of this ledger; it cannot prove no external execution.
        """
        counts = dict.fromkeys(_REQUEST_STATES, 0)
        for row in cls._rows(tx, run_id):
            if owner is None or row["owner"] == owner:
                counts[row["state"]] += 1
        return counts

    @staticmethod
    def _admit(tx: RequestSession, run_id: str) -> None:
        binding = tx.records.get(("binding", run_id))
        if binding is not None:
            try:
                tx.admit(run_id, binding["mission_id"])
            except CoordinationError as exc:
                raise RequestAdmissionError("llm_run_not_admitted") from exc

    async def reserve(
        self, run_id: str, *, owner: str, kind: str, task_id: str | None = None,
        stage: str | None = None, purpose: RequestPurpose | None = None,
        attempt_kind: RequestAttemptKind = "normal",
        request_payload: Any | None = None,
    ) -> str:
        if any(not isinstance(value, str) or not value.strip() for value in (run_id, owner, kind)):
            raise RequestAdmissionError("llm_request_scope_invalid")
        if stage is not None and (not isinstance(stage, str) or not stage.strip()):
            raise RequestAdmissionError("llm_request_scope_invalid")
        if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
            raise RequestAdmissionError("llm_request_scope_invalid")
        if purpose is not None and purpose not in _REQUEST_PURPOSES:
            raise RequestAdmissionError("llm_request_scope_invalid")
        if attempt_kind not in _ATTEMPT_KINDS:
            raise RequestAdmissionError("llm_request_scope_invalid")
        request_id = new_id("llm_request")

        async def change(tx: RequestSession) -> str:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            policy = LLMRequestPolicy.model_validate(saved)
            delay, cooldown = self._cooldown(tx, run_id, policy)
            if delay > 0:
                raise self._cooldown_error(
                    "llm_provider_cooldown_active", cooldown,
                    retryable=True, retry_after=delay,
                )
            rows = self._rows(tx, run_id)
            task_guard = None
            if task_id is not None:
                guard = getattr(tx, "admit_probe_request", None)
                if guard is None:
                    raise RequestAdmissionError("probe_request_admission_not_bound")
                task_guard = guard(task_id, rows, attempt_kind=attempt_kind)
            charged = sum(row["state"] != "released" for row in rows)
            if policy.max_requests is not None and charged >= policy.max_requests:
                raise RequestAdmissionError("run_llm_request_limit_exhausted")
            active = sum(row["state"] in {"reserved", "dispatched"} for row in rows)
            if active >= policy.max_concurrent_requests:
                raise RequestAdmissionError("run_llm_concurrency_unavailable")
            # Claim the sole post-threshold recovery request only after every
            # other admission check passed, so a failed reserve cannot strand
            # half-open state pointing at a nonexistent request.
            _, cooldown = self._cooldown(
                tx, run_id, policy, claim_request_id=request_id,
            )
            request_preview = (
                _exchange_preview(request_payload, limit=LLM_REQUEST_CONTENT_LIMIT)
                if request_payload is not None else None
            )
            tx.records[("llm_request", request_id)] = {
                "request_id": request_id, "run_id": run_id, "owner": owner,
                "kind": kind, "state": "reserved", "total_tokens": None,
                "provider_generation": cooldown["generation"],
                "request_sequence": max(
                    (row.get("request_sequence", 0) for row in rows), default=0,
                ) + 1,
                "attempt_kind": attempt_kind,
                **({"purpose": purpose} if purpose is not None else {}),
                **({"task_circuit_key": task_guard["circuit_key"]}
                   if isinstance(task_guard, dict) else {}),
                **({"task_id": task_id} if task_id is not None else {}),
                **({"stage": stage} if stage is not None else {}),
                **({
                    "request_content": request_preview[0],
                    "request_truncated": request_preview[1],
                } if request_preview is not None else {}),
            }
            return request_id
        return await self._change(run_id, change)

    async def classify_decision(
        self,
        run_id: str,
        request_id: str,
        *,
        purpose: RequestPurpose | None = None,
        outcome: str,
        valid: bool,
        made_progress: bool | None = None,
    ) -> None:
        """Finalize code-known semantics for one settled response.

        The caller classifies validated protocol/tool bindings, never response
        prose. Re-delivery is idempotent and incompatible rewrites fail closed.
        """
        if purpose is not None and purpose not in _REQUEST_PURPOSES:
            raise RequestAdmissionError("llm_request_classification_invalid")
        if outcome not in _DECISION_OUTCOMES:
            raise RequestAdmissionError("llm_request_classification_invalid")

        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            if row["state"] not in {"settled", "unknown"}:
                raise RequestAdmissionError("llm_request_classification_before_settlement")
            proposed = {
                "decision_outcome": outcome,
                "decision_valid": valid,
                **({"purpose": purpose} if purpose is not None else {}),
                **({"made_progress": made_progress} if made_progress is not None else {}),
            }
            for name, value in proposed.items():
                previous = row.get(name)
                worker_refinement = (
                    name == "purpose" and previous == "worker_decision"
                    and isinstance(value, str) and value.startswith("worker_")
                )
                outcome_refinement = (
                    name == "decision_outcome" and previous == "response_received"
                )
                if (previous is not None and previous != value
                        and not worker_refinement and not outcome_refinement):
                    raise RequestAdmissionError("llm_request_classification_conflict")
            row.update(proposed)

        await self._change(run_id, change)

    async def mark_progress(
        self, run_id: str, request_id: str, *, made_progress: bool,
    ) -> None:
        """Finalize whether the validated decision produced new canonical DATA."""
        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            if row["state"] not in {"settled", "unknown"} or row.get("decision_valid") is not True:
                raise RequestAdmissionError("llm_request_progress_before_valid_decision")
            previous = row.get("made_progress")
            if previous is not None and previous != made_progress:
                raise RequestAdmissionError("llm_request_progress_conflict")
            row["made_progress"] = made_progress

        await self._change(run_id, change)

    def launch_blocked(self, tx: RequestSession, run_id: str) -> str | None:
        """Non-reserving Run gate; a new instance cannot evade count/backoff."""
        saved = tx.records.get(("llm_request_policy", run_id))
        if saved is None:
            return None  # legacy activation API remains compatible
        policy = LLMRequestPolicy.model_validate(saved)
        charged = sum(row["state"] != "released" for row in self._rows(tx, run_id))
        if policy.max_requests is not None and charged >= policy.max_requests:
            return "run_llm_request_limit_exhausted"
        try:
            delay, _ = self._cooldown(tx, run_id, policy)
        except RequestAdmissionError as exc:
            return exc.reason_code
        if delay > 0:
            return "llm_provider_cooldown_active"
        return None

    async def dispatch(self, run_id: str, request_id: str) -> None:
        async def change(tx: RequestSession) -> None:
            self._admit(tx, run_id)
            row = self._get(tx, run_id, request_id)
            if row["state"] != "reserved":
                raise RequestAdmissionError("llm_request_not_reserved")
            policy = LLMRequestPolicy.model_validate(tx.records[("llm_request_policy", run_id)])
            delay, cooldown = self._cooldown(
                tx, run_id, policy, claim_request_id=request_id,
                active_request_id=request_id,
            )
            if delay > 0:
                raise self._cooldown_error(
                    "llm_provider_cooldown_active", cooldown,
                    retryable=True, retry_after=delay,
                )
            row["provider_generation"] = cooldown["generation"]
            row["state"] = "dispatched"
            row["dispatched_at"] = self.clock()
        await self._change(run_id, change)

    @classmethod
    def _get(cls, tx: RequestSession, run_id: str, request_id: str) -> dict[str, Any]:
        row = tx.records.get(("llm_request", request_id))
        if row is None:
            raise RequestAdmissionError("llm_request_identity_mismatch")
        row = cls.checked_record(request_id, row)
        if row["run_id"] != run_id:
            raise RequestAdmissionError("llm_request_identity_mismatch")
        return row

    async def settle(
        self, run_id: str, request_id: str, *, unknown: bool = False,
        total_tokens: int | None = None, outcome: dict[str, Any] | None = None,
        usage: LLMUsage | None = None,
    ) -> None:
        measured = StructuredCompletion(value="", total_tokens=total_tokens, usage=usage)
        tokens = measured.total_tokens
        details = asdict(usage) if usage is not None else None

        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            state = "unknown" if unknown else "settled"
            if outcome is not None and (not unknown or not _valid_outcome(outcome, state)):
                raise RequestAdmissionError("llm_request_outcome_invalid")
            if (row["state"] == state and row["total_tokens"] == tokens
                    and row.get("usage") == details
                    and (outcome is None or row.get("outcome") == outcome)):
                return
            if row["state"] != "dispatched":
                raise RequestAdmissionError("llm_request_settlement_conflict")
            row.update(state=state, total_tokens=tokens, settled_at=self.clock())
            if details is not None:
                row["usage"] = details
            if outcome is not None:
                row["outcome"] = dict(outcome)
            if unknown:
                self._hold_unknown_recovery(tx, run_id, request_id)
        await self._change(run_id, change)

    def _release_recovery_claim(
        self, tx: RequestSession, run_id: str, request_id: str,
    ) -> None:
        key = ("llm_provider_cooldown", run_id)
        saved = tx.records.get(key)
        if saved is None:
            return
        state = self.checked_cooldown(saved)
        if state["state"] == "half_open" and state["recovery_request_id"] == request_id:
            tx.records[key] = {
                **state,
                "state": "cooldown",
                "recovery_request_id": None,
            }

    def _hold_unknown_recovery(
        self, tx: RequestSession, run_id: str, request_id: str,
    ) -> None:
        key = ("llm_provider_cooldown", run_id)
        saved = tx.records.get(key)
        if saved is None:
            return
        state = self.checked_cooldown(saved)
        if state["state"] == "half_open" and state["recovery_request_id"] == request_id:
            # The recovery request may have reached the provider. Replaying it
            # would violate at-most-once request accounting, so the same Run
            # requires operator review rather than silently opening the gate.
            tx.records[key] = {
                **state,
                "state": "held",
                "recovery_request_id": None,
            }

    async def settle_provider_outcome(
        self,
        run_id: str,
        request_id: str,
        *,
        failure: LLMProviderFailure | None = None,
        total_tokens: int | None = None,
        usage: LLMUsage | None = None,
        response_payload: Any | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        """Atomically settle one dispatched request and advance provider state.

        A provider failure has unknown usage. A successful response may carry
        request-local usage. Keeping both mutations in one coordinator change
        prevents a crash from persisting the request result without its 429
        cooldown (or vice versa).
        """
        measured = StructuredCompletion(value="", total_tokens=total_tokens, usage=usage)
        tokens = measured.total_tokens
        details = asdict(usage) if usage is not None else None
        response_preview = (
            _exchange_preview(response_payload, limit=LLM_RESPONSE_CONTENT_LIMIT)
            if failure is None and response_payload is not None else None
        )
        if failure is None:
            if outcome is not None:
                raise RequestAdmissionError("llm_request_outcome_invalid")
            outcome_payload = None
        else:
            outcome_payload = dict(outcome or outcome_for_exception(failure))
            if not _valid_outcome(outcome_payload, "unknown"):
                raise RequestAdmissionError("llm_request_outcome_invalid")

        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            target_state = "unknown" if failure is not None else "settled"
            target_tokens = None if failure is not None else tokens
            target_usage = None if failure is not None else details
            provider_outcome = "success" if failure is None else failure.kind
            failure_details = (
                self._provider_failure_payload(failure) if failure is not None else None
            )
            if row["state"] in {"settled", "unknown"}:
                saved_failure_details = self._checked_provider_failure_details(
                    row, provider_outcome,
                )
                if (row["state"] == target_state and row["total_tokens"] == target_tokens
                        and row.get("usage") == target_usage
                        and row.get("provider_outcome") == provider_outcome
                        and (outcome_payload is None or row.get("outcome") == outcome_payload)
                        and (response_preview is None or (
                            row.get("response_content") == response_preview[0]
                            and row.get("response_truncated") is response_preview[1]
                        ))
                        and (failure_details is None or saved_failure_details is None
                             or saved_failure_details == failure_details)):
                    return
                raise RequestAdmissionError("llm_request_settlement_conflict")
            if row["state"] != "dispatched":
                raise RequestAdmissionError("llm_request_settlement_conflict")
            row.update(state=target_state, total_tokens=target_tokens, settled_at=self.clock())
            if target_usage is not None:
                row["usage"] = target_usage
            if outcome_payload is not None:
                row["outcome"] = dict(outcome_payload)
            if response_preview is not None:
                row["response_content"] = response_preview[0]
                row["response_truncated"] = response_preview[1]
            self._apply_provider_outcome(tx, run_id, request_id, failure)
        await self._change(run_id, change)

    async def release_undispatched(self, run_id: str, request_id: str) -> None:
        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            if row["state"] not in {"reserved", "released"}:
                raise RequestAdmissionError("dispatched_request_cannot_be_refunded")
            row["state"] = "released"
            self._release_recovery_claim(tx, run_id, request_id)
        await self._change(run_id, change)

    async def settle_interrupted_dispatch(
        self, run_id: str, request_id: str, *, outcome: dict[str, Any] | None = None,
    ) -> bool:
        """Resolve cancellation at the durable dispatch boundary.

        ``False`` means dispatch never committed and the reservation was safely
        released. ``True`` means the durable row had crossed into dispatched;
        its provider usage is conservatively unknown and remains charged.  The
        distinction is made in the same request transaction, so a timeout can
        never leave outcome classification racing a live row.
        """
        if outcome is not None and not _valid_outcome(outcome, "unknown"):
            raise RequestAdmissionError("llm_request_outcome_invalid")

        async def change(tx: RequestSession) -> bool:
            row = self._get(tx, run_id, request_id)
            if row["state"] in {"reserved", "released"}:
                row["state"] = "released"
                self._release_recovery_claim(tx, run_id, request_id)
                return False
            if row["state"] == "unknown":
                previous = row.get("outcome")
                if outcome is not None and previous is not None and previous != outcome:
                    raise RequestAdmissionError("llm_request_settlement_conflict")
                if outcome is not None and previous is None:
                    row["outcome"] = dict(outcome)
                self._hold_unknown_recovery(tx, run_id, request_id)
                return True
            if row["state"] == "dispatched":
                row.update(state="unknown", total_tokens=None, settled_at=self.clock())
                if outcome is not None:
                    row["outcome"] = dict(outcome)
                self._hold_unknown_recovery(tx, run_id, request_id)
                return True
            raise RequestAdmissionError("llm_request_dispatch_interruption_conflict")

        return await self._change(run_id, change)

    async def snapshot(self, run_id: str) -> dict[str, Any]:
        async def read(tx: RequestSession) -> dict[str, Any]:
            rows = self._rows(tx, run_id)
            saved_cooldown = tx.records.get(("llm_provider_cooldown", run_id))
            return {
                "policy": tx.records.get(("llm_request_policy", run_id)),
                **request_usage(rows),
                "provider_cooldown": (
                    self.checked_cooldown(saved_cooldown)
                    if saved_cooldown is not None else None
                ),
                "by_stage": request_stage_usage(rows),
                "unknown_outcomes": _unknown_outcome_counts(rows),
                "by_task": _group_usage(rows, "task_id"),
                "by_owner": _group_usage(rows, "owner"),
            }
        return await self._change(run_id, read)

    async def recover_owner(self, run_id: str, *, owner: str | None) -> None:
        """An exclusively owned, quiescent Run only: never refund dispatch.

        Reserved means the provider has not been entered. Dispatched means
        lost usage is unknown, retaining the original request and its charge.
        The driver must establish that the actor is no longer running first.
        """
        async def change(tx: RequestSession) -> None:
            for row in self._rows(tx, run_id):
                if owner is not None and row["owner"] != owner:
                    continue
                current = self._get(tx, run_id, row["request_id"])
                if current["state"] == "reserved":
                    current["state"] = "released"
                    self._release_recovery_claim(tx, run_id, row["request_id"])
                elif current["state"] == "dispatched":
                    current.update(
                        state="unknown", total_tokens=None,
                        outcome={"category": "outcome_lost", "source": "recovery"},
                    )
                    self._hold_unknown_recovery(tx, run_id, row["request_id"])
        await self._change(run_id, change)

    async def recover_run(self, run_id: str) -> None:
        """Exclusive cold Run recovery, including planner and verification scopes."""
        await self.recover_owner(run_id, owner=None)
