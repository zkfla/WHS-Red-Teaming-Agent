from __future__ import annotations

from datetime import datetime

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.investigation.requirements import EvidenceRequirement, Observation
from redteam_core.knowledge.recompute import SemanticRecomputer, recompute_relations
from redteam_core.knowledge.state import (
    CommitResult,
    CoreSession,
    KnowledgeSlot,
    KnowledgeSlotStatus,
)
from redteam_core.models.provenance import TrustClass


class SemanticCommitConflict(RuntimeError):
    pass


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation_time must be timezone-aware")


def _is_fresh(
    requirement: EvidenceRequirement,
    observation: Observation,
    evaluation_time: datetime,
) -> bool:
    _require_aware(evaluation_time)
    acquired_at = observation.provenance.acquired_at
    age = (evaluation_time - acquired_at).total_seconds()
    if age < 0:
        raise ValueError("observation acquired_at is later than evaluation_time")
    max_age = requirement.freshness.max_age_seconds
    if max_age is not None and age > max_age:
        return False
    required_snapshot = requirement.freshness.required_snapshot_ref
    if required_snapshot is not None and observation.validity.snapshot_ref != required_snapshot:
        return False
    if (
        observation.validity.not_after is not None
        and evaluation_time > observation.validity.not_after
    ):
        return False
    return True


def _validate_observation(
    *,
    session: CoreSession,
    observation: Observation,
    catalog: ExtensionCatalog,
) -> EvidenceRequirement:
    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before semantic commits")
    # Re-validate objects at the authoritative commit boundary because callers can hold models
    # produced by unchecked copies/deserialization helpers outside the canonical runtime path.
    validated_observation = Observation.model_validate(observation.model_dump(mode="python"))
    requirement = session.investigation.requirements.get(validated_observation.requirement_id)
    if requirement is None:
        raise ValueError("observation references unknown requirement")
    requirement = EvidenceRequirement.model_validate(requirement.model_dump(mode="python"))
    if validated_observation.subject_ref != requirement.subject_ref:
        raise ValueError("observation subject does not match requirement")
    registration = catalog.requirement_types.require(requirement.requirement_type_id)
    if (
        validated_observation.payload.type_id
        not in registration.allowed_observation_payload_type_ids
    ):
        raise ValueError("observation payload type is not allowed for requirement")
    payload_registration = catalog.observation_payloads.require(
        validated_observation.payload.type_id
    )
    payload_registration.decode(validated_observation.payload)
    return requirement


def _build_slot(
    *,
    requirement: EvidenceRequirement,
    observations: tuple[Observation, ...],
    evaluation_time: datetime,
) -> KnowledgeSlot:
    fresh: list[Observation] = []
    stale_ids: list[str] = []
    for observation in sorted(observations, key=lambda item: item.observation_id):
        if _is_fresh(requirement, observation, evaluation_time):
            fresh.append(observation)
        else:
            stale_ids.append(observation.observation_id)

    authoritative = [
        item for item in fresh if item.provenance.trust_class is not TrustClass.INFERRED
    ]
    inferred = [item for item in fresh if item.provenance.trust_class is TrustClass.INFERRED]
    hashes = tuple(sorted({item.payload.content_hash for item in authoritative}))
    if not hashes:
        status = KnowledgeSlotStatus.UNKNOWN
        effective_hash = None
    elif len(hashes) == 1:
        status = KnowledgeSlotStatus.CONSISTENT
        effective_hash = hashes[0]
    else:
        status = KnowledgeSlotStatus.CONFLICTING
        effective_hash = None
    return KnowledgeSlot(
        requirement_id=requirement.requirement_id,
        status=status,
        effective_payload_hash=effective_hash,
        authoritative_payload_hashes=hashes,
        observation_ids=tuple(sorted(item.observation_id for item in fresh)),
        authoritative_observation_ids=tuple(
            sorted(item.observation_id for item in authoritative)
        ),
        inferred_observation_ids=tuple(sorted(item.observation_id for item in inferred)),
        stale_observation_ids=tuple(sorted(stale_ids)),
    )


def _recompute_slot(
    *,
    session: CoreSession,
    requirement_id: str,
    evaluation_time: datetime,
) -> KnowledgeSlot:
    requirement = session.investigation.requirements[requirement_id]
    observations = tuple(
        item
        for item in session.investigation.observations.values()
        if item.requirement_id == requirement_id
    )
    return _build_slot(
        requirement=requirement,
        observations=observations,
        evaluation_time=evaluation_time,
    )


def apply_observations_atomic(
    session: CoreSession,
    observations: tuple[Observation, ...],
    *,
    catalog: ExtensionCatalog,
    evaluation_time: datetime,
    expected_revision: int,
    recomputers: tuple[SemanticRecomputer, ...] = (),
) -> CommitResult:
    """Record a batch and commit its semantic effect exactly once.

    The batch is validated on a deep copy first. Observation order is canonicalized, and all
    affected knowledge slots are recomputed from the complete ledger before one revision bump.
    """

    _require_aware(evaluation_time)
    if expected_revision != session.knowledge.revision:
        raise SemanticCommitConflict(
            f"expected revision {expected_revision}, current {session.knowledge.revision}"
        )
    candidate = session.model_copy(deep=True)
    recorded: list[str] = []
    duplicates: list[str] = []
    affected: set[str] = set()

    for observation in sorted(observations, key=lambda item: item.observation_id):
        existing = candidate.investigation.observations.get(observation.observation_id)
        if existing is not None:
            if existing != observation:
                raise ValueError("observation ID collision with different content")
            duplicates.append(observation.observation_id)
            continue
        _validate_observation(session=candidate, observation=observation, catalog=catalog)
        candidate.investigation.observations[observation.observation_id] = observation
        recorded.append(observation.observation_id)
        affected.add(observation.requirement_id)

    semantic_changed = False
    for requirement_id in sorted(affected):
        new_slot = _recompute_slot(
            session=candidate,
            requirement_id=requirement_id,
            evaluation_time=evaluation_time,
        )
        if candidate.knowledge.slots.get(requirement_id) != new_slot:
            candidate.knowledge.slots[requirement_id] = new_slot
            semantic_changed = True

    relation_changed = False
    if semantic_changed and recomputers:
        relation_changed = recompute_relations(
            candidate,
            catalog=catalog,
            recomputers=recomputers,
        )

    if semantic_changed:
        candidate.knowledge.revision += 1

    # Commit both ledgers only after every validation/recompute step succeeds.
    session.investigation = candidate.investigation
    session.knowledge = candidate.knowledge
    session.semantic = candidate.semantic
    return CommitResult(
        accepted=True,
        recorded_observation_ids=tuple(sorted(recorded)),
        duplicate_observation_ids=tuple(sorted(duplicates)),
        affected_requirement_ids=tuple(sorted(affected)),
        recomputed_relation_ids=(
            tuple(sorted(session.semantic.relations)) if relation_changed else ()
        ),
        semantic_changed=semantic_changed or relation_changed,
        revision=session.knowledge.revision,
    )


def apply_observation(
    session: CoreSession,
    observation: Observation,
    *,
    catalog: ExtensionCatalog,
    evaluation_time: datetime,
    expected_revision: int,
    recomputers: tuple[SemanticRecomputer, ...] = (),
) -> CommitResult:
    return apply_observations_atomic(
        session,
        (observation,),
        catalog=catalog,
        evaluation_time=evaluation_time,
        expected_revision=expected_revision,
        recomputers=recomputers,
    )


def refresh_freshness(
    session: CoreSession,
    *,
    evaluation_time: datetime,
    expected_revision: int,
    catalog: ExtensionCatalog | None = None,
    recomputers: tuple[SemanticRecomputer, ...] = (),
) -> CommitResult:
    _require_aware(evaluation_time)
    if expected_revision != session.knowledge.revision:
        raise SemanticCommitConflict(
            f"expected revision {expected_revision}, current {session.knowledge.revision}"
        )
    candidate = session.model_copy(deep=True)
    changed_requirements: list[str] = []
    for requirement_id in sorted(candidate.investigation.requirements):
        new_slot = _recompute_slot(
            session=candidate,
            requirement_id=requirement_id,
            evaluation_time=evaluation_time,
        )
        if candidate.knowledge.slots.get(requirement_id) != new_slot:
            candidate.knowledge.slots[requirement_id] = new_slot
            changed_requirements.append(requirement_id)
    relation_changed = False
    if changed_requirements and recomputers:
        if catalog is None:
            raise ValueError("catalog is required when semantic recomputers are configured")
        relation_changed = recompute_relations(
            candidate,
            catalog=catalog,
            recomputers=recomputers,
        )
    if changed_requirements:
        candidate.knowledge.revision += 1
        session.knowledge = candidate.knowledge
        session.semantic = candidate.semantic
    return CommitResult(
        accepted=True,
        affected_requirement_ids=tuple(changed_requirements),
        recomputed_relation_ids=(
            tuple(sorted(session.semantic.relations)) if relation_changed else ()
        ),
        semantic_changed=bool(changed_requirements) or relation_changed,
        revision=session.knowledge.revision,
    )
