"""Third hardening pass: concurrency, ambiguous patterns, idempotent writes."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from redteam.attack_graph.patterns import (
    NodePattern,
    PatternDefinitionError,
    resolve,
    validate_patterns,
)
from redteam.attack_graph.predicates import (
    MAX_PROVENANCE_ENTRIES,
    Provenance,
    attrs_of,
    facts_of,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import Finding, GraphNode, Hypothesis
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId


class YieldingStore(InMemoryAttackGraphStore):
    """Awaits between read and write, the way a networked store would — the
    plain in-memory store never yields, so it hides every race."""

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        await asyncio.sleep(0)
        return await super().get_node(node_id)

    async def add_node(self, node: GraphNode) -> None:
        await asyncio.sleep(0)
        await super().add_node(node)


def service(store: InMemoryAttackGraphStore | None = None) -> AttackGraphService:
    return AttackGraphService(store or InMemoryAttackGraphStore())


# -- concurrency ----------------------------------------------------------


async def test_concurrent_upserts_do_not_lose_each_other() -> None:
    svc = service(YieldingStore())
    await svc.upsert_node(GraphNodeType.HOST, key="a.local")

    await asyncio.gather(
        *[
            svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={f"k{index}": True})
            for index in range(30)
        ]
    )

    node = await svc.get_node(GraphNodeId("host:a.local"))
    assert node is not None
    assert len(attrs_of(node.properties)) == 30


async def test_concurrent_attempt_records_all_survive() -> None:
    svc = service(YieldingStore())
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")

    await asyncio.gather(
        *[svc.record_attempt(node.id, f"cap{index}/op") for index in range(30)]
    )

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert len(facts_of(refreshed.properties)["attempted_operations"]) == 30


async def test_concurrent_writers_to_different_nodes_both_land() -> None:
    svc = service(YieldingStore())

    await asyncio.gather(
        *[svc.upsert_node(GraphNodeType.HOST, key=f"h{index}.local") for index in range(20)]
    )

    assert len(await svc.list_nodes()) == 20


async def test_a_concurrent_retraction_is_not_lost() -> None:
    svc = service(YieldingStore())
    node = await svc.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"a": True, "b": True}, step=1
    )

    await asyncio.gather(
        svc.retract(node.id, "a", step=2, reason="gone"),
        svc.retract(node.id, "b", step=2, reason="gone"),
    )

    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert attrs_of(refreshed.properties) == {}
    assert len(refreshed.properties["retractions"]) == 2


# -- a write that changes nothing is not a write --------------------------


async def test_an_unchanged_upsert_does_not_bump_updated_at() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"x": True}, step=1)

    again = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"x": True}, step=1)

    assert again.updated_at == node.updated_at


async def test_merging_a_snapshot_into_itself_is_a_no_op() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"x": True}, step=7)
    for index in range(250):  # push past the fact-list cap
        await svc.record_attempt(node.id, f"cap{index}/op")
    before = await svc.export_snapshot()

    await svc.import_snapshot(before, merge=True)

    assert await svc.export_snapshot() == before


async def test_importing_a_snapshot_keeps_first_seen_step() -> None:
    # `step` defaulted to 0 on the merge path, rewriting the recorded
    # first sighting back to the start of the run.
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"x": True}, step=7)
    records = await svc.export_snapshot()

    restored = service()
    await restored.import_snapshot(records, merge=True)

    node = await restored.get_node(GraphNodeId("host:a.local"))
    assert node is not None
    assert node.properties["first_seen_step"] == 7


# -- patterns that cannot mean what they say ------------------------------


def test_two_patterns_with_the_same_name_are_rejected() -> None:
    # They used to overwrite one another in the candidate table, so the
    # result claimed success having quietly dropped one precondition.
    with pytest.raises(PatternDefinitionError, match="resolve to the name"):
        validate_patterns(
            [
                NodePattern(type=GraphNodeType.HOST, bind="h"),
                NodePattern(type=GraphNodeType.HOST, bind="h"),
            ]
        )


def test_patterns_without_binds_collide_on_their_type_name() -> None:
    with pytest.raises(PatternDefinitionError, match="resolve to the name"):
        resolve(
            [NodePattern(type=GraphNodeType.HOST), NodePattern(type=GraphNodeType.HOST)],
            nodes=[],
        )


def test_a_pattern_cannot_be_distinct_from_itself() -> None:
    with pytest.raises(ValidationError, match="distinct from itself"):
        NodePattern(type=GraphNodeType.HOST, bind="h", distinct_from=["h"])


def test_references_to_undeclared_binds_are_rejected() -> None:
    with pytest.raises(PatternDefinitionError, match="not a declared bind"):
        validate_patterns(
            [NodePattern(type=GraphNodeType.HOST, bind="h", distinct_from=["ghost"])]
        )
    with pytest.raises(PatternDefinitionError, match="not a declared bind"):
        validate_patterns(
            [
                NodePattern(
                    type=GraphNodeType.HOST,
                    bind="h",
                    related_to="ghost",
                    edge_type=GraphEdgeType.HOSTS,
                )
            ]
        )


def test_a_valid_pattern_list_passes_validation() -> None:
    validate_patterns(
        [
            NodePattern(type=GraphNodeType.PRINCIPAL, bind="p"),
            NodePattern(
                type=GraphNodeType.HOST,
                bind="h",
                related_to="p",
                edge_type=GraphEdgeType.CAN_ACCESS,
                distinct_from=[],
            ),
        ]
    )


# -- one bad record must not take a listing down with it ------------------


async def test_an_unreadable_finding_is_skipped_but_reported() -> None:
    # A listing that raises during reporting loses every valid finding
    # alongside the broken one — so skip it, and make integrity block.
    svc = service()
    await svc.record_finding(
        Finding(finding_id="good1", title="t", verified=True, evidence_ids=["ev"])
    )
    await svc.upsert_node(GraphNodeType.FINDING, key="broken")  # no facts at all
    await svc.record_finding(Finding(finding_id="good2", title="t2"))

    findings = await svc.list_findings()

    assert [f.finding_id for f in findings] == ["good1", "good2"]
    report = await svc.integrity_report()
    assert report.malformed_records
    assert any("unreadable record" in line for line in report.blocking)


async def test_an_unreadable_hypothesis_is_skipped_but_reported() -> None:
    svc = service()
    await svc.record_hypothesis(Hypothesis(hypothesis_id="good", description="d"))
    await svc.upsert_node(GraphNodeType.HYPOTHESIS, key="broken")

    assert [h.hypothesis_id for h in await svc.list_hypotheses()] == ["good"]
    assert await svc.get_hypothesis("broken") is None
    assert (await svc.integrity_report()).malformed_records


async def test_reports_still_render_with_an_unreadable_record() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.FINDING, key="broken")

    assert await svc.observe() is not None
    assert await svc.export_snapshot()

    # The chain builder reads leniently, so the broken node surfaces as an
    # empty claim rather than being omitted.
    chains = await svc.evidence_chains()
    assert len(chains) == 1
    assert chains[0].evidence_ids == []
    assert (await svc.integrity_report()).malformed_records


# -- edges are bounded the same way nodes are -----------------------------


async def test_edge_provenance_is_capped() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    service_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/http")

    for step in range(200):
        await svc.link(
            GraphEdgeType.HOSTS,
            host.id,
            service_node.id,
            provenance=Provenance(step=step, source=f"tool:{step}"),
        )

    edge = (await svc.list_edges())[0]
    entries = edge.properties["provenance"]
    assert len(entries) == MAX_PROVENANCE_ENTRIES
    assert entries[0]["source"] == "tool:0"
    assert entries[-1]["source"] == "tool:199"
