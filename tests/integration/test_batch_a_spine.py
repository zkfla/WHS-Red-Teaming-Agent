from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from redteam_core.investigation import (
    ResolverDescriptor,
    ResolverOutput,
    ResolverRuntimeRegistry,
    resolve_and_commit,
)
from redteam_core.knowledge import (
    CoreSession,
    KnowledgeSlotStatus,
    apply_observation,
    effective_payload_envelope,
)
from redteam_core.models import Relation, TriState
from redteam_core.runtime import FixedClock
from tests.helpers import SamplePayload, catalog, observation, requirement

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
RELATION_TYPE = "reference.relation.flows@1"


@dataclass
class ReferenceResolver:
    descriptor: ResolverDescriptor
    value: str

    async def resolve(self, requirement):
        return ResolverOutput(payload=SamplePayload(value=self.value))


@dataclass(frozen=True)
class ReferenceRecomputer:
    requirement_id: str
    recomputer_id: str = "reference.recomputer.access@1"

    def recompute(self, session, catalog):
        envelope = effective_payload_envelope(session, self.requirement_id)
        verdict = TriState.UNKNOWN
        if envelope is not None:
            registration = catalog.observation_payloads.require(envelope.type_id)
            payload = registration.decode(envelope)
            if payload.value == "allow":
                verdict = TriState.TRUE
            elif payload.value == "deny":
                verdict = TriState.FALSE
        return (
            Relation.create(
                source_ref="actor:reference",
                relation_type_id=RELATION_TYPE,
                target_ref="resource:reference",
                verdict=verdict,
                requirement_ids=[self.requirement_id],
            ),
        )


async def test_requirement_resolver_observation_recompute_spine_and_conflict_fails_closed():
    req = requirement()
    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    runtime_catalog = catalog()
    registry = ResolverRuntimeRegistry()
    registry.register(
        ReferenceResolver(
            ResolverDescriptor(
                resolver_id="reference.resolver.integration@1",
                provider_id="reference.local",
                supported_requirement_type_ids=frozenset({req.requirement_type_id}),
            ),
            value="allow",
        )
    )
    registry.freeze()
    recomputer = ReferenceRecomputer(requirement_id=req.requirement_id)

    attempt, commit = await resolve_and_commit(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=runtime_catalog,
        clock=FixedClock(NOW),
        expected_revision=0,
        recomputers=(recomputer,),
    )

    assert attempt.observation is not None
    assert commit is not None and commit.revision == 1
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.CONSISTENT
    relation = next(iter(session.semantic.relations.values()))
    assert relation.verdict is TriState.TRUE
    assert relation.requirement_ids == [req.requirement_id]

    conflicting = observation(
        req=req,
        value="deny",
        acquired_at=NOW,
        resolver_id="reference.resolver.conflict@1",
    )
    second = apply_observation(
        session,
        conflicting,
        catalog=runtime_catalog,
        evaluation_time=NOW,
        expected_revision=1,
        recomputers=(recomputer,),
    )

    assert second.revision == 2
    assert session.knowledge.slots[req.requirement_id].status is KnowledgeSlotStatus.CONFLICTING
    relation = next(iter(session.semantic.relations.values()))
    assert relation.verdict is TriState.UNKNOWN
