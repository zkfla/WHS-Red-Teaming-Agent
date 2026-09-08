"""Single-writer durable passive dispatch. Result receipts/batching belong to the scheduler."""

from collections.abc import Callable

from redteam_core.investigation.requirements import (
    EvidenceRequirement,
    Observation,
    ResolutionFailureKind,
    ResolutionFailureRecord,
)
from redteam_core.investigation.resolution import (
    EvidenceResolutionError,
    ResolverRuntimeRegistry,
    _resolve_selected_observation,
)
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.operational_budget import (
    OperationalBudgetLimits,
    OperationalBudgetState,
    RequestReservation,
    ReservationStatus,
)
from redteam_core.models.resolution_receipt import (
    MAX_RESOLUTION_RECEIPT_LEDGER_BYTES,
    MAX_RESOLUTION_RECEIPTS,
    ResolutionReceipt,
)
from redteam_core.models.runtime import RuntimeLifecycle, RuntimeState
from redteam_core.runtime.checkpoint import build_runtime_composition_fingerprint
from redteam_core.runtime.journal import RuntimeEventType
from redteam_core.runtime.operational_budget import (
    OperationalBudgetExceeded,
    reserve_request,
    synchronize_operational_usage,
    transition_request,
)
from redteam_core.runtime.orchestrator import RuntimeOrchestrator


class DurableResolverDispatcher:
    def __init__(
        self,
        *,
        session: CoreSession,
        runtime: RuntimeState,
        orchestrator: RuntimeOrchestrator,
        registry: ResolverRuntimeRegistry,
        permissions: Callable[[], frozenset[str]],
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ):
        self.session, self.runtime, self.orchestrator = session, runtime, orchestrator
        self.registry, self.permissions, self.domain_states = registry, permissions, domain_states
        self._broken = False
        self._usage_floor = runtime.usage.model_copy(deep=True)
        self._limits = self._ledger().limits
        self._check_binding()
        ledger = self._ledger()
        if any(row.status is ReservationStatus.DISPATCHED for row in ledger.reservations):
            raise RuntimeError("unrecovered dispatch intent; resume before attaching dispatcher")
        stored = orchestrator.checkpoint_store.load(
            catalog=orchestrator.catalog,
            composition_fingerprint=orchestrator.composition_fingerprint,
            journal=orchestrator.journal,
            expected_run_id=runtime.run_id,
        )
        if stored.runtime_state.operational_budget != ledger:
            raise RuntimeError(
                "budget differs from checkpoint; persist recovered state before dispatch"
            )
        if not runtime.usage.dominates(stored.runtime_state.usage):
            raise RuntimeError("runtime usage is older than its checkpoint")
        registry.bind_operational_budget(self._ledger)

    @staticmethod
    def initialize(
        *,
        session: CoreSession,
        runtime: RuntimeState,
        orchestrator: RuntimeOrchestrator,
        limits: OperationalBudgetLimits,
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ) -> None:
        if (
            runtime.operational_budget is not None
            or runtime.resume_count
            or runtime.usage.requests
            or runtime.lifecycle is not RuntimeLifecycle.RUNNING
            or orchestrator.checkpoint_store.path.exists()
        ):
            raise RuntimeError("budget initialization requires a new uncheckpointed run")
        ledger = OperationalBudgetState(
            runtime_limits_hash="sha256:"
            + sha256_text(canonical_json(orchestrator.budget_limits.model_dump(mode="json"))),
            run_id=runtime.run_id,
            composition_hash=orchestrator.composition_fingerprint.composition_hash,
            limits=limits,
        )
        candidate = runtime.model_copy(deep=True)
        candidate.operational_budget = ledger
        candidate.revision += 1
        orchestrator.checkpoint(
            session, candidate, reason="operational-budget-initialized", domain_states=domain_states
        )
        runtime.operational_budget, runtime.revision = ledger, candidate.revision

    def _ledger(self) -> OperationalBudgetState:
        ledger = self.runtime.operational_budget
        if ledger is None:
            raise RuntimeError("durable operational budget is not configured")
        return OperationalBudgetState.model_validate(ledger.model_dump(mode="python"))

    def _check_binding(self):
        if self._broken:
            raise RuntimeError("dispatcher storage failure; reload and recover before continuing")
        RuntimeState.model_validate(self.runtime.model_dump(mode="python"))
        if not self.runtime.usage.dominates(self._usage_floor):
            raise RuntimeError("runtime usage moved backwards")
        if self.runtime.lifecycle is not RuntimeLifecycle.RUNNING:
            raise RuntimeError("runtime is not running")
        ledger = self._ledger()
        expected = self.orchestrator.composition_fingerprint
        actual = build_runtime_composition_fingerprint(
            catalog=self.orchestrator.catalog,
            resolver_descriptors=self.registry.descriptors(),
            priority_provider_ids=expected.priority_provider_ids,
            decision_driver_hash=expected.decision_driver_hash,
            semantic_pipeline_hash=expected.semantic_pipeline_hash,
            context_policy_hash=expected.context_policy_hash,
            progress_policy_hash=expected.progress_policy_hash,
        )
        if (
            not self.registry.frozen
            or actual != expected
            or ledger.run_id != self.runtime.run_id
            or ledger.composition_hash != expected.composition_hash
            or ledger.limits != self._limits
            or ledger.runtime_limits_hash
            != "sha256:"
            + sha256_text(canonical_json(self.orchestrator.budget_limits.model_dump(mode="json")))
        ):
            raise RuntimeError("operational run/composition/resolver binding mismatch")

    def _check_global_projection(self, ledger: OperationalBudgetState):
        limits = self.orchestrator.budget_limits
        requests = (
            self.runtime.usage.requests
            + ledger.charged_requests
            - self.runtime.operational_accounted_requests
        )
        tokens = (
            self.runtime.usage.tokens
            + ledger.charged_tokens
            - self.runtime.operational_accounted_tokens
        )
        if limits.max_requests is not None and requests > limits.max_requests:
            raise OperationalBudgetExceeded("combined runtime request budget")
        if limits.max_tokens is not None:
            if any(
                not r.token_bound_guaranteed and r.status is not ReservationStatus.RELEASED
                for r in ledger.reservations
            ):
                raise OperationalBudgetExceeded("combined runtime hard token bound unsupported")
            if tokens > limits.max_tokens:
                raise OperationalBudgetExceeded("combined runtime token budget")

    def _persist(self, ledger: OperationalBudgetState, *, receipt: ResolutionReceipt | None = None):
        if self._broken:
            raise RuntimeError("dispatcher storage failure; reload before persistence")
        if (
            ledger.limits != self._limits
            or ledger.run_id != self.runtime.run_id
            or ledger.composition_hash != self.orchestrator.composition_fingerprint.composition_hash
        ):
            self._broken = True
            raise RuntimeError("operational budget binding changed before persistence")
        candidate = self.runtime.model_copy(deep=True)
        candidate.operational_budget = ledger
        if receipt is not None:
            candidate.resolution_receipts = (*candidate.resolution_receipts, receipt)
        candidate.revision += 1
        candidate = synchronize_operational_usage(candidate)
        try:
            self.orchestrator.checkpoint(
                self.session,
                candidate,
                reason="operational-budget-transition",
                domain_states=self.domain_states,
            )
        except BaseException:
            self._broken = True
            raise
        self.runtime.operational_budget, self.runtime.revision = ledger, candidate.revision
        self.runtime.usage = candidate.usage
        self.runtime.operational_accounted_requests = candidate.operational_accounted_requests
        self.runtime.operational_accounted_tokens = candidate.operational_accounted_tokens
        self.runtime.resolution_receipts = candidate.resolution_receipts
        self._usage_floor = candidate.usage.model_copy(deep=True)

    def _event(self, event_type: RuntimeEventType, reservation: RequestReservation):
        try:
            self.orchestrator.journal.append(
                event_type,
                attributes={
                    "attempt_id": reservation.attempt_id,
                    "provider_id": reservation.provider_id,
                    "resolver_id": reservation.resolver_id,
                    "composition_hash": self._ledger().composition_hash,
                    "limits_hash": self._ledger().limits_hash,
                },
            )
        except BaseException:
            self._broken = True
            raise

    async def resolve(
        self,
        requirement_id: str,
        *,
        attempt_id: str,
        allowed_descriptor_hashes: tuple[str, ...] | None = None,
        admission_check: Callable[[], None] | None = None,
    ) -> Observation:
        self._check_binding()
        if admission_check is not None:
            admission_check()
        canonical = self.session.investigation.requirements.get(requirement_id)
        if canonical is None:
            raise ValueError("unknown canonical requirement")
        requirement = EvidenceRequirement.model_validate(canonical.model_dump(mode="python"))
        resolver = self.registry.select(requirement, available_permissions=self.permissions())
        if allowed_descriptor_hashes is not None:
            resolver = next(
                (
                    r
                    for r in self.registry.eligible(
                        requirement, available_permissions=self.permissions()
                    )
                    if "sha256:" + sha256_text(canonical_json(r.descriptor.model_dump(mode="json")))
                    in allowed_descriptor_hashes
                ),
                None,
            )
            if resolver is None:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.BUDGET_EXHAUSTED,
                    "admitted resolver is unavailable",
                )
        descriptor = resolver.descriptor.model_copy(deep=True)
        if (
            len(requirement.model_dump_json().encode("utf-8")) + 2048
            > descriptor.max_receipt_bytes_per_call
        ):
            raise ValueError("canonical requirement exceeds durable response reservation")
        descriptor_hash = "sha256:" + sha256_text(
            canonical_json(descriptor.model_dump(mode="json"))
        )
        if (
            allowed_descriptor_hashes is not None
            and descriptor_hash not in allowed_descriptor_hashes
        ):
            raise ValueError("resolver differs from batch admission")
        reservation = RequestReservation(
            attempt_id=attempt_id,
            provider_id=descriptor.provider_id,
            resolver_id=descriptor.resolver_id,
            reserved_tokens=descriptor.max_tokens_per_call or 0,
            token_bound_guaranteed=descriptor.max_tokens_per_call is not None,
            reserved_receipt_bytes=descriptor.max_receipt_bytes_per_call,
        )
        ledger = self._ledger()
        pending_receipts = sum(
            row.status in {ReservationStatus.RESERVED, ReservationStatus.DISPATCHED}
            for row in ledger.reservations
        )
        if len(self.runtime.resolution_receipts) + pending_receipts >= MAX_RESOLUTION_RECEIPTS:
            raise OperationalBudgetExceeded("durable receipt capacity exhausted")
        receipt_bytes = sum(r.serialized_bytes for r in self.runtime.resolution_receipts)
        pending_bytes = sum(
            r.reserved_receipt_bytes
            for r in ledger.reservations
            if r.status in {ReservationStatus.RESERVED, ReservationStatus.DISPATCHED}
        )
        if (
            receipt_bytes + pending_bytes + descriptor.max_receipt_bytes_per_call
            > MAX_RESOLUTION_RECEIPT_LEDGER_BYTES
        ):
            raise OperationalBudgetExceeded("durable receipt byte capacity exhausted")
        reserved = reserve_request(ledger, reservation, expected_revision=ledger.revision)
        self._check_global_projection(reserved)
        self._persist(reserved)
        ledger = self._ledger()
        self._persist(
            transition_request(
                ledger, attempt_id, ReservationStatus.DISPATCHED, expected_revision=ledger.revision
            )
        )
        self._event(RuntimeEventType.OPERATIONAL_DISPATCH_INTENT, reservation)

        def make_receipt(**result):
            receipt = ResolutionReceipt.create(
                run_id=self.runtime.run_id,
                attempt_id=attempt_id,
                composition_hash=self._ledger().composition_hash,
                limits_hash=self._ledger().limits_hash,
                descriptor_hash="sha256:"
                + sha256_text(canonical_json(descriptor.model_dump(mode="json"))),
                requirement=requirement.model_copy(deep=True),
                **result,
            )
            if receipt.serialized_bytes > descriptor.max_receipt_bytes_per_call:
                raise ValueError("response exceeds its durable receipt reservation")
            return receipt

        def before_call():
            if admission_check is not None:
                admission_check()
            self._check_binding()
            self._check_global_projection(self._ledger())
            if (
                resolver.descriptor != descriptor
                or self.session.investigation.requirements.get(requirement_id) != requirement
                or not set(descriptor.required_permissions) <= set(self.permissions())
            ):
                raise RuntimeError("passive dispatch binding or permission changed")

        response_seen = False
        actual_tokens = None

        def on_response(tokens: int | None):
            nonlocal response_seen, actual_tokens
            response_seen, actual_tokens = True, tokens

        try:
            observation = await _resolve_selected_observation(
                requirement=requirement.model_copy(deep=True),
                resolver=resolver,
                catalog=self.orchestrator.catalog,
                clock=self.orchestrator.clock,
                before_call=before_call,
                on_response=on_response,
            )
            # Persist rejection, not a reusable success, if authority/deadline changed in flight.
            if admission_check is not None:
                admission_check()
        except BaseException as exc:
            # A returned invalid payload is a known failure, not an ambiguous remote result.
            # Timeout/cancellation before response remain conservatively UNCERTAIN.
            ledger = self._ledger()
            self._persist(
                transition_request(
                    ledger,
                    attempt_id,
                    ReservationStatus.FAILED if response_seen else ReservationStatus.UNCERTAIN,
                    expected_revision=ledger.revision,
                    actual_tokens=actual_tokens,
                ),
                receipt=make_receipt(
                    failure=ResolutionFailureRecord.create(
                        requirement_id=requirement_id,
                        resolver_id=descriptor.resolver_id,
                        kind=exc.kind
                        if isinstance(exc, EvidenceResolutionError)
                        else ResolutionFailureKind.TIMEOUT
                        if isinstance(exc, TimeoutError)
                        else ResolutionFailureKind.AUTH_REQUIRED
                        if isinstance(exc, PermissionError)
                        else ResolutionFailureKind.TRANSIENT,
                        # Do not durably copy arbitrary adapter exception text or secrets.
                        detail="passive response rejected"
                        if response_seen
                        else "passive completion uncertain",
                        retryable=False,
                        occurred_at=self.orchestrator.clock.now(),
                    )
                ),
            )
            if response_seen:
                self._event(RuntimeEventType.OPERATIONAL_REQUEST_SETTLED, reservation)
            raise
        try:
            receipt = make_receipt(observation=observation)
        except ValueError as exc:
            ledger = self._ledger()
            failure = ResolutionFailureRecord.create(
                requirement_id=requirement_id,
                resolver_id=descriptor.resolver_id,
                kind=ResolutionFailureKind.INVALID_PAYLOAD,
                detail="passive response exceeds durable receipt limit",
                retryable=False,
                occurred_at=self.orchestrator.clock.now(),
            )
            self._persist(
                transition_request(
                    ledger,
                    attempt_id,
                    ReservationStatus.FAILED,
                    expected_revision=ledger.revision,
                    actual_tokens=actual_tokens,
                ),
                receipt=make_receipt(failure=failure),
            )
            self._event(RuntimeEventType.OPERATIONAL_REQUEST_SETTLED, reservation)
            raise EvidenceResolutionError(
                ResolutionFailureKind.INVALID_PAYLOAD,
                str(exc),
                resolver_id=descriptor.resolver_id,
            ) from exc
        ledger = self._ledger()
        self._persist(
            transition_request(
                ledger,
                attempt_id,
                ReservationStatus.SUCCEEDED,
                expected_revision=ledger.revision,
                actual_tokens=actual_tokens,
            ),
            receipt=receipt,
        )
        self._event(RuntimeEventType.OPERATIONAL_REQUEST_SETTLED, reservation)
        return observation
