// Right-hand detail for the selected node. Only stored or published-summary fields appear.
import { h, replace } from "./dom.js";
import { PLANNER, TASK, UNKNOWN, WAIT, WORKER } from "./labels.js";

const rows = (pairs) => h("dl", { class: "gdl" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v === null || v === undefined || v === "" ? UNKNOWN : String(v) })]));

function timeline(view, taskId) {
  const events = view.events.filter((e) => !taskId || e.data.mission_task_id === taskId || e.data.task_id === taskId).slice(0, 12).reverse();
  if (!events.length) return h("p", { class: "note", text: "표시할 기록이 없습니다." });
  return h("ol", { class: "gtimeline" }, events.map((e) => {
    const bits = ["operation", "tool_id", "verdict", "evidence_id"].map((k) => e.data[k]).filter(Boolean).join(" · ");
    return h("li", {}, h("span", { class: "mono", text: e.type }), bits ? ` — ${bits}` : "");
  }));
}

function planner(view) {
  const p = view.planner;
  return [h("h3", { text: "Planner" }), rows([
    ["상태", PLANNER[p.state]], ["판단 단계", p.stage], ["깨운 이벤트", p.trigger],
    ["목표", p.inputs.goal], ["증거 수", p.inputs.evidence], ["조사 Task 수", p.inputs.tasks],
    ["미해결 질문 수", p.inputs.open_questions],
    ["직전 판단", p.last_judgment && p.last_judgment.termination],
    ["직전 판단 사유", p.last_judgment && p.last_judgment.reason],
    ["등록한 Task(누적)", p.tasks_registered],
    ["모델 요청", `${p.usage.requests}건 · 진행 ${p.usage.in_flight} · 토큰 ${p.usage.known_tokens}`],
    ["운영자 안내", p.guidance_in_input ? "플래너 입력에 포함" : "플래너 판단 입력에 포함되지 않음"],
  ]), h("p", { class: "note", text: "저장되지 않는 값은 '알 수 없음'으로 표시합니다." })];
}

function task(view, id) {
  const t = view.tasks.find((x) => x.task_id === id);
  if (!t) return [h("p", { class: "note", text: "이 Task는 더 이상 목록에 없습니다." })];
  return [h("h3", { text: t.title }), rows([
    ["상태", TASK[t.status]], ["담당 역할", t.role], ["현재 워커", t.worker || "없음"],
    ["완료 조건", t.completion.join(" / ")], ["막힌 사유", t.blocking_reason || "없음"],
    ["대기", t.wait ? `${WAIT[t.wait.reason] || t.wait.reason}${t.wait.approval_ref ? ` · 승인 ${t.wait.approval_ref}` : ""}` : "없음"],
    ["증거 수", t.evidence], ["결과", t.has_result ? "저장됨" : "아직 없음"],
  ]), h("h4", { text: "기록" }), timeline(view, id)];
}

function worker(view, id) {
  const w = view.workers.find((x) => x.instance_id === id);
  if (!w) return [h("p", { class: "note", text: "이 워커는 더 이상 목록에 없습니다." })];
  return [h("h3", { text: w.instance_id }), rows([
    ["단계", WORKER[w.phase]], ["맡은 Task", w.task_id], ["모델 요청", w.model_requests], ["도구 호출", w.tool_calls],
    ["호출 중인 도구", w.current_tool || "없음"],
    ["대기", w.wait ? `${WAIT[w.wait.reason] || w.wait.reason}${w.wait.approval_ref ? ` · 승인 ${w.wait.approval_ref}` : ""}` : "없음"],
    ["슬롯", w.slot_released ? "반납됨(요청은 보존)" : "사용 중 또는 해당 없음"],
    ["저장된 증거", w.evidence_refs.join(", ") || "없음"],
  ]), h("h4", { text: "이 Task의 기록" }), timeline(view, w.task_id)];
}

function origin(view) {
  const o = view.origin;
  return [h("h3", { text: "원본 저장" }), rows([
    ["증거 참조", o.evidence], ["그래프 변경", o.graph_updates], ["Finding", o.findings],
    ["검증된 Finding", o.verified_findings], ["가설", o.hypotheses],
    ["남은 액션 예산", view.usage.budget_remaining],
  ])];
}

export function renderDetail(root, view, selected) {
  let body;
  if (!view || !view.available) body = [h("p", { class: "note", text: "상세를 볼 수 없습니다." })];
  else if (selected === "planner") body = planner(view);
  else if (selected === "origin") body = origin(view);
  else if (selected && selected.startsWith("task:")) body = task(view, selected.slice(5));
  else if (selected && selected.startsWith("worker:")) body = worker(view, selected.slice(7));
  else body = [h("p", { class: "note", text: "노드를 선택하면 상세가 여기에 표시됩니다." })];
  replace(root, body);
}
