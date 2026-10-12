import { h, replace } from "./dom.js";
import { RECEIPT, VERDICT } from "./labels.js";

function inputEntry(entry) {
  const nodes = [
    h("div", { class: "msg user" }, h("div", { class: "bubble", text: entry.text })),
  ];
  if (entry.state !== "handled" && entry.state !== "dispatched") {
    nodes.push(h("div", { class: "receipt", text: `${RECEIPT[entry.state] || entry.state}${entry.reason ? ` · ${entry.reason}` : ""}` }));
  }
  const decision = entry.decision;
  if (decision && decision.response) {
    nodes.push(
      h("div", { class: "msg" },
        h("span", { class: "who", text: "에이전트" }),
        h("p", { class: "agent-text", text: decision.response }),
        decision.plan && decision.plan.length
          ? h("ol", {}, decision.plan.map((step) => h("li", { text: step })))
          : null),
    );
  }
  return nodes;
}

function agentEntry(entry) {
  return h("div", { class: "msg" },
    h("span", { class: "who", text: "에이전트" }),
    h("p", { class: "agent-text", text: entry.text }),
    entry.summary ? h("p", { class: "note", text: `판단 요약: ${entry.summary}` }) : null);
}

function actionEntry(entry) {
  const verdict = entry.verdict;
  const pillClass = verdict === "DENY" ? "pill bad" : verdict === "REQUIRE_APPROVAL" ? "pill wait" : "pill";
  return h("div", { class: "action-row" },
    h("span", { class: "mono", text: entry.operation }),
    entry.target ? h("span", { class: "mono", text: entry.target }) : null,
    verdict ? h("span", { class: pillClass, text: VERDICT[verdict] || verdict }) : null,
    entry.result ? h("span", { text: `${entry.result.ok ? "성공" : "실패"}${entry.result.text ? ` · ${entry.result.text}` : ""}` }) : null);
}

function approvalEntry(entry, store, handlers) {
  const pending = store.state && store.state.pending_approval === entry.approval_id;
  const action = store.actions.get(entry.action_id);
  const card = h("div", { class: "card approval" },
    h("h3", { text: pending ? "실행 승인이 필요합니다" : "승인 요청" }),
    action ? h("div", { class: "code", text: `${action.operation} ${action.target}`.trim() }) : null,
    entry.reasons.length ? h("p", { class: "note", text: entry.reasons.join(" · ") }) : null);
  if (pending) {
    card.append(h("div", { class: "row" },
      h("button", { type: "button", class: "btn primary", text: "승인 · 이번 1회", onClick: () => handlers.decide(entry, "approve") }),
      h("button", { type: "button", class: "btn", text: "거부", onClick: () => handlers.decide(entry, "deny") })));
  } else {
    card.append(h("p", { class: "note", text: "이미 처리되었거나 더 이상 대기 중이 아닙니다." }));
  }
  return card;
}

function findingEntry(entry) {
  return h("div", { class: "card finding" },
    h("div", { class: "row" }, h("span", { class: "pill bad", text: entry.verified ? "검증된 발견사항" : "발견사항" })),
    h("h3", { text: entry.title }));
}

export function renderThread(root, store, handlers) {
  const nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 80;
  const inner = h("div", { class: "thread-inner" });
  for (const entry of store.entries.values()) {
    if (entry.type === "input") inner.append(...inputEntry(entry));
    else if (entry.type === "agent") inner.append(agentEntry(entry));
    else if (entry.type === "action") inner.append(actionEntry(entry));
    else if (entry.type === "approval") inner.append(approvalEntry(entry, store, handlers));
    else if (entry.type === "finding") inner.append(findingEntry(entry));
    else if (entry.type === "note") inner.append(h("p", { class: "note", text: entry.text }));
  }
  if (!inner.childElementCount) {
    inner.append(h("p", { class: "empty", text: "에이전트에게 지시하거나 질문하세요. 데이터를 바꾸는 요청은 승인을 거쳐 진행됩니다." }));
  }
  replace(root, inner);
  if (nearBottom) root.scrollTop = root.scrollHeight;
}
