// Uses the same read-only graph projection as the full graph; no inferred worker state.
import { $, h, replace } from "./dom.js";
import { GLYPH, GRAPH_UNAVAILABLE, PLANNER, TASK, UNKNOWN, WORKER } from "./labels.js";
import { badge, dot, emptyState, replaceLive } from "./ui.js";

export function renderRuntime(view, openGraph) {
  const tasks = view?.available ? view.tasks : [];
  $("task-section").hidden = tasks.length === 0;
  replaceLive($("task-list"), tasks.slice(0, 12).map((task) => h("li", {},
    h("button", { type: "button", class: `run-link task-${task.status}`, title: task.title, "data-focus": task.task_id, onClick: () => openGraph(`task:${task.task_id}`) },
      h("span", { class: "task-glyph", "aria-hidden": "true", text: GLYPH[task.status] || "?" }),
      h("span", { class: "run-copy" }, h("span", { class: "run-title", text: task.title }),
        h("small", { text: task.wait?.reason === "approval" ? "승인 대기" : TASK[task.status] || UNKNOWN }))))));
  if (!view?.available) {
    const message = view?.error || GRAPH_UNAVAILABLE[view?.reason] || "실행 상태를 확인하고 있습니다.";
    replace($("agent-status"), emptyState("graph", "에이전트 실행 현황", message));
    return;
  }
  const row = (id, title, description, status, tone) => h("button", {
    type: "button", class: "agent-row", "data-focus": id, onClick: () => openGraph(id),
  }, dot(tone), h("span", { class: "agent-copy" }, h("strong", { text: title }), h("small", { text: description })), badge(status, tone));
  const planner = view.planner;
  const nodes = [row("planner", "Planner", `조사 작업 ${planner.inputs.tasks}개 · 증거 ${planner.inputs.evidence}건`, PLANNER[planner.state] || UNKNOWN, planner.state === "deciding" ? "run" : "")];
  for (const worker of view.workers.slice(0, 5)) {
    const task = tasks.find((item) => item.task_id === worker.task_id);
    nodes.push(row(`worker:${worker.instance_id}`, task?.role || worker.instance_id,
      task?.title || worker.instance_id, WORKER[worker.phase] || UNKNOWN,
      worker.phase === "waiting_approval" || worker.phase === "outcome_unknown" ? "wait" : ""));
  }
  if (view.workers.length > 5) nodes.push(h("button", { type: "button", class: "text-btn more-workers", text: `전체 워커 ${view.workers.length}개 보기`, onClick: () => openGraph() }));
  replaceLive($("agent-status"), nodes);
}
