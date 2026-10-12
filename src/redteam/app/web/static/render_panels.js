import { $, h, replace } from "./dom.js";
import { HOST, PHASE, RECEIPT } from "./labels.js";
import { runList } from "./store.js";
import { badge, dot, emptyState, icon, replaceLive, textValue } from "./ui.js";

const ACTIVE = new Set(["starting", "running", "waiting_approval", "paused"]);
const EVENT_LABEL = {
  RunStarted: "조사 시작", RunFinished: "조사 종료", AgentTurnDecided: "에이전트 응답",
  ActionProposed: "작업 제안", PolicyEvaluated: "정책 검토", ActionDenied: "작업 차단",
  ToolExecutionFinished: "도구 실행 완료", ToolExecutionFailed: "도구 실행 실패",
  ApprovalRequired: "승인 요청", EvidenceCreated: "증거 저장",
  FindingCreated: "발견사항 등록", FindingVerified: "발견사항 검증", TaskCreated: "조사 작업 등록",
};

export function renderTop(store) {
  const phase = store.state?.phase;
  $("headline").textContent = store.session.engagement || "새로운 조사 준비";
  $("scope").textContent = `범위 ${store.session.scope.length}개 대상`;
  const label = store.host.state === "ready" && phase ? PHASE[phase] || phase : HOST[store.host.state] || store.host.state;
  const tone = phase === "running" ? "run" : phase === "waiting_approval" || phase === "paused" || store.host.state !== "ready" ? "wait" : "";
  replace($("phase"), dot(), label);
  $("phase").className = `pill ${tone}`;
  const usable = store.host.state === "ready" && !store.incompatible;
  $("btn-pause").hidden = phase === "paused";
  $("btn-resume").hidden = phase !== "paused";
  $("btn-pause").disabled = !(usable && phase === "running");
  $("btn-resume").disabled = !(usable && phase === "paused");
  $("btn-stop").disabled = !(usable && ACTIVE.has(phase));
  replace($("session-strip"),
    h("span", { class: "strip-state" }, icon("pulse"), label),
    h("span", { class: "strip-description", text: store.state?.activity || (store.host.state === "needs_engagement" ? "새 조사를 만들거나 기존 조사를 선택하세요." : "조사 상태와 실행 기록을 이곳에서 확인합니다.") }),
    store.state?.pending_approval ? badge("검토 필요", "wait") : null);
}

export function renderSide(store, streamLabel, handlers) {
  const runs = runList(store);
  replaceLive($("runs"), runs.length ? runs.slice().reverse().map((run) => h("li", {},
    h("button", { type: "button", class: "run-link", title: run.text, "data-focus": run.key, onClick: () => handlers.jump(run.key) },
      icon("chat"), h("span", { class: "run-copy" }, h("span", { class: "run-title", text: run.text }),
        h("small", { text: RECEIPT[run.state] || run.state })))))
    : h("li", { class: "side-empty", text: "조사 요청이 여기에 모입니다." }));
  $("env-session").textContent = HOST[store.host.state] || store.host.state;
  $("env-stream").textContent = streamLabel;
  const count = store.state?.pending_approval ? 1 : 0;
  $("approval-count").textContent = String(count);
  $("approval-count").hidden = count === 0;
}

export function renderComposer(store) {
  const ready = store.host.state === "ready";
  const note = $("composer-note");
  $("send").disabled = !ready || store.incompatible;
  for (const button of document.querySelectorAll("[data-command]")) button.disabled = !ready || store.incompatible;
  if (store.incompatible) {
    note.textContent = "이 화면과 에이전트의 API 버전이 맞지 않아 읽기 전용입니다. 화면 또는 에이전트를 같은 버전으로 맞추세요.";
    note.dataset.statusNote = "true";
    return;
  }
  if (ready) {
    if (note.dataset.statusNote) note.textContent = "";
    delete note.dataset.statusNote;
    return;
  }
  const needs = {
    needs_engagement: "‘새 조사’ 또는 ‘조사 선택’에서 점검할 범위를 준비하세요.",
    needs_settings: "‘설정’에서 모델과 실행 설정을 완료하세요.",
  };
  note.textContent = needs[store.host.state] || `세션 상태: ${HOST[store.host.state] || store.host.state}`;
  note.dataset.statusNote = "true";
}

function approvalPanel(store, handlers) {
  const pending = store.state?.pending_approval;
  const entry = pending ? store.entries.get(`apr:${pending}`) : null;
  if (!pending) return [emptyState("shield", "대기 중인 승인이 없습니다", "검토가 필요한 요청이 생기면 이곳에 표시됩니다.")];
  const action = entry ? store.actions.get(entry.action_id) : null;
  return [h("div", { class: "approval-review" },
    h("div", { class: "section-heading" }, h("h3", { text: "실행 요청 검토" }), badge("승인 대기", "wait")),
    h("p", { class: "note mono", text: pending }),
    action ? h("div", { class: "code", text: `${action.operation}\n${textValue(action.target)}`.trim() }) : null,
    h("h4", { text: "승인이 필요한 이유" }),
    entry?.reasons.length ? h("ul", { class: "review-reasons" }, entry.reasons.map((reason) => h("li", {}, icon("shield"), h("span", { text: textValue(reason) }))))
      : h("p", { class: "note", text: "추가 검토 정보를 기다리고 있습니다." }),
    h("p", { class: "approval-scope", text: "이 요청 한 건에만 적용됩니다. 승인 대상과 Run은 처리 직전에 다시 확인합니다." }),
    entry ? h("div", { class: "approval-buttons" },
      h("button", { type: "button", class: "btn primary", text: "승인 · 이번 1회", "data-focus": `approve:${pending}`, disabled: store.incompatible || !entry.run_id, onClick: () => handlers.decide(entry, "approve") }),
      h("button", { type: "button", class: "btn", text: "거부", "data-focus": `reject:${pending}`, disabled: store.incompatible || !entry.run_id, onClick: () => handlers.decide(entry, "deny") }))
      : h("p", { class: "note", text: "승인 요청의 Run 정보를 아직 받지 못했습니다." }))];
}

export function renderInspector(store, handlers) {
  replaceLive($("tab-approval"), approvalPanel(store, handlers));
  replaceLive($("tab-activity"), store.activity.length
    ? store.activity.slice(-80).reverse().map((line) => h("details", { class: "activity-record", "data-key": `activity:${line.key}` },
      h("summary", { "data-focus": `activity:${line.key}` }, dot(), h("span", { text: EVENT_LABEL[line.text] || (line.kind === "console" ? "실행 메시지" : line.text) }), h("time", { text: line.ts })),
      h("p", { class: "detail-text mono", text: line.text })))
    : emptyState("pulse", "아직 실행 기록이 없습니다", "작업이 진행되면 최근 활동부터 표시됩니다."));
  replaceLive($("tab-evidence"), store.evidence.length
    ? store.evidence.slice(-50).reverse().map((item) => h("details", { class: "evidence-record", "data-key": `evidence:${item.id}` },
      h("summary", { "data-focus": `evidence:${item.id}` }, icon("file"), h("span", { class: "evidence-name", text: item.id }), h("time", { text: item.ts })),
      h("p", { class: "detail-text mono", text: `증거 ID · ${item.id}\nAction · ${item.action_id || "연결 정보 없음"}` })))
    : emptyState("file", "아직 저장된 증거가 없습니다", "도구가 수집한 증거의 참조 정보를 확인할 수 있습니다."));
  replace($("usage"), [
    ["세션", HOST[store.host.state] || store.host.state],
    ["실행 단계", store.state?.phase ? PHASE[store.state.phase] || store.state.phase : "시작 전"],
    ["제안된 액션", String(store.actionCount)],
    ["남은 액션 예산", store.budget === null ? "정보 없음" : String(store.budget)],
  ].map(([name, value]) => h("div", {}, h("dt", { text: name }), h("dd", { text: value }))));
}
