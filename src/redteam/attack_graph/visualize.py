"""Rendering the graph as a standalone HTML page.

Hand-rolled SVG: no plotting library, no CDN, no script tag, so the output
opens offline. Layout is deterministic, so two runs can be compared.
"""

from __future__ import annotations

import html
from pathlib import Path

from redteam.attack_graph.predicates import (
    attrs_of,
    cleanup_of,
    facts_of,
    last_seen_step,
    predicate_flags,
    retractions_of,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId

#: Left-to-right order: roughly how an engagement discovers things.
COLUMN_ORDER: tuple[GraphNodeType, ...] = (
    GraphNodeType.HOST,
    GraphNodeType.SERVICE,
    GraphNodeType.ENDPOINT,
    GraphNodeType.PRINCIPAL,
    GraphNodeType.CREDENTIAL,
    GraphNodeType.ASSET,
    GraphNodeType.RESOURCE,
    GraphNodeType.HYPOTHESIS,
    GraphNodeType.FINDING,
)

_FILL = {
    GraphNodeType.HOST: "#2f6f9f",
    GraphNodeType.SERVICE: "#3f8f7f",
    GraphNodeType.ENDPOINT: "#4f7f4f",
    GraphNodeType.PRINCIPAL: "#8f6f2f",
    GraphNodeType.CREDENTIAL: "#8f4f6f",
    GraphNodeType.ASSET: "#5f5f8f",
    GraphNodeType.RESOURCE: "#6f6f6f",
    GraphNodeType.HYPOTHESIS: "#7f5f9f",
    GraphNodeType.FINDING: "#a03f3f",
}

_SEVERITY_STROKE = {
    "critical": "#ff4d4d",
    "high": "#ff8c42",
    "medium": "#ffd166",
    "low": "#9fd356",
}

COLUMN_WIDTH = 230
ROW_HEIGHT = 58
NODE_WIDTH = 190
NODE_HEIGHT = 34
MARGIN_X = 40
MARGIN_Y = 80
DEFAULT_MAX_NODES = 300


def _label(node: GraphNode) -> str:
    _, _, key = str(node.id).partition(":")
    return key or str(node.id)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tooltip(node: GraphNode) -> str:
    lines = [str(node.id), f"last seen: step {last_seen_step(node.properties)}"]
    attrs = attrs_of(node.properties)
    if attrs:
        lines.append("attrs: " + ", ".join(f"{k}={v}" for k, v in sorted(attrs.items())))
    facts = {k: v for k, v in facts_of(node.properties).items() if k != "attempted_operations"}
    if facts:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(facts.items()))
        lines.append("facts: " + _truncate(rendered, 200))
    attempted = facts_of(node.properties).get("attempted_operations") or []
    if attempted:
        lines.append("attempted: " + ", ".join(str(item) for item in attempted))
    for entry in retractions_of(node.properties):
        lines.append(
            f"retracted {entry.get('key')} @ step {entry.get('step')}: "
            f"{entry.get('reason')}"
        )
    item = cleanup_of(node.properties)
    if item:
        lines.append(f"cleanup: {item.get('resource_type')} — {item.get('status')}")
    return "\n".join(lines)


def _stroke(node: GraphNode) -> tuple[str, float]:
    if node.type is GraphNodeType.FINDING:
        severity = str(facts_of(node.properties).get("severity", "")).lower()
        return _SEVERITY_STROKE.get(severity, "#d0d0d0"), 2.5
    item = cleanup_of(node.properties)
    if item is not None and str(item.get("status")) in ("pending", "failed"):
        return "#ffd166", 2.5
    return "#5a5a5a", 1.0


def _positions(
    nodes: list[GraphNode],
) -> tuple[dict[GraphNodeId, tuple[float, float]], int, int]:
    by_type: dict[GraphNodeType, list[GraphNode]] = {}
    for node in nodes:
        by_type.setdefault(node.type, []).append(node)

    columns = [t for t in COLUMN_ORDER if t in by_type]
    columns += [t for t in by_type if t not in COLUMN_ORDER]

    positions: dict[GraphNodeId, tuple[float, float]] = {}
    tallest = 0
    for column, node_type in enumerate(columns):
        members = sorted(by_type[node_type], key=lambda item: str(item.id))
        tallest = max(tallest, len(members))
        for row, node in enumerate(members):
            positions[node.id] = (
                MARGIN_X + column * COLUMN_WIDTH,
                MARGIN_Y + row * ROW_HEIGHT,
            )
    width = MARGIN_X * 2 + max(len(columns), 1) * COLUMN_WIDTH
    height = MARGIN_Y + max(tallest, 1) * ROW_HEIGHT + MARGIN_Y
    return positions, width, height


def _edge_path(start: tuple[float, float], end: tuple[float, float]) -> str:
    x1, y1 = start[0] + NODE_WIDTH, start[1] + NODE_HEIGHT / 2
    x2, y2 = end[0], end[1] + NODE_HEIGHT / 2
    if end[0] < start[0]:  # right-to-left: leave from the left edge instead
        x1, x2 = start[0], end[0] + NODE_WIDTH
    control = abs(x2 - x1) / 2 or 30
    return (
        f"M {x1:.1f} {y1:.1f} "
        f"C {x1 + control:.1f} {y1:.1f}, {x2 - control:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}"
    )


def render_svg(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> tuple[str, bool]:
    """The ``<svg>`` element, plus whether the graph had to be truncated."""
    truncated = len(nodes) > max_nodes
    shown = sorted(nodes, key=lambda node: (-last_seen_step(node.properties), str(node.id)))[
        :max_nodes
    ]
    visible = {node.id for node in shown}
    positions, width, height = _positions(shown)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMinYMin meet" xmlns="http://www.w3.org/2000/svg">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8a8a8a"/></marker></defs>',
    ]

    for node_type in COLUMN_ORDER:
        column_nodes = [n for n in shown if n.type is node_type]
        if not column_nodes:
            continue
        x, _ = positions[sorted(column_nodes, key=lambda n: str(n.id))[0].id]
        parts.append(
            f'<text x="{x}" y="{MARGIN_Y - 24}" fill="#9a9a9a" font-size="12" '
            f'font-family="ui-monospace,monospace">{html.escape(node_type.value)} '
            f'({len(column_nodes)})</text>'
        )

    for edge in edges:
        if edge.source_id not in visible or edge.target_id not in visible:
            continue
        path = _edge_path(positions[edge.source_id], positions[edge.target_id])
        parts.append(
            f'<path d="{path}" fill="none" stroke="#8a8a8a" stroke-width="1.2" '
            f'opacity="0.55" marker-end="url(#arrow)">'
            f'<title>{html.escape(edge.type.value)}</title></path>'
        )

    for node in shown:
        x, y = positions[node.id]
        stroke, stroke_width = _stroke(node)
        label = _truncate(_label(node), 22)
        flags = _truncate(predicate_flags(node.properties), 22)
        parts.append(
            f'<g><title>{html.escape(_tooltip(node))}</title>'
            f'<rect x="{x}" y="{y}" width="{NODE_WIDTH}" height="{NODE_HEIGHT}" rx="6" '
            f'fill="{_FILL.get(node.type, "#555")}" stroke="{stroke}" '
            f'stroke-width="{stroke_width}"/>'
            f'<text x="{x + 10}" y="{y + 15}" fill="#f5f5f5" font-size="12" '
            f'font-family="ui-monospace,monospace">{html.escape(label)}</text>'
            f'<text x="{x + 10}" y="{y + 28}" fill="#cfd6dd" font-size="10" '
            f'font-family="ui-monospace,monospace">{html.escape(flags)}</text></g>'
        )

    parts.append("</svg>")
    return "\n".join(parts), truncated


def render_html(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    title: str = "Attack Graph",
    max_nodes: int = DEFAULT_MAX_NODES,
) -> str:
    """A self-contained page — no CDN, no script, opens offline."""
    svg, truncated = render_svg(nodes, edges, max_nodes=max_nodes)
    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.type.value] = counts.get(node.type.value, 0) + 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "empty"
    warning = (
        f'<p class="warn">showing the {max_nodes} most recently observed of '
        f"{len(nodes)} nodes</p>"
        if truncated
        else ""
    )
    legend = "".join(
        f'<span class="chip" style="background:{_FILL[t]}">{t.value}</span>'
        for t in COLUMN_ORDER
        if t.value in counts
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; padding:24px; background:#14171a; color:#e8eaed;
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }}
  h1 {{ font-size:18px; margin:0 0 4px; }}
  p {{ margin:0 0 12px; color:#9aa4ad; }}
  .warn {{ color:#ffd166; }}
  .chip {{ display:inline-block; padding:2px 8px; margin:2px 4px 2px 0;
           border-radius:10px; font-size:11px; color:#fff; }}
  .canvas {{ overflow-x:auto; border:1px solid #2a2f35; border-radius:8px;
             background:#1b1f24; padding:8px; }}
</style></head>
<body>
<h1>{html.escape(title)}</h1>
<p>{html.escape(summary)} &middot; {len(edges)} edges</p>
<div>{legend}</div>
{warning}
<div class="canvas">{svg}</div>
<p>Hover a node or edge for details. Finding outlines are coloured by severity;
a yellow outline marks a resource still awaiting cleanup.</p>
</body></html>
"""


def write_html(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    path: str | Path,
    *,
    title: str = "Attack Graph",
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_html(nodes, edges, title=title, max_nodes=max_nodes), encoding="utf-8"
    )
    return destination
