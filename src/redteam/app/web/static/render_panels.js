import { $, h, replace } from "./dom.js";
import { HOST, PHASE } from "./labels.js";
import { runList } from "./store.js";

const ACTIVE = new Set(["starting", "running", "waiting_approval", "paused"]);

function phaseOf(store) {
  return store.state ? store.state.phase : null;
}

export function renderTop(store) {
  const phase = phaseOf(store);
  $("engagement").textContent = store.session.engagement || "—";
  $("scope").textContent = `범위 ${store.session.scope.length}개 대상`;
  const badge = $("phase");
  const label = store.host.state === "ready" && phase ? PHASE[phase] || phase : HOST[store.host.state] || store.host.state;
  badge.textContent = label;
  badge.className = phase === "running" ? "pill run" : phase === "waiting_approval" || phase === "paused" || store.host.state !== "ready" ? "pill wait" : "pill";
  const usable = store.host.state === "ready" && !store.incompatible;
  $("btn-pause").disabled = !(usable && phase === "running");
  $("btn-resume").disabled = !(usable && phase === "paused");
  $("btn-stop").disabled = !(usable && ACTIVE.has(phase));
}

export function renderSide(store, streamLabel) {
  const runs = runList(store);
  replace($("runs"), runs.length ? runs.map((run) => h("li", { text: run.text })) : h("li", { text: "아직 조사가 없습니다." }));
  const error = store.host.error ? ` · ${store.host.error}` : "";
  $("env-session").textContent = `${HOST[store.host.state] || store.host.state}${error}`;
  $("env-stream").textContent = streamLabel;
}

export function renderComposer(store) {
  const ready = store.host.state === "ready";
  const note = $("composer-note");
  if (store.incompatible) {
    $("send").disabled = true;
    note.textContent = "이 화면과 에이전트의 API 버전이 맞지 않아 읽기 전용입니다. 화면 또는 에이전트를 같은 버전으로 맞추세요.";
    return;
  }
  $("send").disabled = !ready;
  if (ready) return;
  const needs = {
    needs_engagement: "Engagement를 작성해야 입력할 수 있습니다. 왼쪽의 '조사 선택'을 여세요.",
    needs_settings: "설정을 완료해야 입력할 수 있습니다. 왼쪽의 '설정'을 여세요.",
  };
  note.textContent = needs[store.host.state] || `세션 상태: ${HOST[store.host.state] || store.host.state}`;
}

function approvalPanel(store, handlers) {
  const pending = store.state && store.state.pending_approval;
  const entry = pending ? store.entries.get(`apr:${pending}`) : null;
  if (!pending) return [h("p", { class: "empty", text: "승인을 기다리는 요청이 없습니다." })];
  const action = entry ? store.actions.get(entry.action_id) : null;
  return [
    h("p", { class: "label", text: `승인 ID ${pending}` }),
    action ? h("div", { class: "code", text: `${action.operation} ${action.target}`.trim() }) : null,
    entry && entry.reasons.length ? h("p", { class: "note", text: entry.reasons.join(" · ") }) : null,
    h("p", { class: "note", text: "이 요청 한 건에만 적용됩니다. 처리 직전에 저장된 승인 기록과 Run을 다시 확인합니다." }),
    entry
      ? h("div", { class: "card", style: null },
        h("button", { type: "button", class: "btn primary", text: "승인 · 이번 1회", onClick: () => handlers.decide(entry, "approve") }),
        h("button", { type: "button", class: "btn", text: "거부", onClick: () => handlers.decide(entry, "deny") }))
      : h("p", { class: "note", text: "승인 요청의 Run 정보를 아직 받지 못했습니다." }),
  ];
}

export function renderInspector(store, handlers) {
  replace($("tab-approval"), approvalPanel(store, handlers));
  replace($("tab-activity"), store.activity.length
    ? store.activity.slice(-80).map((line) => h("div", { class: "log-line", text: `${line.ts} ${line.text}` }))
    : h("p", { class: "empty", text: "표시할 활동이 없습니다." }));
  replace($("tab-evidence"), store.evidence.length
    ? store.evidence.slice(-50).map((item) => h("div", { class: "log-line", text: `${item.ts} ${item.id}` }))
    : h("p", { class: "empty", text: "저장된 증거가 없습니다." }));
  const phase = phaseOf(store);
  const rows = [
    ["세션", HOST[store.host.state] || store.host.state],
    ["Run 단계", phase ? PHASE[phase] || phase : "알 수 없음"],
    ["현재 활동", store.state ? store.state.activity : "알 수 없음"],
    ["남은 액션 예산", store.budget === null ? "알 수 없음" : String(store.budget)],
    ["제안된 액션", String(store.actionCount)],
    ["토큰·시간", "알 수 없음"],
  ];
  replace($("usage"), rows.flatMap(([name, value]) => [h("dt", { text: name }), h("dd", { text: value })]));
}
