"""AttackGraphService: the write path, and the rules it enforces."""

from __future__ import annotations

import pytest

from redteam.attack_graph.identity import finding_node_id, host_node_id, hypothesis_node_id
from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    SensitiveValueError,
    attrs_of,
    facts_of,
)
from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_OPEN,
    AttackGraphService,
    UngroundedFindingError,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import Finding, GraphNode, Hypothesis
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import GraphEdgeType, GraphNodeType


def service() -> AttackGraphService:
    return AttackGraphService(InMemoryAttackGraphStore())


async def node_at(svc: AttackGraphService, node_id) -> GraphNode:
    node = await svc.get_node(node_id)
    assert node is not None
    return node


async def hypothesis_at(svc: AttackGraphService, hypothesis_id: str) -> Hypothesis:
    hypothesis = await svc.get_hypothesis(hypothesis_id)
    assert hypothesis is not None
    return hypothesis


async def test_upserting_the_same_identity_merges_instead_of_duplicating() -> None:
    svc = service()
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"in_scope": True}, step=1)
    await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=3)

    nodes = await svc.list_nodes()
    assert len(nodes) == 1
    assert attrs_of(nodes[0].properties) == {"in_scope": True, "reachable": True}
    assert nodes[0].properties["first_seen_step"] == 1
    assert nodes[0].properties["last_seen_step"] == 3


async def test_a_plain_properties_dict_is_treated_as_facts() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, {"banner": "nginx"}, key="a.local")
    assert facts_of(node.properties) == {"banner": "nginx"}


async def test_upsert_needs_an_identity() -> None:
    with pytest.raises(ValueError, match="node_id or key"):
        await service().upsert_node(GraphNodeType.HOST)


async def test_the_secret_guard_runs_on_the_write_path() -> None:
    with pytest.raises(SensitiveValueError):
        await service().upsert_node(
            GraphNodeType.CREDENTIAL, key="c1", facts={"password": "hunter2"}
        )


async def test_observe_target_derives_a_stable_id() -> None:
    svc = service()
    node = await svc.observe_target(HostTarget(host="A.Local"), attrs={"reachable": True}, step=1)
    assert node.id == host_node_id("a.local")
    assert facts_of(node.properties)["display_name"] == "A.Local"


async def test_linking_the_same_relation_twice_stays_one_edge() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    svc_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/ssh")

    await svc.link(GraphEdgeType.HOSTS, host.id, svc_node.id)
    await svc.link(GraphEdgeType.HOSTS, host.id, svc_node.id)

    assert len(await svc.list_edges()) == 1
    assert (await svc.neighbors(host.id))[0].id == svc_node.id


async def test_retraction_drops_the_predicate_and_records_why() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"reachable": True})

    assert await svc.retract(node.id, "reachable", step=4, reason="host stopped answering")

    refreshed = await node_at(svc, node.id)
    assert "reachable" not in attrs_of(refreshed.properties)
    assert refreshed.properties["retractions"][0]["reason"] == "host stopped answering"


async def test_retracting_on_a_missing_node_is_a_no_op() -> None:
    assert await service().retract(host_node_id("nope"), "x", step=1, reason="r") is False


async def test_attempts_accumulate_on_the_target_node() -> None:
    svc = service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local")

    await svc.record_attempt(node.id, "recon.service_discovery", step=1)
    await svc.record_attempt(node.id, "recon.service_discovery", step=2)
    await svc.record_attempt(node.id, "recon.endpoint_discovery", step=3)

    refreshed = await node_at(svc, node.id)
    assert facts_of(refreshed.properties)["attempted_operations"] == [
        "recon.service_discovery",
        "recon.endpoint_discovery",
    ]


async def test_recording_an_attempt_on_an_unknown_node_returns_none() -> None:
    assert await service().record_attempt(host_node_id("nope"), "x/y") is None


async def test_hypothesis_lifecycle_lives_in_the_graph() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    hypothesis = Hypothesis(hypothesis_id="hyp_1", description="host is reachable", confidence=0.4)

    node = await svc.record_hypothesis(hypothesis, about=host.id, step=1)
    assert node.id == hypothesis_node_id("hyp_1")
    assert await svc.list_hypotheses(status=HYPOTHESIS_OPEN) == [hypothesis]
    # ...and it is connected to what it is about, not floating.
    assert (await svc.neighbors(node.id))[0].id == host.id

    resolved = await svc.resolve_hypothesis("hyp_1", status=HYPOTHESIS_CONFIRMED, confidence=0.9)
    assert resolved is not None and resolved.status == HYPOTHESIS_CONFIRMED
    assert (await hypothesis_at(svc, "hyp_1")).confidence == 0.9
    assert await svc.list_hypotheses(status=HYPOTHESIS_OPEN) == []


async def test_resolving_an_unknown_hypothesis_returns_none() -> None:
    assert await service().resolve_hypothesis("nope", status=HYPOTHESIS_CONFIRMED) is None


async def test_a_verified_finding_without_evidence_is_refused() -> None:
    # A claim nobody can check must not enter the graph — by the time it
    # reaches a report the run is over.
    with pytest.raises(UngroundedFindingError):
        await service().record_finding(
            Finding(finding_id="f1", title="t", verified=True, evidence_ids=[])
        )


async def test_a_grounded_finding_is_stored_and_linked() -> None:
    svc = service()
    host = await svc.upsert_node(GraphNodeType.HOST, key="a.local")
    await svc.record_hypothesis(Hypothesis(hypothesis_id="hyp_1", description="d"), about=host.id)

    finding = Finding(
        finding_id="f1", title="t", severity="high", verified=True, evidence_ids=["ev_1"]
    )
    node = await svc.record_finding(finding, hypothesis_id="hyp_1", about=host.id, step=2)

    assert node.id == finding_node_id("f1")
    assert await svc.get_finding("f1") == finding
    assert await svc.list_findings(verified=True) == [finding]
    neighbours = {n.id for n in await svc.neighbors(node.id)}
    assert neighbours == {hypothesis_node_id("hyp_1"), host.id}


async def test_unverified_findings_need_no_evidence() -> None:
    svc = service()
    finding = Finding(finding_id="f2", title="suspected", verified=False)
    assert (await svc.record_finding(finding)) is not None
    assert await svc.list_findings(verified=False) == [finding]


async def test_snapshot_round_trips_through_a_fresh_store() -> None:
    svc = service()
    host = await svc.upsert_node(
        GraphNodeType.HOST,
        key="a.local",
        attrs={"reachable": True},
        provenance=Provenance(step=1, source="tool:x", confidence=Confidence.MEASURED),
        step=1,
    )
    service_node = await svc.upsert_node(GraphNodeType.SERVICE, key="a.local/ssh")
    await svc.link(GraphEdgeType.HOSTS, host.id, service_node.id)

    records = await svc.export_snapshot(metadata={"run_id": "run_1"})

    restored = service()
    metadata = await restored.import_snapshot(records)

    assert metadata == {"run_id": "run_1"}
    assert await restored.export_snapshot(metadata={"run_id": "run_1"}) == records
    assert (await node_at(restored, host.id)).properties == host.properties


async def test_observation_is_bounded_and_prefers_recent_nodes() -> None:
    svc = service()
    for index in range(10):
        await svc.upsert_node(GraphNodeType.HOST, key=f"h{index}.local", step=index)

    observation = await svc.observe(step=10, max_per_type=3, max_total=3)

    assert observation.truncated is True
    assert observation.node_counts == {"host": 10}
    assert len(observation.nodes_by_type["host"]) == 3
    # Most recently seen wins the limited slots.
    assert observation.nodes_by_type["host"][0].startswith("h9")


async def test_relation_reconfirmation_changes_revision_only_for_meaningful_grounding_or_value():
    from redteam.attack_graph.predicates import (
        claim_assertions,
        claim_key_for_fact,
        claim_provenance_of,
    )
    from redteam.orchestration.analysis import claim_target_is_grounded
    from redteam.orchestration.progress import observation_fingerprint
    from tests.integration import _engine_llm_harness as h

    svc = service()
    a = await svc.observe_target(HostTarget(host="host-01"))
    b = await svc.observe_target(HostTarget(host="related.local"))
    content = {
        "relation": {"type": "observed", "source_node_id": str(a.id), "target_node_id": str(b.id)},
        "observation": "discovered",
    }

    def prov(eid, confidence):
        return Provenance(
            step=1, source="test", evidence_id=eid, run_id="run-reconfirm", confidence=confidence
        )

    async def revision():
        observation = await svc.observe()
        return observation.relation_digests, observation_fingerprint(
            observation, include_edge_types=[GraphEdgeType.OBSERVED]
        )

    await svc.link(
        GraphEdgeType.OBSERVED,
        a.id,
        b.id,
        properties=content,
        provenance=prov("inferred", Confidence.INFERRED),
    )
    before = await revision()
    first = await svc.link(
        GraphEdgeType.OBSERVED,
        a.id,
        b.id,
        properties=content,
        provenance=prov("measured-1", Confidence.MEASURED),
    )
    measured = await revision()
    assert before[0] != measured[0] and before[1] != measured[1]
    await svc.link(
        GraphEdgeType.OBSERVED,
        a.id,
        b.id,
        properties=content,
        provenance=prov("measured-2", Confidence.MEASURED),
    )
    assert await revision() == measured
    entry = claim_provenance_of(first.properties)[claim_key_for_fact("relation")]
    assert entry["evidence_id"] == "inferred"
    current = (await svc.list_edges())[0]
    assertions = claim_assertions(
        claim_provenance_of(current.properties)[claim_key_for_fact("relation")]
    )
    assert {item["evidence_id"] for item in assertions} == {"inferred", "measured-1", "measured-2"}
    for eid in ("measured-1", "measured-2"):
        record = h.make_tool_evidence(
            evidence_id=eid,
            run_id="run-reconfirm",
            engagement_id="eng-t1",
            action_id="act-" + eid,
            invocation_id="inv-" + eid,
            path_id="path-1",
            path_version=1,
            step_id="step-1",
            target_node_id=str(a.id),
        )
        proposal, _ = h.make_proposal_result(
            run_id="run-reconfirm",
            engagement_id="eng-t1",
            action_id="act-" + eid,
            invocation_id="inv-" + eid,
            host="host-01",
        )
        record.metadata["proposal"] = proposal.model_dump(mode="json")
        assert await claim_target_is_grounded(svc, record, str(b.id))
    await svc.link(
        GraphEdgeType.OBSERVED,
        a.id,
        b.id,
        properties={**content, "observation": "contacted"},
        provenance=prov("changed", Confidence.MEASURED),
    )
    changed = await revision()
    assert changed[0] != measured[0] and changed[1] != measured[1]
