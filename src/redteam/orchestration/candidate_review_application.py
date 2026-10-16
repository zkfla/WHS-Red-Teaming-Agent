"""Apply durable final review receipts to Central State exactly once.

Evidence inspection and validity capture happen before the SQLite transaction.
The transaction remains the sole owner of candidate/Finding state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.ids import EvidenceId
from redteam.orchestration.central_state import CheckedEvidence, check_evidence
from redteam.orchestration.coordination import CoordinationError, StateConflict


class CandidateReviewApplicationResult(BaseModel):
    """Structured result returned to tools and settlement consumers."""

    model_config = ConfigDict(extra="allow")

    contract_version: int = 1
    review_receipt_id: str
    candidate_id: str | None = None
    candidate_pre_verdict_revision: int | None = None
    candidate_revision: int | None = None
    finding_id: str | None = None
    task_id: str | None = None
    status: Literal[
        "promoted",
        "candidate_rejected",
        "inconclusive",
        "pending_task_result",
        "already_applied",
        "refused",
        "retryable_error",
    ]
    reason: str = ""


@dataclass(frozen=True)
class PreparedCandidateReview:
    """Immutable external reads ready for a synchronous Central transaction."""

    receipt: EvidenceRecord
    checked: tuple[CheckedEvidence, ...]
    validity_read: Any
    task_id: str | None = None


class CandidateReviewPreparationError(RuntimeError):
    """Bounded preparation failure without evidence bodies in its message."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class CandidateReviewApplicator(Protocol):
    """Run-bound port consumed by graph recording, settlement and recovery."""

    async def apply(
        self, receipt_id: str, *, task_id: str | None = None,
    ) -> CandidateReviewApplicationResult: ...

    async def prepare_pending(
        self, task_id: str,
    ) -> tuple[PreparedCandidateReview, ...]: ...

    def apply_in_transaction(
        self, tx: Any, prepared: PreparedCandidateReview, *, task_id: str,
    ) -> dict[str, Any]: ...

    async def retry_pending(
        self, *, task_id: str | None = None,
    ) -> tuple[CandidateReviewApplicationResult, ...]: ...


class CandidateReviewApplicationService:
    """One Run's public application boundary over existing stores."""

    def __init__(
        self, *, evidence_store: Any, graph_store: Any, central_state: Any,
        event_bus: Any = None,
    ) -> None:
        self._deps = SimpleNamespace(
            evidence_store=evidence_store,
            graph_store=graph_store,
            central_state=central_state,
            event_bus=event_bus,
        )

    def bind_event_bus(self, event_bus: Any) -> None:
        """Finish composition after the root wraps the bus with Central outbox delivery."""
        self._deps.event_bus = event_bus

    async def apply(
        self, receipt_id: str, *, task_id: str | None = None,
    ) -> CandidateReviewApplicationResult:
        result = await apply_candidate_review_receipt(
            self._deps,
            receipt_id,
            task_id=task_id,
        )
        flush = getattr(self._deps.event_bus, "flush", None)
        if result.status in {"promoted", "candidate_rejected"} and flush is not None:
            await flush()
        return result

    async def prepare_pending(
        self, task_id: str,
    ) -> tuple[PreparedCandidateReview, ...]:
        return await prepare_pending_candidate_reviews(self._deps, task_id)

    def apply_in_transaction(
        self, tx: Any, prepared: PreparedCandidateReview, *, task_id: str,
    ) -> dict[str, Any]:
        return tx.apply_candidate_review(
            receipt=prepared.receipt,
            checked=prepared.checked,
            validity_read=prepared.validity_read,
            task_id=task_id,
        )

    async def retry_pending(
        self, *, task_id: str | None = None,
    ) -> tuple[CandidateReviewApplicationResult, ...]:
        return await retry_pending_candidate_reviews(self._deps, task_id=task_id)


async def prepare_candidate_review(
    deps: Any, receipt_id: str, *, task_id: str | None = None,
) -> PreparedCandidateReview:
    """Read and integrity-check a receipt and its originals outside SQLite."""
    try:
        receipt = await deps.evidence_store.get(EvidenceId(receipt_id))
    except Exception as exc:
        raise CandidateReviewPreparationError(
            "candidate_review_receipt_unreadable", retryable=True,
        ) from exc
    if receipt is None:
        raise CandidateReviewPreparationError(
            "candidate_review_receipt_missing", retryable=False,
        )
    meta = receipt.metadata if isinstance(receipt.metadata, dict) else {}
    if meta.get("record_kind") != "review_receipt":
        raise CandidateReviewPreparationError(
            "candidate_review_receipt_kind_invalid", retryable=False,
        )
    raw_sources = meta.get("source_evidence_ids")
    if not isinstance(raw_sources, (list, tuple)):
        raise CandidateReviewPreparationError(
            "candidate_review_source_identity_invalid", retryable=False,
        )
    source_ids = [
        str(item).strip() for item in raw_sources
        if isinstance(item, str) and item.strip()
    ]
    if not source_ids or len(source_ids) != len(set(source_ids)):
        raise CandidateReviewPreparationError(
            "candidate_review_source_identity_invalid", retryable=False,
        )
    try:
        checked = await check_evidence(
            deps.evidence_store,
            run_id=deps.central_state.run_id,
            engagement_id=deps.central_state.engagement_id,
            evidence_ids=[receipt_id, *source_ids],
        )
        from redteam.attack_graph.service import AttackGraphService
        from redteam.orchestration.evidence_validity import capture_validity

        validity_read, errors = await capture_validity(
            deps,
            source_ids,
            graph_service=AttackGraphService(deps.graph_store),
        )
    except CandidateReviewPreparationError:
        raise
    except Exception as exc:
        raise CandidateReviewPreparationError(
            "candidate_review_original_inspection_failed", retryable=True,
        ) from exc
    if errors or validity_read is None:
        raise CandidateReviewPreparationError(
            "candidate_review_evidence_not_current", retryable=False,
        )
    return PreparedCandidateReview(
        receipt=receipt,
        checked=checked,
        validity_read=validity_read,
        task_id=task_id,
    )


async def apply_prepared_candidate_review(
    deps: Any, prepared: PreparedCandidateReview,
) -> CandidateReviewApplicationResult:
    payload = await deps.central_state.transaction(lambda tx: tx.apply_candidate_review(
        receipt=prepared.receipt,
        checked=prepared.checked,
        validity_read=prepared.validity_read,
        task_id=prepared.task_id,
    ))
    return CandidateReviewApplicationResult.model_validate(payload)


async def apply_candidate_review_receipt(
    deps: Any, receipt_id: str, *, task_id: str | None = None,
) -> CandidateReviewApplicationResult:
    """Application boundary used by graph.record and explicit recovery."""
    try:
        prepared = await prepare_candidate_review(deps, receipt_id, task_id=task_id)
        return await apply_prepared_candidate_review(deps, prepared)
    except CandidateReviewPreparationError as exc:
        return CandidateReviewApplicationResult(
            review_receipt_id=receipt_id,
            task_id=task_id,
            status="retryable_error" if exc.retryable else "refused",
            reason=exc.code,
        )
    except Exception as exc:
        # Contract/state rejections use bounded owner-produced messages.
        # Unexpected storage failures remain retryable and expose no body,
        # because it may contain backend or secret material.
        if isinstance(exc, ValidationError):
            first = exc.errors(include_url=False, include_context=False)[0]
            location = ".".join(map(str, first.get("loc", ()))) or "root"
            reason = "validation:" + location + ":" + str(first.get("type") or "invalid")
            status = "refused"
        elif isinstance(exc, (CoordinationError, StateConflict)):
            reason = str(exc)
            status = "refused"
        else:
            reason = type(exc).__name__
            status = "retryable_error"
        if not reason or any(char.isspace() for char in reason):
            reason = type(exc).__name__
        return CandidateReviewApplicationResult(
            review_receipt_id=receipt_id,
            task_id=task_id,
            status=status,
            reason=reason[:160],
        )


async def prepare_pending_candidate_reviews(
    deps: Any, task_id: str,
) -> tuple[PreparedCandidateReview, ...]:
    """Capture only explicit current-Run pending applications for one Task."""
    pending = await deps.central_state.candidate_review_applications(
        task_id=task_id,
        status="pending_task_result",
    )
    prepared: list[PreparedCandidateReview] = []
    for item in pending:
        receipt_id = str(item.get("review_receipt_id") or "")
        if not receipt_id:
            continue
        try:
            prepared.append(await prepare_candidate_review(
                deps, receipt_id, task_id=task_id,
            ))
        except CandidateReviewPreparationError:
            # Task settlement remains valid. The durable pending entry makes
            # this apply retryable without replaying target execution.
            continue
    return tuple(prepared)


async def retry_pending_candidate_reviews(
    deps: Any, *, task_id: str | None = None,
) -> tuple[CandidateReviewApplicationResult, ...]:
    """Retry explicit pending receipts whose canonical Task result now exists.

    This is a bounded recovery pass over the application ledger, not a scan of
    historical review evidence. External reads remain outside Central State
    transactions and each receipt retains its exact idempotency identity.
    """
    pending = await deps.central_state.candidate_review_applications(
        task_id=task_id,
        status="pending_task_result",
    )
    results: list[CandidateReviewApplicationResult] = []
    for item in pending:
        pending_task_id = str(item.get("task_id") or "").strip()
        receipt_id = str(item.get("review_receipt_id") or "").strip()
        if not pending_task_id or not receipt_id:
            continue
        task = await deps.central_state.get_task(pending_task_id)
        if task is None or task.result is None:
            continue
        results.append(await apply_candidate_review_receipt(
            deps,
            receipt_id,
            task_id=pending_task_id,
        ))
    flush = getattr(deps.event_bus, "flush", None)
    if results and flush is not None:
        await flush()
    return tuple(results)
