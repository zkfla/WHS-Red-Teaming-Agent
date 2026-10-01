"""Located context is bounded and never becomes a second source of truth."""

from datetime import datetime, timezone

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import EvidenceSourceType, GraphNodeType
from redteam.core.ids import EngagementId, EvidenceId, GraphNodeId, RunId
from redteam.orchestration.agents.definition import ContextProfile
from redteam.orchestration.context_projection import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    ContextProjectionCache,
    ContextProjector,
    project_evidence_excerpts,
)


async def test_projection_keeps_the_target_and_drops_unrelated_nodes() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    target = await service.upsert_node(GraphNodeType.HOST, key="target.local")
    await service.upsert_node(GraphNodeType.HOST, key="unrelated.local")
    await service.upsert_node(GraphNodeType.ASSET, key="app.apk")
    full = await service.observe(step=3)

    projected = await ContextProjector(service).project(
        ContextProfile(
            profile_id="recon",
            max_nodes=5,
            neighbor_depth=0,
            include_node_types=[GraphNodeType.HOST],
            include_hypotheses=False,
            include_findings=False,
        ),
        step=3,
        target_node_ids=[target.id],
        fallback=full,
    )

    assert projected.node_ids(GraphNodeType.HOST) == [target.id]
    assert GraphNodeType.ASSET.value not in projected.node_counts


async def test_projection_obeys_the_declared_node_ceiling() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    targets = [
        await service.upsert_node(GraphNodeType.HOST, key=f"h{index}.local")
        for index in range(5)
    ]
    full = await service.observe(step=1)

    projected = await ContextProjector(service).project(
        ContextProfile(
            profile_id="small",
            max_nodes=2,
            neighbor_depth=0,
            include_hypotheses=False,
            include_findings=False,
        ),
        step=1,
        target_node_ids=[item.id for item in targets],
        fallback=full,
    )

    assert sum(len(ids) for ids in projected.node_ids_by_type.values()) == 2
    assert projected.truncated is True


async def test_projection_does_not_inject_unrelated_open_hypotheses() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    target = await service.upsert_node(GraphNodeType.HOST, key="target.local")
    unrelated = await service.upsert_node(GraphNodeType.HOST, key="other.local")
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp-related", description="related"),
        about=target.id,
    )
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp-unrelated", description="unrelated"),
        about=unrelated.id,
    )
    full = await service.observe(step=1)

    projected = await ContextProjector(service).project(
        ContextProfile(
            profile_id="located",
            neighbor_depth=1,
            include_node_types=[GraphNodeType.HOST, GraphNodeType.HYPOTHESIS],
            include_findings=False,
        ),
        step=1,
        target_node_ids=[target.id],
        fallback=full,
    )

    hypothesis_ids = projected.node_ids(GraphNodeType.HYPOTHESIS)
    assert len(hypothesis_ids) == 1
    assert "hyp-related" in str(hypothesis_ids[0])


async def test_projection_flags_exclude_related_hypothesis_nodes() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    target = await service.upsert_node(GraphNodeType.HOST, key="target.local")
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp-related", description="related"),
        about=target.id,
    )
    full = await service.observe(step=1)

    projected = await ContextProjector(service).project(
        ContextProfile(
            profile_id="no-hypotheses",
            neighbor_depth=1,
            include_hypotheses=False,
            include_findings=False,
        ),
        step=1,
        target_node_ids=[target.id],
        fallback=full,
    )

    assert GraphNodeType.HYPOTHESIS.value not in projected.node_counts
    assert projected.open_hypotheses == []


async def test_projection_cache_is_revision_scoped_and_returns_copies() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    target = await service.upsert_node(GraphNodeType.HOST, key="target.local")
    fallback = await service.observe(step=1)
    profile = ContextProfile(profile_id="cached", neighbor_depth=0)
    cache = ContextProjectionCache(max_entries=2)
    projector = ContextProjector(service, cache)

    first = await projector.project(
        profile,
        step=1,
        revision=7,
        target_node_ids=[target.id],
        fallback=fallback,
    )
    first.nodes_by_type.clear()
    second = await projector.project(
        profile,
        step=2,
        revision=7,
        target_node_ids=[target.id],
        fallback=fallback,
    )

    assert second.step == 2
    assert second.nodes_by_type
    assert await cache.__len__() == 1

    await projector.project(
        profile,
        step=3,
        revision=8,
        cache_namespace="run-a",
        target_node_ids=[target.id],
        fallback=fallback,
    )
    assert await cache.__len__() == 2

    await projector.project(
        profile,
        step=3,
        revision=8,
        cache_namespace="run-b",
        target_node_ids=[target.id],
        fallback=fallback,
    )
    # max_entries=2: the different run namespace creates a distinct entry and
    # evicts the oldest one rather than serving cross-run context.
    assert await cache.__len__() == 2


def test_evidence_projection_requires_an_explicit_target_association() -> None:
    def record(evidence_id: str, target: str | None) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=EvidenceId(evidence_id),
            run_id=RunId("run"),
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="tool",
            content_type="application/json",
            digest=evidence_id,
            summary=f"summary {evidence_id}",
            created_at=datetime.now(timezone.utc),
            metadata={"target_node_id": target} if target else {},
        )

    projected = project_evidence_excerpts(
        [record("ev-related", "host:a.local"), record("ev-unrelated", None)],
        target_node_ids=[GraphNodeId("host:a.local")],
        limit=10,
    )

    assert [item.evidence_id for item in projected] == ["ev-related"]


def test_evidence_excerpt_carries_status_and_error_for_replanning() -> None:
    # RC#2: the raw outcome (HTTP status, failure code) must reach the excerpt
    # so a later planning turn can adapt -- but only non-secret codes.
    record = EvidenceRecord(
        evidence_id=EvidenceId("ev-outcome"),
        run_id=RunId("run"),
        engagement_id=EngagementId("eng"),
        source_type=EvidenceSourceType.TOOL,
        source_id="web.http_request",
        content_type="application/json",
        digest="digest",
        summary="probe",
        created_at=datetime.now(timezone.utc),
        metadata={
            "target_node_id": "endpoint:mock-bank:8080 GET /api/accounts/alice",
            "http_observation": {"status_code": 401},
            "error": {"code": "tool_output_schema_mismatch", "retryable": False},
        },
    )

    excerpt = project_evidence_excerpts(
        [record],
        target_node_ids=[GraphNodeId("endpoint:mock-bank:8080 GET /api/accounts/alice")],
        limit=1,
    )[0]

    assert excerpt.status_code == 401
    assert excerpt.error_code == "tool_output_schema_mismatch"


def test_evidence_excerpt_summary_is_bounded() -> None:
    record = EvidenceRecord(
        evidence_id=EvidenceId("ev-long"),
        run_id=RunId("run"),
        engagement_id=EngagementId("eng"),
        source_type=EvidenceSourceType.TOOL,
        source_id="tool",
        content_type="application/json",
        digest="digest",
        summary="x" * (MAX_EVIDENCE_SUMMARY_CHARS + 100),
        created_at=datetime.now(timezone.utc),
        metadata={"target_node_id": "host:a.local"},
    )

    excerpt = project_evidence_excerpts(
        [record], target_node_ids=[GraphNodeId("host:a.local")], limit=1
    )[0]

    assert len(excerpt.summary or "") == MAX_EVIDENCE_SUMMARY_CHARS
