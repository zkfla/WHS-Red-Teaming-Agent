// Graph view: stored state is the source; events only hint that a re-read is due.
import { $, replace } from "./dom.js";
import { api } from "./api.js";
import { GRAPH_UNAVAILABLE, describeError } from "./labels.js";
import { renderDetail } from "./graph_detail.js";
import { drawEdges } from "./graph_lines.js";
import { originNode, plannerNode, taskNodes, workerNodes } from "./graph_nodes.js";

const POLL_MS = 5000;
const DEBOUNCE_MS = 700;

export function setupGraph() {
  let open = false;
  let view = null;
  let selected = null;
  let timer = null;
  let pending = null;
  let reading = false;

  function pick(id) {
    selected = selected === id ? null : id;
    paint();
  }

  function paint() {
    const note = $("graph-note");
    if (!view || !view.available) {
      const reason = view ? view.reason : null;
      note.textContent = GRAPH_UNAVAILABLE[reason] || (reason ? `실행 상태를 읽지 못했습니다(${reason}).` : "불러오는 중입니다.");
      for (const id of ["graph-planner", "graph-tasks", "graph-workers", "graph-origin"]) replace($(id));
      $("graph-lines").replaceChildren();
      renderDetail($("graph-detail"), view, selected);
      return;
    }
    note.textContent = view.mission && view.mission.termination_reason ? `조사 종료 · ${view.mission.termination_reason}` : "";
    replace($("graph-planner"), plannerNode(view, selected, pick));
    replace($("graph-tasks"), taskNodes(view, selected, pick));
    replace($("graph-workers"), workerNodes(view, selected, pick));
    replace($("graph-origin"), originNode(view, selected, pick));
    drawEdges($("graph-canvas"), $("graph-lines"), view.edges);
    renderDetail($("graph-detail"), view, selected);
  }

  async function read() {
    if (!open || reading) return;
    reading = true;
    try {
      view = await api.graph();
    } catch (failure) {
      view = { available: false, reason: null };
      $("graph-note").textContent = describeError(failure);
      return;
    } finally {
      reading = false;
    }
    paint();
  }

  function hint() {
    if (!open || pending) return;
    pending = setTimeout(() => {
      pending = null;
      read();
    }, DEBOUNCE_MS);
  }

  function show(on) {
    open = on;
    $("view-graph").hidden = !on;
    $("view-chat").hidden = on;
    $("headline").textContent = on ? "실행 그래프" : "대화";
    clearInterval(timer);
    if (on) {
      read();
      timer = setInterval(read, POLL_MS);
    }
  }

  window.addEventListener("resize", () => open && view && view.available && paint());
  return { show, hint };
}
