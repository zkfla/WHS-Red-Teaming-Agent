// Curves are drawn in scene coordinates; zoom never changes relationship geometry.
import { NODE_HEIGHT, NODE_WIDTH } from './graph_layout.js';

export function drawEdges(svg, edges, layout, related) {
  svg.replaceChildren();
  svg.setAttribute('width', String(layout.width));
  svg.setAttribute('height', String(layout.height));
  const ns = svg.namespaceURI;
  const defs = document.createElementNS(ns, 'defs');
  for (const kind of ['registered', 'held_by', 'returned', 'stored', 'wakes']) {
    const marker = document.createElementNS(ns, 'marker');
    for (const [key, value] of Object.entries({id:`graph-arrow-${kind}`, viewBox:'0 0 10 10', refX:'9', refY:'5', markerWidth:'6', markerHeight:'6', orient:'auto-start-reverse'})) marker.setAttribute(key, value);
    const arrow = document.createElementNS(ns, 'path');
    arrow.setAttribute('d', 'M 1 1 L 9 5 L 1 9 Z');
    arrow.setAttribute('class', `graph-arrow ${kind}`);
    marker.append(arrow);
    defs.append(marker);
  }
  svg.append(defs);
  for (const edge of edges) {
    const start = layout.positions.get(edge.from);
    const end = layout.positions.get(edge.to);
    if (!start || !end) continue;
    const active = related && related.has(edge.from) && related.has(edge.to);
    const line = document.createElementNS(ns, 'path');
    let d;
    if (edge.kind === 'wakes') {
      const fromX = start.x + NODE_WIDTH / 2;
      const toX = end.x + NODE_WIDTH / 2;
      d = `M${fromX},${start.y} V104 Q${fromX},84 ${fromX - 20},84 H${toX + 20} Q${toX},84 ${toX},104 V${end.y}`;
    } else {
      const fromX = start.x + NODE_WIDTH;
      const fromY = start.y + NODE_HEIGHT / 2;
      const toX = end.x;
      const toY = end.y + NODE_HEIGHT / 2;
      const mid = (fromX + toX) / 2;
      d = `M${fromX},${fromY} C${mid},${fromY} ${mid},${toY} ${toX},${toY}`;
    }
    line.setAttribute('d', d);
    line.setAttribute('class', `graph-edge ${edge.kind}${active ? ' highlighted' : related ? ' dim' : ''}`);
    line.setAttribute('marker-end', `url(#graph-arrow-${edge.kind})`);
    line.setAttribute('data-from', edge.from);
    line.setAttribute('data-to', edge.to);
    svg.append(line);
  }
}
