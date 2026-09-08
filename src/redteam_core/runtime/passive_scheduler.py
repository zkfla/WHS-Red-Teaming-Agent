"""Bounded single-writer passive batches. Shared waiters reference one durable response."""

import asyncio
from collections.abc import Callable
from datetime import timedelta

from redteam_core.investigation.resolution import EvidenceResolutionError
from redteam_core.knowledge.recompute import SemanticRecomputer
from redteam_core.models.base import canonical_json, sha256_text, stable_id
from redteam_core.models.operational_budget import ReservationStatus
from redteam_core.models.requirements import EvidenceRequirement, ResolutionFailureKind
from redteam_core.models.resolution_batch import ResolutionBatch, ResolutionBatchItem
from redteam_core.models.runtime import RuntimeState
from redteam_core.runtime.operational_budget import OperationalBudgetExceeded
from redteam_core.runtime.resolution_commit import commit_resolution_receipts
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher


class PassiveResolutionScheduler:
    def __init__(self, dispatcher: DurableResolverDispatcher, *, auth_context: Callable[[], str]):
        self.dispatcher = dispatcher
        self.auth_context = auth_context
        self._running: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    async def close(self, *, grace_seconds: float = 5) -> None:
        """Stop owned shared tasks before runtime finalization; never claim stuck tasks stopped."""
        if not 0 < grace_seconds <= 60:
            raise ValueError("shutdown grace must be bounded")
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            if pending:
                self.dispatcher._broken = True
                raise RuntimeError("passive tasks still running; finalization is not safe")

    def _require_open(self):
        if self._closed:
            raise RuntimeError("passive scheduler is closed")

    async def wait(self, batch_id: str, waiter_id: str) -> ResolutionBatchItem:
        """Cancellation of one waiter does not cancel the shared batch's owned task."""
        self._require_open()
        batch = self._batch(batch_id)
        if not any(waiter_id in item.waiter_ids for item in batch.items):
            raise ValueError("unknown batch waiter")
        task = self._tasks.get(batch_id)
        if task is None:
            task = asyncio.create_task(self.run(batch_id))
            self._tasks[batch_id] = task

            def finished(completed):
                self._tasks.pop(batch_id, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
        try:
            completed = await asyncio.shield(task)
        except asyncio.CancelledError:
            current = self._batch(batch_id)
            self._store(
                ResolutionBatch.model_validate(
                    {
                        **current.model_dump(mode="python"),
                        "cancelled_waiter_ids": tuple(
                            sorted(set(current.cancelled_waiter_ids) | {waiter_id})
                        ),
                    }
                )
            )
            raise
        return next(item for item in completed.items if waiter_id in item.waiter_ids)

    def _batch(self, batch_id: str) -> ResolutionBatch:
        return next(b for b in self.dispatcher.runtime.resolution_batches if b.batch_id == batch_id)

    def _store(self, batch: ResolutionBatch):
        d = self.dispatcher
        d._check_binding()
        candidate = d.runtime.model_dump(mode="python")
        rows = {b.batch_id: b for b in d.runtime.resolution_batches}
        rows[batch.batch_id] = batch
        candidate["resolution_batches"] = tuple(rows[key] for key in sorted(rows))
        candidate["revision"] += 1
        runtime = RuntimeState.model_validate(candidate)
        try:
            d.orchestrator.checkpoint(
                d.session, runtime, reason="passive-batch-state", domain_states=d.domain_states
            )
        except BaseException:
            d._broken = True
            raise
        d.runtime.resolution_batches = runtime.resolution_batches
        d.runtime.revision = runtime.revision

    def admit(
        self,
        *,
        batch_id: str,
        requests: tuple[tuple[str, str], ...],
        concurrency: int = 1,
        per_provider_concurrency: int = 1,
        timeout_seconds: float = 60,
        allow_fallback: bool = False,
        max_batch_bytes: int = 1_048_576,
    ) -> ResolutionBatch:
        self._require_open()
        d = self.dispatcher
        d._check_binding()
        if not 0 < timeout_seconds <= 3600 or not 1 <= len(requests) <= 256:
            raise ValueError("batch requires bounded requests and deadline")
        if not 4096 <= max_batch_bytes <= 1_048_576:
            raise ValueError("batch byte budget out of range")
        if any(b.batch_id == batch_id for b in d.runtime.resolution_batches):
            raise ValueError("batch ID must not be reused")
        if len({w for w, _ in requests}) != len(requests) or any(
            not 1 <= len(w) <= 200 for w, _ in requests
        ):
            raise ValueError("waiter IDs must be unique and nonempty")
        permissions = frozenset(d.permissions())
        auth = self.auth_context()
        if not 1 <= len(auth) <= 200:
            raise ValueError("authorization context exceeds limit")
        input_bytes = len(canonical_json(sorted(permissions)).encode("utf-8"))
        grouped = {}
        for waiter, requirement_id in requests:
            raw = d.session.investigation.requirements.get(requirement_id)
            if raw is None:
                raise ValueError("unknown canonical batch requirement")
            input_bytes += len(raw.model_dump_json().encode("utf-8")) + len(waiter.encode("utf-8"))
            if input_bytes > max_batch_bytes:
                raise ValueError("batch input exceeds byte budget")
            requirement = EvidenceRequirement.model_validate(raw.model_dump(mode="python"))
            failure = None
            try:
                d.registry.select(requirement, available_permissions=permissions)
                descriptors = d.registry.eligible(requirement, available_permissions=permissions)
            except EvidenceResolutionError as exc:
                failure, descriptors = exc.kind, ()
            hashes = tuple(
                "sha256:" + sha256_text(canonical_json(r.descriptor.model_dump(mode="json")))
                for r in descriptors
            )
            key = canonical_json(
                {
                    "requirement": requirement.model_dump(mode="json"),
                    "descriptors": hashes,
                    "auth": auth,
                    "permissions": sorted(permissions),
                }
            )
            if key not in grouped:
                grouped[key] = (requirement, hashes, failure, [])
            grouped[key][3].append(waiter)
        items = []
        for key, (requirement, hashes, failure, waiters) in sorted(grouped.items()):
            identity = stable_id("batchitem", batch_id, key)
            items.append(
                ResolutionBatchItem(
                    item_id=identity,
                    attempt_id=stable_id("passive", d.runtime.run_id, identity),
                    fallback_attempt_ids=tuple(
                        stable_id("passive", d.runtime.run_id, identity, index)
                        for index in range(1, len(hashes))
                    )
                    if allow_fallback
                    else (),
                    requirement=requirement,
                    waiter_ids=tuple(sorted(waiters)),
                    descriptor_hashes=hashes,
                    failure_kind=failure,
                    settled=failure is not None,
                )
            )
        batch = ResolutionBatch(
            batch_id=batch_id,
            run_id=d.runtime.run_id,
            composition_hash=d._ledger().composition_hash,
            auth_context=auth,
            permissions=tuple(sorted(permissions)),
            items=tuple(items),
            concurrency=concurrency,
            per_provider_concurrency=per_provider_concurrency,
            allow_fallback=allow_fallback,
            max_serialized_bytes=max_batch_bytes,
            deadline_at=d.orchestrator.clock.now() + timedelta(seconds=timeout_seconds),
        )
        # Reserve growth for every result hash, final outcome and cancelled waiter before calls.
        growth = sum(96 * len(i.attempt_ids) + 256 for i in batch.items)
        growth += len(
            canonical_json([w for i in batch.items for w in i.waiter_ids]).encode("utf-8")
        )
        if len(batch.model_dump_json().encode("utf-8")) + growth > max_batch_bytes:
            raise ValueError("batch lacks reserved outcome byte capacity")
        self._store(batch)
        return batch

    def _settle(self, batch_id, item, *, failure=None):
        batch = self._batch(batch_id)
        by_attempt = {r.attempt_id: r for r in self.dispatcher.runtime.resolution_receipts}
        history = tuple(by_attempt[a] for a in item.attempt_ids if a in by_attempt)
        receipt = history[-1] if history else None
        update = item.model_dump(mode="python")
        update.update(
            settled=True,
            receipt_hash=receipt.receipt_hash if receipt else None,
            receipt_hashes=tuple(r.receipt_hash for r in history),
            failure_kind=failure or (receipt.failure.kind if receipt and receipt.failure else None),
        )
        settled = ResolutionBatchItem.model_validate(update)
        self._store(
            ResolutionBatch.model_validate(
                {
                    **batch.model_dump(mode="python"),
                    "items": tuple(
                        settled if i.item_id == item.item_id else i for i in batch.items
                    ),
                }
            )
        )

    async def run(self, batch_id: str) -> ResolutionBatch:
        self._require_open()
        d = self.dispatcher
        d._check_binding()
        if batch_id in self._running:
            raise RuntimeError("batch already has a writer")
        batch = self._batch(batch_id)
        stored = d.orchestrator.checkpoint_store.load(
            catalog=d.orchestrator.catalog,
            composition_fingerprint=d.orchestrator.composition_fingerprint,
            journal=d.orchestrator.journal,
            expected_run_id=d.runtime.run_id,
        )
        if stored.runtime_state.resolution_batches != d.runtime.resolution_batches:
            raise ValueError("batch admission differs from durable state")
        if batch.complete:
            return batch
        self._running.add(batch_id)
        remaining = max(0, (batch.deadline_at - d.orchestrator.clock.now()).total_seconds())
        end = asyncio.get_running_loop().time() + remaining
        providers = {}
        pending = iter(batch.items)

        def check_admission(item):
            if (
                self.auth_context() != batch.auth_context
                or tuple(sorted(d.permissions())) != batch.permissions
            ):
                raise PermissionError("batch authorization context changed")
            if (
                d.session.investigation.requirements.get(item.requirement.requirement_id)
                != item.requirement
            ):
                raise ValueError("batch requirement changed")
            if (
                asyncio.get_running_loop().time() >= end
                or d.orchestrator.clock.now() >= batch.deadline_at
            ):
                raise TimeoutError("batch deadline exceeded")

        async def worker():
            for item in pending:
                if item.settled:
                    continue
                semaphore = providers.setdefault(
                    item.requirement.provider_id, asyncio.Semaphore(batch.per_provider_concurrency)
                )
                failure = None
                for index, attempt_id in enumerate(item.attempt_ids):
                    receipt = next(
                        (r for r in d.runtime.resolution_receipts if r.attempt_id == attempt_id),
                        None,
                    )
                    failure = receipt.failure.kind if receipt and receipt.failure else None
                    if receipt is None:
                        if any(r.attempt_id == attempt_id for r in d._ledger().reservations):
                            # An intent without response does not authorize fallback.
                            failure = ResolutionFailureKind.TRANSIENT
                            break
                        try:
                            async with asyncio.timeout(
                                max(0, end - asyncio.get_running_loop().time())
                            ):
                                async with semaphore:
                                    check_admission(item)
                                    await d.resolve(
                                        item.requirement.requirement_id,
                                        attempt_id=attempt_id,
                                        allowed_descriptor_hashes=(item.descriptor_hashes[index],)
                                        if batch.allow_fallback
                                        else item.descriptor_hashes,
                                        admission_check=lambda item=item: check_admission(item),
                                    )
                            if asyncio.get_running_loop().time() >= end:
                                failure = ResolutionFailureKind.TIMEOUT
                        except TimeoutError:
                            failure = ResolutionFailureKind.TIMEOUT
                        except PermissionError:
                            failure = ResolutionFailureKind.AUTH_REQUIRED
                        except OperationalBudgetExceeded:
                            failure = ResolutionFailureKind.BUDGET_EXHAUSTED
                        except EvidenceResolutionError as exc:
                            failure = exc.kind
                        except ValueError:
                            if d._broken:
                                raise
                            failure = ResolutionFailureKind.INVALID_PAYLOAD
                        receipt = next(
                            (
                                r
                                for r in d.runtime.resolution_receipts
                                if r.attempt_id == attempt_id
                            ),
                            None,
                        )
                    reservation = next(
                        (r for r in d._ledger().reservations if r.attempt_id == attempt_id), None
                    )
                    can_fallback = (
                        batch.allow_fallback
                        and receipt is not None
                        and receipt.failure is not None
                        and reservation is not None
                        and reservation.status is ReservationStatus.FAILED
                        and failure is ResolutionFailureKind.INVALID_PAYLOAD
                        and index + 1 < len(item.attempt_ids)
                    )
                    if not can_fallback:
                        break
                self._settle(batch_id, item, failure=failure)

        tasks = [
            asyncio.create_task(worker()) for _ in range(min(batch.concurrency, len(batch.items)))
        ]
        try:
            await asyncio.gather(*tasks)
            current = self._batch(batch_id)
            self._store(
                ResolutionBatch.model_validate(
                    {**current.model_dump(mode="python"), "complete": True}
                )
            )
            return self._batch(batch_id)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._running.discard(batch_id)

    def commit(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        recomputers: tuple[SemanticRecomputer, ...] = (),
    ):
        batch = self._batch(batch_id)
        if not batch.complete:
            raise ValueError("batch is not complete")
        failures = {
            r.receipt_hash
            for r in self.dispatcher.runtime.resolution_receipts
            if r.failure is not None
        }

        def check_authorization(stored_batch):
            if stored_batch != batch:
                raise ValueError("batch changed from durable admission")
            if (
                self.auth_context() != batch.auth_context
                or tuple(sorted(self.dispatcher.permissions())) != batch.permissions
            ):
                raise PermissionError("batch authorization context changed before commit")

        return commit_resolution_receipts(
            self.dispatcher,
            attempt_ids=tuple(
                r.attempt_id
                for i in batch.items
                for r in self.dispatcher.runtime.resolution_receipts
                if r.receipt_hash in i.receipt_hashes
                and (i.failure_kind is None or r.receipt_hash in failures)
            ),
            expected_revision=expected_revision,
            recomputers=recomputers,
            batch_authorization_check=check_authorization,
        )
