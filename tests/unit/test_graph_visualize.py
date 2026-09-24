"""The HTML/SVG renderer: self-contained, deterministic, and safe to embed."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import build_properties
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.visualize import render_html, render_svg
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

NOW = datetime.now(timezone.utc)


def node(node_id: str, node_type: GraphNodeType, **kwargs) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=build_properties(**kwargs),
        created_at=NOW,
        updated_at=NOW,
    )


def test_the_page_is_self_contained() -> None:
    # A report artifact has to open offline — no CDN, no script tag.
    page = render_html([node("host:a.local", GraphNodeType.HOST)], [])
    assert page.startswith("<!doctype html>")
    assert "<svg" in page
    assert "http://" not in page.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "<script" not in page


def test_node_and_edge_content_is_escaped() -> None:
    # Node keys come from observed hostnames/paths, so they are untrusted.
    hostile = node('host:<script>alert("x")</script>', GraphNodeType.HOST)
    svg, _ = render_svg([hostile], [])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_rendering_is_deterministic() -> None:
    nodes = [
        node("host:b.local", GraphNodeType.HOST),
        node("host:a.local", GraphNodeType.HOST),
    ]
    assert render_svg(nodes, [])[0] == render_svg(list(reversed(nodes)), [])[0]


def test_large_graphs_are_truncated_and_say_so() -> None:
    nodes = [node(f"host:h{i}.local", GraphNodeType.HOST, step=i) for i in range(50)]
    svg, truncated = render_svg(nodes, [], max_nodes=10)
    assert truncated
    assert svg.count("<rect") == 10
    # Most recently observed survive the cut.
    assert "h49.local" in svg and "h00.local" not in svg
    assert "showing the 10 most recently observed of 50 nodes" in render_html(
        nodes, [], max_nodes=10
    )


def test_edges_to_hidden_nodes_are_dropped() -> None:
    nodes = [node("host:a", GraphNodeType.HOST), node("host:b", GraphNodeType.HOST)]
    edges = [
        GraphEdge(
            id=GraphEdgeId("e1"),
            type=GraphEdgeType.RELATED_TO,
            source_id=GraphNodeId("host:a"),
            target_id=GraphNodeId("host:missing"),
            created_at=NOW,
        )
    ]
    svg, _ = render_svg(nodes, edges)
    assert "<path" not in svg.split("</defs>")[1]


def test_an_empty_graph_still_renders() -> None:
    page = render_html([], [])
    assert "empty" in page
    assert "<svg" in page


async def test_export_html_writes_a_file(tmp_path) -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True}, step=1
    )
    await service.record_hypothesis(
        Hypothesis(hypothesis_id="hyp_1", description="d"), about=host.id
    )
    await service.record_finding(
        Finding(
            finding_id="find_1",
            title="t",
            severity="critical",
            verified=True,
            evidence_ids=["ev_1"],
        ),
        hypothesis_id="hyp_1",
        about=host.id,
    )

    path = await service.export_html(tmp_path / "graph.html", title="Run 1")
    page = path.read_text(encoding="utf-8")

    assert path.exists()
    assert "Run 1" in page
    assert "a.local" in page and "find_1" in page
    # Findings are outlined by severity so they stand out in a report.
    assert "#ff4d4d" in page


async def test_export_html_can_focus_one_neighbourhood(tmp_path) -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(GraphNodeType.HOST, key="a.local")
    svc = await service.upsert_node(GraphNodeType.SERVICE, key="a.local/http")
    await service.upsert_node(GraphNodeType.HOST, key="unrelated.local")
    await service.link(GraphEdgeType.HOSTS, host.id, svc.id)

    path = await service.export_html(tmp_path / "focus.html", around=host.id, radius=1)
    page = path.read_text()

    assert "a.local/http" in page
    assert "unrelated.local" not in page
