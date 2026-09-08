from __future__ import annotations

import re
from typing import Protocol

from pydantic import ConfigDict, Field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel
from redteam_core.models.provenance import TYPE_ID_PATTERN
from redteam_core.models.security import SecurityCandidate

_COMPONENT_ID_RE = re.compile(TYPE_ID_PATTERN)


class CandidateDeriverDescriptor(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    deriver_id: str = Field(pattern=TYPE_ID_PATTERN)


class CandidateDeriver(Protocol):
    descriptor: CandidateDeriverDescriptor

    def derive(
        self,
        session: CoreSession,
        catalog: ExtensionCatalog,
    ) -> tuple[SecurityCandidate, ...]: ...


class CandidateDeriverRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, CandidateDeriver] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, deriver: CandidateDeriver) -> None:
        if self._frozen:
            raise RuntimeError("candidate deriver registry is frozen")
        descriptor = deriver.descriptor
        if descriptor.deriver_id in self._by_id:
            raise ValueError(f"duplicate candidate deriver id: {descriptor.deriver_id}")
        self._by_id[descriptor.deriver_id] = deriver

    def freeze(self) -> None:
        self._frozen = True

    def values(self) -> tuple[CandidateDeriver, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))


def _validate_candidate(
    candidate: SecurityCandidate,
    *,
    deriver_id: str,
    session: CoreSession,
    catalog: ExtensionCatalog,
) -> SecurityCandidate:
    validated = SecurityCandidate.model_validate(candidate.model_dump(mode="python"))
    deriver_registration = catalog.candidate_derivers.require(deriver_id)
    if validated.candidate_type_id not in deriver_registration.candidate_type_ids:
        raise ValueError("candidate deriver emitted an undeclared candidate type")
    registration = catalog.candidate_types.require(validated.candidate_type_id)
    if registration.owner_bundle_id != deriver_registration.owner_bundle_id:
        raise ValueError("candidate type owner does not match candidate deriver owner")
    if validated.root_cause.owner_bundle_id != registration.owner_bundle_id:
        raise ValueError("root-cause owner does not match candidate type owner")
    if validated.candidate_payload is not None:
        registration.decode(validated.candidate_payload)

    relation_ids = set(session.semantic.relations)
    for relation_id in (
        *validated.supporting_relation_ids,
        *validated.contradicting_relation_ids,
        *validated.guard_relation_ids,
    ):
        if relation_id not in relation_ids:
            raise ValueError(f"candidate references unknown relation: {relation_id}")
    for requirement_id in validated.requirement_ids:
        if requirement_id not in session.investigation.requirements:
            raise ValueError(f"candidate references unknown requirement: {requirement_id}")
    return validated


def derive_candidates(
    session: CoreSession,
    *,
    catalog: ExtensionCatalog,
    registry: CandidateDeriverRegistry,
) -> tuple[SecurityCandidate, ...]:
    """Derive canonical candidates directly from state without materializing Findings first."""

    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before candidate derivation")
    if not registry.frozen:
        raise RuntimeError("candidate deriver registry must be frozen before derivation")
    runtime_ids = {item.descriptor.deriver_id for item in registry.values()}
    registered_ids = {item.deriver_id for item in catalog.candidate_derivers.values()}
    if runtime_ids != registered_ids:
        raise ValueError("runtime candidate derivers do not match the frozen semantic manifest")
    by_id: dict[str, SecurityCandidate] = {}
    for deriver in registry.values():
        descriptor = deriver.descriptor
        if not _COMPONENT_ID_RE.fullmatch(descriptor.deriver_id):
            raise ValueError("invalid candidate deriver id")
        registration = catalog.candidate_derivers.require(descriptor.deriver_id)
        if registration.owner_bundle_id not in catalog.bundle_manifests:
            raise ValueError("candidate deriver owner bundle is not loaded")
        read_view = session.model_copy(deep=True)
        produced = tuple(
            sorted(
                deriver.derive(read_view, catalog),
                key=lambda item: item.candidate_id,
            )
        )
        local_ids: set[str] = set()
        for candidate in produced:
            validated = _validate_candidate(
                candidate,
                deriver_id=descriptor.deriver_id,
                session=session,
                catalog=catalog,
            )
            if validated.candidate_id in local_ids:
                raise ValueError("candidate deriver emitted a duplicate candidate id")
            local_ids.add(validated.candidate_id)
            existing = by_id.get(validated.candidate_id)
            if existing is not None and existing != validated:
                raise ValueError("candidate identity collision across derivers")
            by_id[validated.candidate_id] = validated
    return tuple(by_id[key] for key in sorted(by_id))
