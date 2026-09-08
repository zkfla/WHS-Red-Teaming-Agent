"""Synthetic scale fixtures. Construction/verification are outside operation timing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from redteam_core.graph.frontier import FrontierQuery, build_attack_frontiers
from redteam_core.investigation import (
    EvidenceRequirement,
    Observation,
    ResolverDescriptor,
    ResolverOutput,
    ResolverRuntimeRegistry,
    resolve_to_observation,
)
from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.knowledge import CoreSession, apply_observations_atomic, refresh_freshness
from redteam_core.knowledge.recompute import effective_payload_envelope
from redteam_core.models import (
    ClaimContract,
    ClaimStatus,
    Provenance,
    RootCauseRef,
    RuntimeState,
    SecurityCandidate,
    TrustClass,
)
from redteam_core.models.base import canonical_json
from redteam_core.reasoning import project_decision_context
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from tests.helpers import OBSERVATION_TYPE, REQUIREMENT_TYPE, SamplePayload, catalog
from tests.performance.graph_adapter import graph_input
from tests.performance.oracles import Edge

NOW = datetime(2026, 9, 8, 3, tzinfo=UTC)


@dataclass(frozen=True)
class Case:
    stage: str
    size: int
    cycles: int = 1

    def __post_init__(self):
        if self.stage not in {
            "commit",
            "freshness",
            "frontier",
            "frontier_partial",
            "context",
            "checkpoint",
            "resolve",
            "payload",
        }:
            raise ValueError("unsupported benchmark stage")
        if not 1 <= self.size <= 100_000 or self.cycles not in {1, 10, 100}:
            raise ValueError("case bounds exceeded")
        if self.stage in {"frontier", "frontier_partial"} and self.size > 10_000:
            raise ValueError("frontier fixture exceeds relation scale bound")
        if self.stage == "frontier_partial" and self.size < 18:
            raise ValueError("partial fixture needs 17 suffix relations and one source")
        if self.stage == "resolve" and self.size > 10_000:
            raise ValueError("resolver fixture exceeds call scale bound")


def knowledge_input(size: int):
    cat = catalog()
    requirements = tuple(
        EvidenceRequirement.create(
            requirement_type_id=REQUIREMENT_TYPE,
            provider_id="reference.local",
            subject_ref=f"subject:{i}",
            reason="synthetic scale baseline",
        )
        for i in range(max(1, size // 10))
    )
    registration = cat.observation_payloads.require(OBSERVATION_TYPE)
    envelopes = [registration.envelope_from_model(SamplePayload(value=str(i))) for i in range(4)]
    observations = []
    for i in range(size):
        req = requirements[i % len(requirements)]
        payload = envelopes[(i % len(requirements)) % 4]
        observations.append(
            Observation.create(
                requirement_id=req.requirement_id,
                resolver_id="reference.resolver.scale@1",
                subject_ref=req.subject_ref,
                payload=payload,
                provenance=Provenance(
                    source_kind="synthetic",
                    producer_id="reference.resolver.scale@1",
                    trust_class=TrustClass.OBSERVED,
                    acquired_at=NOW - timedelta(microseconds=i),
                    content_hash=payload.content_hash,
                ),
            )
        )
    session = CoreSession()
    session.investigation.requirements = {r.requirement_id: r for r in requirements}
    return session, cat, tuple(observations)


def prepare(
    case: Case,
    *,
    artifact_root: str | None = None,
    lookup_mode: str = "full_scan",
    memoize_sink: bool = False,
):
    """Return independent operation/verification closures for one sample."""
    if case.stage == "resolve":
        cat = catalog()
        req = EvidenceRequirement.create(
            requirement_type_id=REQUIREMENT_TYPE,
            provider_id="reference.local",
            subject_ref="scale:resolve",
            reason="synthetic passive call",
        )

        class FakeResolver:
            descriptor = ResolverDescriptor(
                resolver_id="reference.resolver.scale@1",
                provider_id="reference.local",
                supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
                max_calls=case.size,
            )

            async def resolve(self, requirement):
                return ResolverOutput(payload=SamplePayload(value="synthetic"))

        registry = ResolverRuntimeRegistry()
        registry.register(FakeResolver())
        registry.freeze()

        async def resolve_batch():
            return [
                await resolve_to_observation(
                    requirement=req, resolver_registry=registry, catalog=cat, clock=FixedClock(NOW)
                )
                for _ in range(case.size)
            ]

        def verify_resolutions(result):
            assert len(result) == case.size
            assert registry.call_count(FakeResolver.descriptor.resolver_id) == case.size
            assert all(item == result[0] for item in result)
            return [item.model_dump(mode="json") for item in result]

        return lambda: asyncio.run(resolve_batch()), verify_resolutions

    if case.stage in {"frontier", "frontier_partial"}:
        edges = (
            Edge("s", "a", "source"),
            *(Edge("a", f"z{i}", "sink") for i in range(case.size - 1)),
        )
        if case.stage == "frontier_partial":
            edges = (
                *(Edge(f"s{i}", "a0", "source", verdict="FALSE") for i in range(case.size - 17)),
                *(Edge(f"a{i}", f"a{i + 1}", "flow") for i in range(16)),
                Edge("a16", "target", "sink"),
            )
        session, cat = graph_input(edges)

        def operation():
            return build_attack_frontiers(
                session,
                catalog=cat,
                query=FrontierQuery(max_expansions=100_000, max_frontiers=100_000, max_depth=32),
                memoize_sink=memoize_sink,
            )

        def verify(result):
            assert result.search_complete and not result.truncated
            if case.stage == "frontier_partial":
                assert len(result.frontiers) == case.size - 17
                assert all(f.potential_sink_ref == "target" for f in result.frontiers)
            elif case.size > 1:
                assert len(result.frontiers) == case.size - 1
            return result.model_dump(mode="json")

        return operation, verify

    session, cat, observations = knowledge_input(case.size)
    if case.stage == "context":
        populate_context_candidates(session, min(1000, max(10, case.size // 10)))
    if case.stage != "commit":
        apply_observations_atomic(
            session,
            observations,
            catalog=cat,
            evaluation_time=NOW,
            expected_revision=0,
        )

    if case.stage == "checkpoint":
        directory = TemporaryDirectory(prefix="generic-core-benchmark-", dir=artifact_root)
        clock = FixedClock(NOW)
        runtime = RuntimeState(run_id="benchmark:fixed")
        journal = EventJournal(
            Path(directory.name) / "journal.jsonl", run_id=runtime.run_id, clock=clock
        )
        store = AtomicCheckpointStore(Path(directory.name) / "checkpoint.json")
        fingerprint = build_runtime_composition_fingerprint(catalog=cat)

        def write_checkpoint():
            for _ in range(case.cycles):
                result = store.write(
                    session=session,
                    runtime_state=runtime,
                    catalog=cat,
                    composition_fingerprint=fingerprint,
                    journal=journal,
                    clock=clock,
                    reason="synthetic-benchmark",
                )
            return result

        def verify_checkpoint(result):
            try:
                loaded = store.load(
                    catalog=cat,
                    composition_fingerprint=fingerprint,
                    journal=journal,
                    expected_run_id=runtime.run_id,
                )
                assert loaded == result
                assert loaded.session == session
                return result.model_dump(mode="json")
            finally:
                directory.cleanup()

        return write_checkpoint, verify_checkpoint

    def operation():
        if case.stage == "commit":
            for step in range(case.cycles):
                batch = observations[
                    step * len(observations) // case.cycles : (step + 1)
                    * len(observations)
                    // case.cycles
                ]
                apply_observations_atomic(
                    session,
                    batch,
                    catalog=cat,
                    evaluation_time=NOW,
                    expected_revision=session.knowledge.revision,
                    lookup_mode=lookup_mode,
                )
        elif case.stage == "freshness":
            for _ in range(case.cycles):
                refresh_freshness(
                    session,
                    evaluation_time=NOW,
                    expected_revision=session.knowledge.revision,
                    lookup_mode=lookup_mode,
                )
        elif case.stage == "payload":
            return [
                effective_payload_envelope(session, req, lookup_mode=lookup_mode)
                for req in sorted(session.investigation.requirements)
            ]
        else:
            return project_decision_context(session)
        return session

    def verify(result):
        assert len(session.investigation.observations) == case.size
        assert all(slot.status.value == "CONSISTENT" for slot in session.knowledge.slots.values())
        if case.stage == "context":
            assert len(result.content) <= 12_000
        if case.stage == "payload":
            assert all(item is not None for item in result)
            return [item.model_dump(mode="json") for item in result]
        return result.model_dump(mode="json")

    return operation, verify


def semantic_digest(result: object) -> str:
    return sha256(canonical_json(result).encode("utf-8")).hexdigest()


def populate_context_candidates(session: CoreSession, count: int) -> None:
    """Pathless candidates backed by real requirement refs and coverage entries."""
    if not 1 <= count <= 1000 or not session.investigation.requirements:
        raise ValueError("bounded candidates and requirements required")
    requirements = sorted(session.investigation.requirements)
    contract = ClaimContract.create(
        claim_type_id="reference.candidate.value@1", title="Synthetic state invariant"
    )
    candidates = tuple(
        SecurityCandidate.create(
            candidate_type_id="reference.candidate.value@1",
            semantic_key=f"state:{index}",
            root_cause=RootCauseRef(owner_bundle_id="reference", family_id="state", key=str(index)),
            claim_contract=contract,
            max_claim_status=ClaimStatus.SUPPORTED,
            requirement_ids=[requirements[index % len(requirements)]],
            grounding_refs=[f"state:{index}"],
        )
        for index in range(count)
    )
    reconcile_candidate_state(session, candidates)
    CoreSession.model_validate(session.model_dump(mode="python"))
