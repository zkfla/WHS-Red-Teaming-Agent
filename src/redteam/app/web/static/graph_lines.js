// Draws the flow between node elements. Positions come from the laid-out DOM, so the server
// sends relations only. SVG is built with createElementNS; styling stays in graph.css.
function anchor(canvas, id, side) {
  const el = canvas.querySelector(`[data-node="${CSS.escape(id)}"]`);
  if (!el) return null;
  const box = el.getBoundingClientRect();
  const origin = canvas.getBoundingClientRect();
  const x = (side === "right" ? box.right : side === "left" ? box.left : box.left + box.width / 2) - origin.left + canvas.scrollLeft;
  const y = (side === "top" ? box.top : box.top + box.height / 2) - origin.top + canvas.scrollTop;
  return { x, y };
}

function path(svg, d, kind) {
  // The namespace is taken from the page's own <svg>, so no URL literal is needed here.
  const el = document.createElementNS(svg.namespaceURI, "path");
  el.setAttribute("d", d);
  el.setAttribute("class", `graph-edge ${kind}`);
  return el;
}

export function drawEdges(canvas, svg, edges) {
  svg.replaceChildren();
  svg.setAttribute("width", String(canvas.scrollWidth));
  svg.setAttribute("height", String(canvas.scrollHeight));
  for (const edge of edges) {
    if (edge.kind === "wakes") {
      const from = anchor(canvas, edge.from, "top");
      const to = anchor(canvas, edge.to, "top");
      if (!from || !to) continue;
      const lift = Math.max(12, Math.min(from.y, to.y) - 14);
      svg.append(path(svg, `M${from.x},${from.y} C${from.x},${lift} ${to.x},${lift} ${to.x},${to.y}`, "wakes"));
      continue;
    }
    const from = anchor(canvas, edge.from, "right");
    const to = anchor(canvas, edge.to, "left");
    if (!from || !to) continue;
    const mid = (from.x + to.x) / 2;
    svg.append(path(svg, `M${from.x},${from.y} C${mid},${from.y} ${mid},${to.y} ${to.x},${to.y}`, edge.kind));
  }
}
