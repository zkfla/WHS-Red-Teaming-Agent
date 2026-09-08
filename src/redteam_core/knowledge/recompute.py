from __future__ import annotations

import re
from typing import Protocol

from redteam_core.diagnostics.performance import PerformanceStage, performance_span
from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.lookup import ObservationLookupMode
from redteam_core.knowledge.state import CoreSession, KnowledgeSlotStatus, SemanticState
from redteam_core.models.provenance import TypedPayloadEnvelope
from redteam_core.models.security import Relation

_COMPONENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+@[1-9][0-9]*$")


class SemanticRecomputer(Protocol):
    """Pure-ish domain semantic projection invoked after canonical knowledge changes.

    Implementations receive a deep-copied session view from the orchestrator. They return their
    complete owned derived Relation set and must not perform external I/O or mutate authoritative
    state directly.
    """

    recomputer_id: str

    def recompute(
        self,
        session: CoreSession,
        catalog: ExtensionCatalog,
    ) -> tuple[Relation, ...]: ...


def effective_payload_envelope(
    session: CoreSession,
    requirement_id: str,
    *,
    lookup_mode: ObservationLookupMode = ObservationLookupMode.FULL_SCAN,
) -> TypedPayloadEnvelope | None:
    """Return a deterministic effective payload only for a consistent knowledge slot."""

    lookup_mode = ObservationLookupMode(lookup_mode)
    slot = session.knowledge.slots.get(requirement_id)
    if slot is None or slot.status is not KnowledgeSlotStatus.CONSISTENT:
        return None
    effective_hash = slot.effective_payload_hash
    if effective_hash is None:
        return None
    if lookup_mode is ObservationLookupMode.INDEXED:
        for observation_id in sorted(slot.authoritative_observation_ids):
            observation = session.investigation.observations.get(observation_id)
            if (
                observation is not None
                and observation.requirement_id == requirement_id
                and observation.observation_id == observation_id
                and observation.payload.content_hash == effective_hash
            ):
                return observation.payload
        return None
    matches = sorted(
        (
            observation
            for observation in session.investigation.observations.values()
            if observation.requirement_id == requirement_id
            and observation.observation_id in slot.authoritative_observation_ids
            and observation.payload.content_hash == effective_hash
        ),
        key=lambda item: item.observation_id,
    )
    return matches[0].payload if matches else None


@performance_span(
    PerformanceStage.RECOMPUTE,
    input_items=lambda session, **kwargs: len(session.semantic.relations),
)
def recompute_relations(
    session: CoreSession,
    *,
    catalog: ExtensionCatalog,
    recomputers: tuple[SemanticRecomputer, ...],
) -> bool:
    """Recompute all registered derived relations in deterministic component order."""

    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before semantic recompute")
    before_relations = dict(session.semantic.relations)
    before_producers = dict(session.semantic.relation_producers)
    seen_recomputer_ids: set[str] = set()

    for recomputer in sorted(recomputers, key=lambda item: item.recomputer_id):
        recomputer_id = recomputer.recomputer_id
        if not _COMPONENT_ID_RE.fullmatch(recomputer_id):
            raise ValueError(f"invalid semantic recomputer id: {recomputer_id!r}")
        if recomputer_id in seen_recomputer_ids:
            raise ValueError(f"duplicate semantic recomputer id: {recomputer_id}")
        seen_recomputer_ids.add(recomputer_id)

        for relation_id, producer_id in list(session.semantic.relation_producers.items()):
            if producer_id == recomputer_id:
                session.semantic.relation_producers.pop(relation_id, None)
                session.semantic.relations.pop(relation_id, None)

        # A recomputer cannot mutate authoritative state through its input reference.
        read_view = session.model_copy(deep=True)
        produced = tuple(
            sorted(
                recomputer.recompute(read_view, catalog),
                key=lambda item: item.relation_id,
            )
        )
        local_ids: set[str] = set()
        for relation in produced:
            if relation.relation_id in local_ids:
                raise ValueError(
                    f"semantic recomputer {recomputer_id} emitted duplicate relation id "
                    f"{relation.relation_id}"
                )
            local_ids.add(relation.relation_id)
            catalog.relation_types.require(relation.relation_type_id)
            existing = session.semantic.relations.get(relation.relation_id)
            if existing is not None:
                owner = session.semantic.relation_producers.get(relation.relation_id, "unowned")
                raise ValueError(
                    f"semantic relation collision for {relation.relation_id}: "
                    f"existing owner {owner}, new owner {recomputer_id}"
                )
            session.semantic.relations[relation.relation_id] = relation
            session.semantic.relation_producers[relation.relation_id] = recomputer_id

    # Re-run the cross-ledger invariant after dictionary mutations performed on the private copy.
    session.semantic = SemanticState.model_validate(session.semantic.model_dump(mode="python"))
    return (
        session.semantic.relations != before_relations
        or session.semantic.relation_producers != before_producers
    )
