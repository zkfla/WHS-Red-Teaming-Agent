import { h } from "./dom.js";
import { RECEIPT, VERDICT } from "./labels.js";
import { pendingApprovalIds } from "./store.js";
import { badge, emptyState, icon, replaceLive, textValue, timestamp } from "./ui.js";

function author(time) {
  return h("div", { class: "message-author" }, h("span", { class: "agent-avatar" }, icon("target")),
    h("strong", { text: "에이전트" }), h("span", { class: "note", text: "Red Team Agent" }),
    h("time", { class: "note", text: timestamp(time) }));
}

function disclosure(key, title, ...children) {
  return h("details", { class: "record-details", "data-key": key },
    h("summary", { "data-focus": key }, icon("file"), title, icon("chevron")), ...children);
}

function inputEntry(entry) {
  const nodes = [h("div", { class: "msg user" }, h("div", { class: "bubble", text: entry.text }),
    h("time", { class: "note", text: timestamp(entry.received_at) }))];
  if (entry.state !== "handled" && entry.state !== "dispatched") {
    nodes.push(h("div", { class: "receipt" }, badge(RECEIPT[entry.state] || entry.state, "wait"),
      entry.reason ? h("span", { text: entry.reason }) : null,
      entry.diagnostic?.label ? disclosure(
        `diag:${entry.key}`, `실패 원인 · ${entry.diagnostic.code || "진단"}`,
        h("p", { class: "detail-text", text: entry.diagnostic.label }),
        entry.diagnostic.rule ? h("p", { class: "note mono", text: `규칙 ${entry.diagnostic.rule} · 위치 ${entry.diagnostic.location || "-"}` }) : null,
        entry.diagnostic.provider_status != null ? h("p", { class: "note mono", text: `제공자 상태 ${entry.diagnostic.provider_status}` }) : null,
        entry.diagnostic.provider_code ? h("p", { class: "note mono", text: `제공자 코드 ${entry.diagnostic.provider_code}` }) : null,
        entry.diagnostic.retry_after != null ? h("p", { class: "note mono", text: `재시도 대기 ${entry.diagnostic.retry_after}초` }) : null,
        entry.diagnostic.repair_condition ? h("p", { class: "note", text: entry.diagnostic.repair_condition }) : null,
      ) : null));
  }
  const decision = entry.decision;
  if (decision?.response) {
    nodes.push(h("div", { class: "msg" }, author(entry.received_at),
      h("p", { class: "agent-text", text: decision.response }),
      decision.plan?.length ? disclosure(`plan:${entry.key}`, `응답 계획 · ${decision.plan.length}개 항목`,
        h("ol", { class: "plan-list" }, decision.plan.map((step) => h("li", { text: step })))) : null));
  }
  return nodes;
}

function agentEntry(entry) {
  return h("div", { class: "msg" }, author(entry.ts), h("p", { class: "agent-text", text: entry.text }),
    entry.summary ? disclosure(`summary:${entry.key}`, "판단 요약", h("p", { class: "detail-text", text: entry.summary })) : null);
}

function actionEntry(entry, store) {
  const verdict = entry.verdict;
  const count = store.evidence.filter((item) => item.action_id === entry.action_id).length;
  return h("details", { class: "tool-record", "data-key": entry.key },
    h("summary", { "data-focus": entry.key }, icon("terminal"),
      h("span", { class: "tool-label", text: entry.operation }),
      entry.result ? badge(entry.result.ok ? "실행 성공" : "실행 실패", entry.result.ok ? "" : "bad")
        : verdict ? badge(VERDICT[verdict] || verdict, verdict === "ALLOW" ? "" : "wait") : badge("제안됨"),
      count ? h("span", { class: "note", text: `증거 ${count}건` }) : null, icon("chevron")),
    h("div", { class: "tool-body" },
      entry.target ? h("div", { class: "code", text: textValue(entry.target) }) : null,
      entry.result?.text ? h("p", { class: "detail-text", text: entry.result.text }) : null,
      entry.rules?.length ? h("p", { class: "note", text: entry.rules.map(textValue).join(" · ") }) : null,
      h("p", { class: "note mono", text: `Action · ${entry.action_id}` })));
}

function approvalEntry(entry, store, handlers) {
  const pending = pendingApprovalIds(store).includes(entry.approval_id);
  const stopped = !pending && store.state?.termination_reason === "operator_stopped"
    && store.state?.binding?.run_id === entry.run_id;
  const action = store.actions.get(entry.action_id);
  return h("div", { class: `card approval${pending ? " pending" : ""}` },
    h("div", { class: "card-heading" }, icon("shield"), h("h3", { text: pending ? "다음 단계를 진행하려면 승인이 필요해요" : "승인 요청 기록" })),
    action ? h("div", { class: "code", text: `${action.operation} ${textValue(action.target)}`.trim() }) : null,
    h("div", { class: "row" }, badge(pending ? "승인 대기" : "대기 종료", pending ? "wait" : ""),
      h("span", { class: "note mono", text: entry.approval_id })),
    entry.reasons.length ? h("p", { class: "note", text: entry.reasons.map(textValue).join(" · ") }) : null,
    pending ? h("div", { class: "row" },
      h("button", { type: "button", class: "btn primary", "data-focus": `review:${entry.key}`, onClick: () => handlers.inspect("approval") }, "검토하고 결정", icon("chevron")),
      h("button", { type: "button", class: "btn", text: "거부", disabled: store.incompatible || !entry.run_id, "data-focus": `deny:${entry.key}`, onClick: () => handlers.decide(entry, "deny") }))
      : h("p", { class: "note", text: stopped
        ? "Run이 중단되어 이 요청은 더 이상 실행할 수 없습니다."
        : "처리되었거나 더 이상 실행 가능한 대기 요청이 아닙니다." }));
}

function findingEntry(entry) {
  return h("div", { class: "card finding" }, h("div", { class: "finding-icon" }, icon(entry.verified ? "check" : "alert")),
    h("div", { class: "finding-content" },
      h("div", { class: "row" }, badge("발견사항"), badge(entry.verified ? "검증됨" : "검증 미확인", entry.verified ? "" : "wait")),
      h("h3", { text: entry.title })));
}

export function renderThread(root, store, handlers) {
  const nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 80;
  const inner = h("div", { class: "thread-inner" });
  if (store.host.error) {
    inner.append(h("section", { class: "card error-card", role: "status" },
      h("div", { class: "card-heading" }, icon("alert"), h("h3", { text: "세션에서 오류가 발생했습니다" })),
      h("p", { class: "note", text: "오류 상세를 확인하고 실행 설정을 점검해 주세요." }),
      disclosure("host-error", "오류 상세 보기", h("p", { class: "detail-text mono", text: store.host.error }))));
  }
  for (const entry of store.entries.values()) {
    const group = h("article", { class: "thread-entry", "data-entry": entry.key });
    if (entry.type === "input") group.append(...inputEntry(entry));
    else if (entry.type === "agent") group.append(agentEntry(entry));
    else if (entry.type === "action") group.append(actionEntry(entry, store));
    else if (entry.type === "approval") group.append(approvalEntry(entry, store, handlers));
    else if (entry.type === "finding") group.append(findingEntry(entry));
    else if (entry.type === "note") group.append(disclosure(entry.key, "조사 상태 기록", h("p", { class: "detail-text", text: entry.text })));
    if (group.childElementCount) inner.append(group);
  }
  if (!inner.childElementCount) {
    inner.append(h("div", { class: "welcome" }, emptyState("target", "무엇을 조사할까요?", "조사 범위와 실행 설정을 준비한 뒤, 에이전트에게 목표를 알려주세요."),
      h("div", { class: "welcome-steps" },
        h("div", {}, badge("01"), h("strong", { text: "범위 설정" }), h("p", { class: "note", text: "대상과 제한을 정합니다" })),
        h("div", {}, badge("02"), h("strong", { text: "함께 조사" }), h("p", { class: "note", text: "진행 상황을 대화로 확인합니다" })),
        h("div", {}, badge("03"), h("strong", { text: "결과 검토" }), h("p", { class: "note", text: "발견사항과 증거를 확인합니다" })))));
  }
  replaceLive(root, inner);
  if (nearBottom) root.scrollTop = root.scrollHeight;
}
