from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.investigation.requirements import (
    EvidenceRequirement,
    Observation,
    ObservationValidity,
    ResolutionFailureKind,
    ResolutionFailureRecord,
)
from redteam_core.knowledge.recompute import SemanticRecomputer
from redteam_core.knowledge.state import CommitResult, CoreSession
from redteam_core.models.base import SchemaModel
from redteam_core.models.provenance import Provenance, TrustClass
from redteam_core.runtime.clock import RuntimeClock


class EvidenceResolutionError(RuntimeError):
    def __init__(
        self,
        kind: ResolutionFailureKind,
        detail: str,
        *,
        retryable: bool = False,
        resolver_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.retryable = retryable
        self.resolver_id = resolver_id


@dataclass(frozen=True)
class ResolverOutput:
    payload: BaseModel
    validity: ObservationValidity = field(default_factory=ObservationValidity)
    evidence_ids: tuple[str, ...] = ()


class ResolverDescriptor(SchemaModel):
    resolver_id: str = Field(min_length=1, max_length=200)
    provider_id: str = Field(min_length=1, max_length=120)
    supported_requirement_type_ids: frozenset[str] = Field(min_length=1)
    required_permissions: tuple[str, ...] = ()
    max_calls: int = Field(default=1, ge=1, le=10_000)
    timeout_seconds: float = Field(default=15.0, gt=0.0, le=3_600.0)
    trust_rank: int = Field(default=50, ge=0, le=100)
    cost_rank: int = Field(default=50, ge=0, le=100)
    output_trust_class: TrustClass = TrustClass.OBSERVED
    side_effect_free: bool = True


class EvidenceResolver(Protocol):
    descriptor: ResolverDescriptor

    async def resolve(self, requirement: EvidenceRequirement) -> ResolverOutput: ...


class ResolverRuntimeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, EvidenceResolver] = {}
        self._call_counts: dict[str, int] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, resolver: EvidenceResolver) -> None:
        if self._frozen:
            raise RuntimeError("resolver registry is frozen")
        descriptor = resolver.descriptor
        if descriptor.resolver_id in self._by_id:
            raise ValueError(f"duplicate resolver id: {descriptor.resolver_id}")
        if not descriptor.side_effect_free:
            raise ValueError(
                "evidence resolvers must be side-effect-free; use experiment execution later"
            )
        self._by_id[descriptor.resolver_id] = resolver
        self._call_counts[descriptor.resolver_id] = 0

    def freeze(self) -> None:
        self._frozen = True

    def call_count(self, resolver_id: str) -> int:
        if resolver_id not in self._by_id:
            raise KeyError(f"unknown resolver id: {resolver_id}")
        return self._call_counts[resolver_id]

    def descriptors(self) -> tuple[ResolverDescriptor, ...]:
        return tuple(
            self._by_id[resolver_id].descriptor for resolver_id in sorted(self._by_id)
        )

    def _compatible(self, requirement: EvidenceRequirement) -> tuple[EvidenceResolver, ...]:
        return tuple(
            resolver
            for resolver in self._by_id.values()
            if resolver.descriptor.provider_id == requirement.provider_id
            and requirement.requirement_type_id
            in resolver.descriptor.supported_requirement_type_ids
        )

    def eligible(
        self,
        requirement: EvidenceRequirement,
        *,
        available_permissions: frozenset[str] = frozenset(),
    ) -> tuple[EvidenceResolver, ...]:
        if not self._frozen:
            raise RuntimeError("resolver registry must be frozen before selection")
        eligible = [
            resolver
            for resolver in self._compatible(requirement)
            if set(resolver.descriptor.required_permissions) <= set(available_permissions)
            and self._call_counts[resolver.descriptor.resolver_id] < resolver.descriptor.max_calls
        ]
        eligible.sort(
            key=lambda resolver: (
                -resolver.descriptor.trust_rank,
                resolver.descriptor.cost_rank,
                resolver.descriptor.resolver_id,
            )
        )
        return tuple(eligible)

    def select(
        self,
        requirement: EvidenceRequirement,
        *,
        available_permissions: frozenset[str] = frozenset(),
    ) -> EvidenceResolver:
        compatible = self._compatible(requirement)
        if not compatible:
            raise EvidenceResolutionError(
                ResolutionFailureKind.UNSUPPORTED,
                f"no resolver registered for {requirement.requirement_type_id}",
            )
        permission_eligible = tuple(
            resolver
            for resolver in compatible
            if set(resolver.descriptor.required_permissions) <= set(available_permissions)
        )
        if not permission_eligible:
            raise EvidenceResolutionError(
                ResolutionFailureKind.AUTH_REQUIRED,
                (
                    f"registered resolvers for {requirement.requirement_type_id} "
                    "require unavailable permissions"
                ),
            )
        eligible = self.eligible(
            requirement,
            available_permissions=available_permissions,
        )
        if not eligible:
            raise EvidenceResolutionError(
                ResolutionFailureKind.BUDGET_EXHAUSTED,
                f"resolver call budget exhausted for {requirement.requirement_type_id}",
            )
        return eligible[0]

    def reserve_call(self, resolver_id: str) -> None:
        if not self._frozen:
            raise RuntimeError("resolver registry must be frozen before execution")
        resolver = self._by_id.get(resolver_id)
        if resolver is None:
            raise KeyError(f"unknown resolver id: {resolver_id}")
        used = self._call_counts[resolver_id]
        if used >= resolver.descriptor.max_calls:
            raise EvidenceResolutionError(
                ResolutionFailureKind.BUDGET_EXHAUSTED,
                f"resolver call budget exhausted: {resolver_id}",
                resolver_id=resolver_id,
            )
        self._call_counts[resolver_id] = used + 1


class ResolutionAttempt(SchemaModel):
    observation: Observation | None = None
    failure: ResolutionFailureRecord | None = None

    def model_post_init(self, __context) -> None:
        if bool(self.observation) == bool(self.failure):
            raise ValueError("resolution attempt must contain exactly one observation or failure")


def _matching_payload_registration(
    *,
    payload: BaseModel,
    requirement: EvidenceRequirement,
    catalog: ExtensionCatalog,
):
    requirement_registration = catalog.requirement_types.require(requirement.requirement_type_id)
    matches = []
    for type_id in sorted(requirement_registration.allowed_observation_payload_type_ids):
        registration = catalog.observation_payloads.require(type_id)
        if isinstance(payload, registration.model):
            matches.append(registration)
    if not matches:
        raise EvidenceResolutionError(
            ResolutionFailureKind.INVALID_PAYLOAD,
            "resolver returned a payload that is not allowed by the requirement schema",
        )
    if len(matches) > 1:
        raise EvidenceResolutionError(
            ResolutionFailureKind.INVALID_PAYLOAD,
            "resolver payload matches more than one registered observation schema",
        )
    return matches[0]


async def resolve_to_observation(
    *,
    requirement: EvidenceRequirement,
    resolver_registry: ResolverRuntimeRegistry,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
    available_permissions: frozenset[str] = frozenset(),
) -> Observation:
    resolver = resolver_registry.select(
        requirement,
        available_permissions=available_permissions,
    )
    descriptor = resolver.descriptor
    resolver_registry.reserve_call(descriptor.resolver_id)
    try:
        resolved = await asyncio.wait_for(
            resolver.resolve(requirement), timeout=descriptor.timeout_seconds
        )
    except TimeoutError as exc:
        raise EvidenceResolutionError(
            ResolutionFailureKind.TIMEOUT,
            f"resolver timed out after {descriptor.timeout_seconds:g}s",
            retryable=True,
            resolver_id=descriptor.resolver_id,
        ) from exc
    except EvidenceResolutionError as exc:
        if exc.resolver_id is None:
            exc.resolver_id = descriptor.resolver_id
        raise
    except Exception as exc:
        raise EvidenceResolutionError(
            ResolutionFailureKind.TRANSIENT,
            f"resolver failed: {type(exc).__name__}: {exc}",
            retryable=True,
            resolver_id=descriptor.resolver_id,
        ) from exc

    if not isinstance(resolved, ResolverOutput):
        raise EvidenceResolutionError(
            ResolutionFailureKind.INVALID_PAYLOAD,
            "resolver must return ResolverOutput",
            resolver_id=descriptor.resolver_id,
        )
    registration = _matching_payload_registration(
        payload=resolved.payload,
        requirement=requirement,
        catalog=catalog,
    )
    envelope = registration.envelope_from_model(resolved.payload)
    acquired_at = clock.now()
    provenance = Provenance(
        source_kind="resolver",
        producer_id=descriptor.resolver_id,
        trust_class=descriptor.output_trust_class,
        acquired_at=acquired_at,
        content_hash=envelope.content_hash,
        location_ref=requirement.subject_ref,
    )
    return Observation.create(
        requirement_id=requirement.requirement_id,
        resolver_id=descriptor.resolver_id,
        subject_ref=requirement.subject_ref,
        payload=envelope,
        provenance=provenance,
        validity=resolved.validity,
        evidence_ids=list(resolved.evidence_ids),
    )


async def resolve_and_record(
    *,
    session: CoreSession,
    requirement: EvidenceRequirement,
    resolver_registry: ResolverRuntimeRegistry,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
    available_permissions: frozenset[str] = frozenset(),
) -> ResolutionAttempt:
    try:
        observation = await resolve_to_observation(
            requirement=requirement,
            resolver_registry=resolver_registry,
            catalog=catalog,
            clock=clock,
            available_permissions=available_permissions,
        )
        return ResolutionAttempt(observation=observation)
    except EvidenceResolutionError as exc:
        resolver_id = exc.resolver_id
        failure = ResolutionFailureRecord.create(
            requirement_id=requirement.requirement_id,
            resolver_id=resolver_id,
            kind=exc.kind,
            detail=str(exc),
            retryable=exc.retryable,
            occurred_at=clock.now(),
        )
        session.investigation.resolution_failures[failure.failure_id] = failure
        return ResolutionAttempt(failure=failure)


async def resolve_and_commit(
    *,
    session: CoreSession,
    requirement: EvidenceRequirement,
    resolver_registry: ResolverRuntimeRegistry,
    catalog: ExtensionCatalog,
    clock: RuntimeClock,
    expected_revision: int,
    available_permissions: frozenset[str] = frozenset(),
    recomputers: tuple[SemanticRecomputer, ...] = (),
) -> tuple[ResolutionAttempt, CommitResult | None]:
    """Execute one passive resolution attempt and atomically commit successful semantics."""

    attempt = await resolve_and_record(
        session=session,
        requirement=requirement,
        resolver_registry=resolver_registry,
        catalog=catalog,
        clock=clock,
        available_permissions=available_permissions,
    )
    if attempt.observation is None:
        return attempt, None

    # Local import keeps the investigation/runtime dependency directed at the knowledge commit
    # boundary without creating a package import cycle during module initialization.
    from redteam_core.knowledge.commit import apply_observation

    commit = apply_observation(
        session,
        attempt.observation,
        catalog=catalog,
        evaluation_time=clock.now(),
        expected_revision=expected_revision,
        recomputers=recomputers,
    )
    return attempt, commit
