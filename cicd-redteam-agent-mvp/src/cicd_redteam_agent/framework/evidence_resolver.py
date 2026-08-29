from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.investigation import (
    ExternalEvidencePayload,
    InvestigationTask,
    InvestigationTaskKind,
    Observation,
    ObservationKind,
    ObservationSource,
    RedTeamSession,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, EvidenceRequirementKind


class ResolutionFailureKind(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    TRANSIENT = "TRANSIENT"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    AMBIGUOUS_NOT_FOUND = "AMBIGUOUS_NOT_FOUND"


class EvidenceResolutionError(RuntimeError):
    def __init__(
        self,
        kind: ResolutionFailureKind,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.kind = kind
        self.retryable = retryable


@dataclass(frozen=True)
class ResolverDescriptor:
    resolver_id: str
    provider: EvidenceProvider
    supported_kinds: frozenset[EvidenceRequirementKind]
    required_permissions: tuple[str, ...] = ()
    max_calls: int = 1
    timeout_seconds: float = 15.0


class EvidenceResolver(Protocol):
    descriptor: ResolverDescriptor
    observation_source: ObservationSource

    async def resolve(
        self,
        session: RedTeamSession,
        requirement: EvidenceRequirement,
    ) -> ExternalEvidencePayload: ...


class ResolverRegistry:
    def __init__(self, resolvers: tuple[EvidenceResolver, ...] = ()) -> None:
        self._by_kind: dict[EvidenceRequirementKind, EvidenceResolver] = {}
        self._by_id: dict[str, EvidenceResolver] = {}
        for resolver in resolvers:
            self.register(resolver)

    def register(self, resolver: EvidenceResolver) -> None:
        descriptor = resolver.descriptor
        if descriptor.resolver_id in self._by_id:
            raise ValueError(f"duplicate resolver id: {descriptor.resolver_id}")
        for kind in descriptor.supported_kinds:
            if kind in self._by_kind:
                raise ValueError(f"duplicate resolver for evidence requirement: {kind.value}")
        self._by_id[descriptor.resolver_id] = resolver
        for kind in descriptor.supported_kinds:
            self._by_kind[kind] = resolver

    def descriptor_for(self, kind: EvidenceRequirementKind) -> ResolverDescriptor | None:
        resolver = self._by_kind.get(kind)
        return resolver.descriptor if resolver is not None else None

    def has_resolver(self, kind: EvidenceRequirementKind) -> bool:
        return kind in self._by_kind

    def runtime_metrics_snapshot(self) -> dict[str, int | None]:
        totals: dict[str, int | None] = {
            "network_requests": 0,
            "cache_hits": 0,
            "not_modified_hits": 0,
            "rate_limit_errors": 0,
            "retries": 0,
            "rate_limit_remaining": None,
        }
        remaining_values: list[int] = []
        for resolver in self._by_id.values():
            metrics = getattr(resolver, "metrics", None)
            if metrics is None:
                continue
            for field_name in (
                "network_requests",
                "cache_hits",
                "not_modified_hits",
                "rate_limit_errors",
                "retries",
            ):
                value = getattr(metrics, field_name, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[field_name] = int(totals[field_name] or 0) + value
            remaining = getattr(metrics, "rate_limit_remaining", None)
            if isinstance(remaining, int) and not isinstance(remaining, bool):
                remaining_values.append(remaining)
        if remaining_values:
            totals["rate_limit_remaining"] = min(remaining_values)
        return totals

    async def observe(
        self,
        session: RedTeamSession,
        task: InvestigationTask,
    ) -> Observation:
        if task.kind is not InvestigationTaskKind.RESOLVE_EXTERNAL_EVIDENCE:
            raise ValueError(f"resolver registry cannot execute legacy task kind: {task.kind}")
        if task.requirement_id is None or task.resolver_id is None:
            raise ValueError("external evidence task is missing requirement or resolver id")
        requirement = session.investigation_state.requirements.get(task.requirement_id)
        if requirement is None:
            raise ValueError("external evidence task references an unknown requirement")
        resolver = self._by_kind.get(requirement.kind)
        if resolver is None:
            raise EvidenceResolutionError(
                ResolutionFailureKind.UNSUPPORTED,
                f"no resolver registered for {requirement.kind.value}",
            )
        if resolver.descriptor.resolver_id != task.resolver_id:
            raise ValueError("task resolver does not match resolver registry")
        if resolver.descriptor.provider is not requirement.provider:
            raise ValueError("resolver provider does not match evidence requirement")

        payload = await resolver.resolve(session, requirement)
        if payload.type != requirement.kind.value:
            raise ValueError("resolver returned a payload for the wrong evidence requirement")
        observation_id = stable_id(
            "obs",
            session.analysis_state.repository.repository_id,
            task.task_id,
            requirement.requirement_id,
            resolver.descriptor.resolver_id,
            payload.model_dump_json(),
        )
        return Observation(
            observation_id=observation_id,
            task_id=task.task_id,
            kind=ObservationKind.EXTERNAL_EVIDENCE,
            source=resolver.observation_source,
            subject_id=task.subject_id,
            requirement_id=requirement.requirement_id,
            resolver_id=resolver.descriptor.resolver_id,
            payload=payload,
        )
