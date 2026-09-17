from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import GraphNodeType
from redteam.orchestration.progress import observation_fingerprint


def test_fingerprint_ignores_turn_and_attempt_bookkeeping() -> None:
    first = GraphObservation(
        step=1,
        node_counts={"host": 1},
        nodes_by_type={"host": ["a.local"]},
        attempted=["recon/ping@host:a.local"],
    )
    second = first.model_copy(
        update={"step": 99, "attempted": ["different/bookkeeping@host:a.local"]}
    )

    assert observation_fingerprint(first) == observation_fingerprint(second)


def test_fingerprint_changes_when_meaningful_predicates_change() -> None:
    first = GraphObservation(
        node_counts={"host": 1},
        nodes_by_type={"host": ["a.local"]},
    )
    second = first.model_copy(
        update={"nodes_by_type": {"host": ["a.local [reachable]"]}}
    )

    assert observation_fingerprint(first) != observation_fingerprint(second)


def test_scoped_fingerprint_ignores_unrelated_node_types() -> None:
    before = GraphObservation(
        node_counts={"host": 1, "asset": 1},
        nodes_by_type={"host": ["a.local"], "asset": ["app.apk"]},
    )
    after = before.model_copy(
        update={
            "node_counts": {"host": 2, "asset": 1},
            "nodes_by_type": {
                "host": ["a.local", "b.local"],
                "asset": ["app.apk"],
            },
        }
    )

    assert observation_fingerprint(
        before, include_node_types=[GraphNodeType.ASSET]
    ) == observation_fingerprint(after, include_node_types=[GraphNodeType.ASSET])
    assert observation_fingerprint(
        before, include_node_types=[GraphNodeType.HOST]
    ) != observation_fingerprint(after, include_node_types=[GraphNodeType.HOST])


def test_scoped_fingerprint_honours_finding_exclusion() -> None:
    before = GraphObservation(node_counts={"host": 1}, nodes_by_type={"host": ["a"]})
    after = before.model_copy(
        update={
            "node_counts": {"host": 1, "finding": 1},
            "nodes_by_type": {"host": ["a"], "finding": ["finding:new"]},
            "findings": ["finding:new [high/verified]"],
        }
    )

    assert observation_fingerprint(before, include_findings=False) == (
        observation_fingerprint(after, include_findings=False)
    )
