"""Run-wide request accounting in the existing atomic coordinator records.

No prompts, responses, secrets or private reasoning are retained. Reservations
are not time-expired: recovering an execution slot must never refund a possibly
dispatched request. A recovered unknown remains charged.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from typing import Any, Protocol

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ids import new_id
from redteam.core.ports.llm import LLMProviderFailure
from redteam.orchestration.cognition import ReasoningHalted
from redteam.orchestration.coordination import CoordinationError, Coordinator

_REQUEST_STATES = ("reserved", "dispatched", "settled", "unknown", "released")


def request_stage_usage(rows) -> dict[str, dict[str, int]]:
    """Derived attribution from recorded stages; never infer stages from owners/methods."""
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        stage = row.get("stage") or "unknown"
        bucket = grouped.setdefault(stage, {
            "spent": 0, "reserved": 0, "known_tokens": 0,
            "unknown_usage": 0, "unknown_dispatch": 0,
        })
        if row["state"] in {"settled", "unknown"}:
            bucket["spent"] += 1
            bucket["known_tokens"] += row["total_tokens"] or 0
            bucket["unknown_usage"] += row["total_tokens"] is None
            bucket["unknown_dispatch"] += row["state"] == "unknown"
        elif row["state"] in {"reserved", "dispatched"}:
            bucket["reserved"] += 1
    return dict(sorted(grouped.items()))


class RequestSession(Protocol):
    """Only the existing request ledger's records and Run admission boundary."""

    @property
    def records(self) -> MutableMapping[tuple[str, str], Any]: ...

    def admit(self, run_id: str, mission_id: str, *, token: str | None = None) -> None: ...


RequestChange = Callable[[RequestSession], Coroutine[Any, Any, Any]]


class RequestAdmissionError(ReasoningHalted):
    """A fixed safe reason; callers must not repair/retry an admission denial."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(
            reason_code, termination_reason=(
                "budget_exhausted" if reason_code == "run_llm_request_limit_exhausted" else None
            ),
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

    def _cooldown(self, tx: RequestSession, run_id: str, policy: LLMRequestPolicy) -> float:
        saved = tx.records.get(("llm_provider_cooldown", run_id), {})
        if saved.get("held"):
            raise RequestAdmissionError("llm_rate_limit_requires_operator_review")
        if saved.get("consecutive", 0) >= policy.max_consecutive_rate_limits:
            raise RequestAdmissionError("llm_consecutive_rate_limit_exhausted")
        return max(0.0, saved.get("until", 0.0) - self.clock())

    async def wait_seconds(self, run_id: str) -> float:
        async def read(tx: RequestSession) -> float:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            return self._cooldown(tx, run_id, LLMRequestPolicy.model_validate(saved))
        return await self._change(run_id, read)

    async def record_provider_outcome(
        self, run_id: str, failure: LLMProviderFailure | None = None,
    ) -> None:
        async def change(tx: RequestSession) -> None:
            key = ("llm_provider_cooldown", run_id)
            if failure is None:
                # Do not undo ANY exhausted Run-wide hold using an earlier
                # concurrently dispatched request's late success. A new facade
                # or successful sibling is not an operator reset of that hold.
                saved = tx.records.get(key, {})
                policy = LLMRequestPolicy.model_validate(
                    tx.records[("llm_request_policy", run_id)],
                )
                if (not saved.get("held") and saved.get("consecutive", 0)
                        < policy.max_consecutive_rate_limits):
                    tx.records[key] = {"consecutive": 0, "until": saved.get("until", 0.0)}
                return
            if failure.kind != "rate_limited":
                return
            policy = LLMRequestPolicy.model_validate(tx.records[("llm_request_policy", run_id)])
            previous = tx.records.get(key, {})
            count = previous.get("consecutive", 0) + 1
            delay = failure.retry_after
            if delay is None:
                delay = min(policy.max_rate_limit_wait_seconds,
                            policy.rate_limit_base_seconds * 2 ** min(count - 1, 30))
            tx.records[key] = {
                "consecutive": count,
                "until": max(previous.get("until", 0.0), self.clock() + delay),
                "held": bool(previous.get("held")) or not failure.retryable
                        or delay > policy.max_rate_limit_wait_seconds,
            }
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
        if row.get("stage") is not None and (
            not isinstance(row["stage"], str) or not row["stage"].strip()
        ):
            raise RequestAdmissionError("llm_request_record_invalid")
        if (tokens is not None and (type(tokens) is not int or tokens < 0
                                    or row["state"] not in {"settled", "unknown"})):
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
        stage: str | None = None,
    ) -> str:
        if any(not isinstance(value, str) or not value.strip() for value in (run_id, owner, kind)):
            raise RequestAdmissionError("llm_request_scope_invalid")
        if stage is not None and (not isinstance(stage, str) or not stage.strip()):
            raise RequestAdmissionError("llm_request_scope_invalid")
        request_id = new_id("llm_request")

        async def change(tx: RequestSession) -> str:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            policy = LLMRequestPolicy.model_validate(saved)
            if self._cooldown(tx, run_id, policy) > 0:
                raise RequestAdmissionError("llm_provider_cooldown_active")
            rows = self._rows(tx, run_id)
            if task_id is not None:
                guard = getattr(tx, "admit_probe_request", None)
                if guard is None:
                    raise RequestAdmissionError("probe_request_admission_not_bound")
                guard(task_id, rows)
            charged = sum(row["state"] != "released" for row in rows)
            if policy.max_requests is not None and charged >= policy.max_requests:
                raise RequestAdmissionError("run_llm_request_limit_exhausted")
            active = sum(row["state"] in {"reserved", "dispatched"} for row in rows)
            if active >= policy.max_concurrent_requests:
                raise RequestAdmissionError("run_llm_concurrency_unavailable")
            tx.records[("llm_request", request_id)] = {
                "request_id": request_id, "run_id": run_id, "owner": owner,
                "kind": kind, "state": "reserved", "total_tokens": None,
                **({"task_id": task_id} if task_id is not None else {}),
                **({"stage": stage} if stage is not None else {}),
            }
            return request_id
        return await self._change(run_id, change)

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
            self._cooldown(tx, run_id, policy)
        except RequestAdmissionError as exc:
            return exc.reason_code
        return None

    async def dispatch(self, run_id: str, request_id: str) -> None:
        async def change(tx: RequestSession) -> None:
            self._admit(tx, run_id)
            row = self._get(tx, run_id, request_id)
            if row["state"] != "reserved":
                raise RequestAdmissionError("llm_request_not_reserved")
            policy = LLMRequestPolicy.model_validate(tx.records[("llm_request_policy", run_id)])
            if self._cooldown(tx, run_id, policy) > 0:
                raise RequestAdmissionError("llm_provider_cooldown_active")
            row["state"] = "dispatched"
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
        total_tokens: int | None = None,
    ) -> None:
        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            state = "unknown" if unknown else "settled"
            tokens = total_tokens if type(total_tokens) is int and total_tokens >= 0 else None
            if row["state"] == state and row["total_tokens"] == tokens:
                return
            if row["state"] != "dispatched":
                raise RequestAdmissionError("llm_request_settlement_conflict")
            row.update(state=state, total_tokens=tokens)
        await self._change(run_id, change)

    async def release_undispatched(self, run_id: str, request_id: str) -> None:
        async def change(tx: RequestSession) -> None:
            row = self._get(tx, run_id, request_id)
            if row["state"] not in {"reserved", "released"}:
                raise RequestAdmissionError("dispatched_request_cannot_be_refunded")
            row["state"] = "released"
        await self._change(run_id, change)

    async def snapshot(self, run_id: str) -> dict[str, Any]:
        async def read(tx: RequestSession) -> dict[str, Any]:
            rows = self._rows(tx, run_id)
            spent = [r for r in rows if r["state"] in {"settled", "unknown"}]
            return {
                "policy": tx.records.get(("llm_request_policy", run_id)),
                "spent": len(spent),
                "reserved": sum(r["state"] in {"reserved", "dispatched"} for r in rows),
                "known_tokens": sum(r["total_tokens"] or 0 for r in spent),
                "unknown_usage": sum(r["total_tokens"] is None for r in spent),
                "unknown_dispatch": sum(r["state"] == "unknown" for r in spent),
                "provider_cooldown": tx.records.get(("llm_provider_cooldown", run_id)),
                "by_stage": request_stage_usage(rows),
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
                elif current["state"] == "dispatched":
                    current.update(state="unknown", total_tokens=None)
        await self._change(run_id, change)

    async def recover_run(self, run_id: str) -> None:
        """Exclusive cold Run recovery, including planner and verification scopes."""
        await self.recover_owner(run_id, owner=None)
