// Node cards for the four lanes. Every status is a glyph plus a word, never color alone.
import { h } from "./dom.js";
import { GLYPH, PLANNER, TASK, UNKNOWN, WAIT, WORKER, WORKER_GLYPH } from "./labels.js";

function node(id, selected, onPick, className, ...children) {
  return h("button", {
    type: "button", "data-node": id, class: `gnode ${className}${selected === id ? " on" : ""}`,
    "aria-pressed": String(selected === id), onclick: () => onPick(id),
  }, ...children);
}

const chip = (glyph, word) => h("span", { class: "gchip" }, h("span", { "aria-hidden": "true", text: glyph }), ` ${word}`);

export function plannerNode(view, selected, onPick) {
  const p = view.planner;
  const inputs = p.inputs;
  return node("planner", selected, onPick, "planner",
    h("strong", { text: "Planner" }),
    chip(GLYPH[p.state], PLANNER[p.state] || UNKNOWN),
    h("span", { class: "gmeta", text: `증거 ${inputs.evidence} · Task ${inputs.tasks} · 열린 질문 ${inputs.open_questions ?? UNKNOWN}` }),
    p.last_judgment ? h("span", { class: "gmeta", text: `직전 판단: ${p.last_judgment.termination || UNKNOWN}` }) : null);
}

export function taskNodes(view, selected, onPick, limit = 30) {
  const nodes = view.tasks.slice(0, limit).map((t) =>
    node(`task:${t.task_id}`, selected, onPick, `task s-${t.status}`,
      h("strong", { text: t.title }),
      chip(GLYPH[t.status] || GLYPH.unknown, TASK[t.status] || UNKNOWN),
      h("span", { class: "gmeta", text: `역할 ${t.role || UNKNOWN} · 워커 ${t.worker || "없음"}` }),
      t.wait ? h("span", { class: "gmeta", text: `대기: ${WAIT[t.wait.reason] || t.wait.reason}` }) : null));
  if (view.tasks.length > limit) nodes.push(h("p", { class: "note", text: `외 ${view.tasks.length - limit}개 Task` }));
  return nodes.length ? nodes : [h("p", { class: "note", text: "등록된 Task가 없습니다." })];
}

export function workerNodes(view, selected, onPick, limit = 20) {
  const nodes = view.workers.slice(0, limit).map((w) =>
    node(`worker:${w.instance_id}`, selected, onPick, `worker p-${w.phase}`,
      h("strong", { text: w.instance_id }),
      chip(WORKER_GLYPH[w.phase] || "?", WORKER[w.phase] || UNKNOWN),
      h("span", { class: "gmeta", text: `모델 요청 ${w.model_requests} · 도구 호출 ${w.tool_calls}` }),
      w.slot_released ? h("span", { class: "gmeta", text: "슬롯 반납됨 · 요청은 보존" }) : null));
  return nodes.length ? nodes : [h("p", { class: "note", text: "실행 중인 워커가 없습니다." })];
}

export function originNode(view, selected, onPick) {
  const o = view.origin;
  return node("origin", selected, onPick, "origin",
    h("strong", { text: "원본 저장" }),
    h("span", { class: "gmeta", text: `증거 ${o.evidence} · 그래프 변경 ${o.graph_updates}` }),
    h("span", { class: "gmeta", text: `Finding ${o.findings} (검증 ${o.verified_findings}) · 가설 ${o.hypotheses ?? UNKNOWN}` }));
}
