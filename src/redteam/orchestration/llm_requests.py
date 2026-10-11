"""Run-wide request accounting in the existing atomic coordinator records.

No prompts, responses, secrets or private reasoning are retained. Reservations
are not time-expired: recovering an execution slot must never refund a possibly
dispatched request. A recovered unknown remains charged.
"""

from __future__ import annotations

import time
from typing import Any

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ids import new_id
from redteam.core.ports.llm import LLMProviderFailure
from redteam.orchestration.cognition import ReasoningHalted
from redteam.orchestration.coordination import ChangeSet, CoordinationError, Coordinator

_REQUEST_STATES = ("reserved", "dispatched", "settled", "unknown", "released")


class RequestAdmissionError(ReasoningHalted):
    """A fixed safe reason; callers must not repair/retry an admission denial."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(
            reason_code, termination_reason=(
                "budget_exhausted" if reason_code == "run_llm_request_limit_exhausted" else None
            ),
        )


class RunRequestLedger:
    def __init__(self, coordinator: Coordinator, *, clock=time.time) -> None:
        self.coordinator = coordinator
        self.clock = clock

    def _cooldown(self, tx: ChangeSet, run_id: str, policy: LLMRequestPolicy) -> float:
        saved = tx.records.get(("llm_provider_cooldown", run_id), {})
        if saved.get("held"):
            raise RequestAdmissionError("llm_rate_limit_requires_operator_review")
        if saved.get("consecutive", 0) >= policy.max_consecutive_rate_limits:
            raise RequestAdmissionError("llm_consecutive_rate_limit_exhausted")
        return max(0.0, saved.get("until", 0.0) - self.clock())

    async def wait_seconds(self, run_id: str) -> float:
        async def read(tx: ChangeSet) -> float:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            return self._cooldown(tx, run_id, LLMRequestPolicy.model_validate(saved))
        return await self.coordinator.change(read)

    async def record_provider_outcome(
        self, run_id: str, failure: LLMProviderFailure | None = None,
    ) -> None:
        async def change(tx: ChangeSet) -> None:
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
        await self.coordinator.change(change)

    async def bind(self, run_id: str, policy: LLMRequestPolicy) -> None:
        async def change(tx: ChangeSet) -> None:
            key = ("llm_request_policy", run_id)
            payload = policy.model_dump(mode="json")
            saved = tx.records.get(key)
            if saved is not None and saved != payload:
                raise RequestAdmissionError("llm_request_policy_changed_new_run_required")
            tx.records[key] = payload
        await self.coordinator.change(change)

    @staticmethod
    def _checked_row(request_id: str, row: Any) -> dict[str, Any]:
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
        if (tokens is not None and (type(tokens) is not int or tokens < 0
                                    or row["state"] not in {"settled", "unknown"})):
            raise RequestAdmissionError("llm_request_record_invalid")
        return row

    @classmethod
    def _rows(cls, tx: ChangeSet, run_id: str) -> list[dict[str, Any]]:
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
                rows.append(cls._checked_row(request_id, row))
        return rows

    @classmethod
    def owner_has_activity(cls, tx: ChangeSet, run_id: str, owner: str) -> bool:
        """Even a released reservation contradicts a never-entered actor proof.

        Read-only: recovery never refunds requests or resets the shared policy.
        """
        return any(row["owner"] == owner for row in cls._rows(tx, run_id))

    @classmethod
    def request_states(
        cls, tx: ChangeSet, run_id: str, *, owner: str | None = None,
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
    def _admit(tx: ChangeSet, run_id: str) -> None:
        binding = tx.records.get(("binding", run_id))
        if binding is not None:
            try:
                tx.admit(run_id, binding["mission_id"])
            except CoordinationError as exc:
                raise RequestAdmissionError("llm_run_not_admitted") from exc

    async def reserve(self, run_id: str, *, owner: str, kind: str) -> str:
        if any(not isinstance(value, str) or not value.strip() for value in (run_id, owner, kind)):
            raise RequestAdmissionError("llm_request_scope_invalid")
        request_id = new_id("llm_request")

        async def change(tx: ChangeSet) -> str:
            self._admit(tx, run_id)
            saved = tx.records.get(("llm_request_policy", run_id))
            if saved is None:
                raise RequestAdmissionError("llm_request_policy_not_bound")
            policy = LLMRequestPolicy.model_validate(saved)
            if self._cooldown(tx, run_id, policy) > 0:
                raise RequestAdmissionError("llm_provider_cooldown_active")
            rows = self._rows(tx, run_id)
            charged = sum(row["state"] != "released" for row in rows)
            if policy.max_requests is not None and charged >= policy.max_requests:
                raise RequestAdmissionError("run_llm_request_limit_exhausted")
            active = sum(row["state"] in {"reserved", "dispatched"} for row in rows)
            if active >= policy.max_concurrent_requests:
                raise RequestAdmissionError("run_llm_concurrency_unavailable")
            tx.records[("llm_request", request_id)] = {
                "request_id": request_id, "run_id": run_id, "owner": owner,
                "kind": kind, "state": "reserved", "total_tokens": None,
            }
            return request_id
        return await self.coordinator.change(change)

    def launch_blocked(self, tx: ChangeSet, run_id: str) -> str | None:
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
        async def change(tx: ChangeSet) -> None:
            self._admit(tx, run_id)
            row = self._get(tx, run_id, request_id)
            if row["state"] != "reserved":
                raise RequestAdmissionError("llm_request_not_reserved")
            policy = LLMRequestPolicy.model_validate(tx.records[("llm_request_policy", run_id)])
            if self._cooldown(tx, run_id, policy) > 0:
                raise RequestAdmissionError("llm_provider_cooldown_active")
            row["state"] = "dispatched"
        await self.coordinator.change(change)

    @classmethod
    def _get(cls, tx: ChangeSet, run_id: str, request_id: str) -> dict[str, Any]:
        row = tx.records.get(("llm_request", request_id))
        if row is None:
            raise RequestAdmissionError("llm_request_identity_mismatch")
        row = cls._checked_row(request_id, row)
        if row["run_id"] != run_id:
            raise RequestAdmissionError("llm_request_identity_mismatch")
        return row

    async def settle(
        self, run_id: str, request_id: str, *, unknown: bool = False,
        total_tokens: int | None = None,
    ) -> None:
        async def change(tx: ChangeSet) -> None:
            row = self._get(tx, run_id, request_id)
            state = "unknown" if unknown else "settled"
            tokens = total_tokens if type(total_tokens) is int and total_tokens >= 0 else None
            if row["state"] == state and row["total_tokens"] == tokens:
                return
            if row["state"] != "dispatched":
                raise RequestAdmissionError("llm_request_settlement_conflict")
            row.update(state=state, total_tokens=tokens)
        await self.coordinator.change(change)

    async def release_undispatched(self, run_id: str, request_id: str) -> None:
        async def change(tx: ChangeSet) -> None:
            row = self._get(tx, run_id, request_id)
            if row["state"] not in {"reserved", "released"}:
                raise RequestAdmissionError("dispatched_request_cannot_be_refunded")
            row["state"] = "released"
        await self.coordinator.change(change)

    async def snapshot(self, run_id: str) -> dict[str, Any]:
        async def read(tx: ChangeSet) -> dict[str, Any]:
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
            }
        return await self.coordinator.change(read)
