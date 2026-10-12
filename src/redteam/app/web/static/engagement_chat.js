// Interview view: the assistant asks, the operator answers. The assistant only proposes a
// draft; reviewing and saving still go through the explicit confirmation step.
import { h, replace } from "./dom.js";
import { api } from "./api.js";
import { CODE, FIELD, describeError } from "./labels.js";

function bubble(role, text) {
  return h("div", { class: `eng-bubble ${role}` }, h("p", { text }));
}

function draftLines(draft) {
  const rows = [];
  const add = (label, value) => {
    const text = Array.isArray(value) ? value.join(", ") : value;
    if (text !== null && text !== undefined && text !== "") rows.push(h("li", { text: `${label}: ${text}` }));
  };
  add("이름", draft.name);
  add("목표", draft.objective);
  add("허용 호스트", draft.allowed_hosts);
  add("허용 도메인", draft.allowed_domains);
  add("허용 네트워크", draft.allowed_networks);
  add("허용 기능", draft.allowed_capabilities);
  add("금지 기능", draft.denied_capabilities);
  add("금지 행동", draft.denied_operations);
  add("추가 제약", draft.notes);
  add("최대 작업 횟수", draft.max_actions);
  return rows.length ? rows : [h("li", { class: "note", text: "아직 수집된 값이 없습니다." })];
}

export function renderChat({ body, error, id, baseDraft, onReview, onManual }) {
  let sessionId = null;
  let draft = baseDraft || {};
  let origins = {};
  let busy = false;
  const log = h("div", { class: "eng-chat-log", "aria-live": "polite" },
    bubble("assistant", id
      ? "수정하고 싶은 내용을 말씀해 주세요. 바꾸지 않은 항목은 그대로 둡니다."
      : "어떤 대상을 점검하려는지 알려 주세요. 허용된 주소와 제한, 예산은 제가 물어보겠습니다."));
  const summary = h("ul", { class: "eng-chat-draft" });
  const input = h("textarea", { rows: 2, maxlength: 2000, placeholder: "메시지를 입력하세요", "aria-label": "메시지" });
  const send = h("button", { type: "button", text: "보내기" });
  const review = h("button", { type: "button", text: "검토하고 저장", disabled: true });
  const manual = h("button", { type: "button", class: "ghost", text: "직접 입력으로 이동" });

  function paintDraft() {
    replace(summary, draftLines(draft));
  }

  function note(result) {
    const lines = [];
    if (result.acknowledgement) lines.push(result.acknowledgement);
    result.questions.forEach((q) => lines.push(q));
    if (lines.length) log.append(bubble("assistant", lines.join("\n")));
    for (const item of result.rejected) {
      log.append(bubble("system", `${FIELD[item.field] || item.field}: ${CODE[item.code] || item.code}`));
    }
    log.scrollTop = log.scrollHeight;
  }

  async function submit() {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    send.disabled = true;
    error.textContent = "";
    log.append(bubble("user", text));
    input.value = "";
    try {
      const result = await api.interview({ session_id: sessionId, id, draft: baseDraft, message: text });
      sessionId = result.session_id;
      draft = result.draft;
      origins = result.target_origins || {};
      note(result);
      paintDraft();
      review.disabled = false;
      if (result.complete) log.append(bubble("system", "필수 항목이 모두 모였습니다. '검토하고 저장'으로 확인하세요."));
    } catch (failure) {
      error.textContent = describeError(failure);
      input.value = text; // the message is kept so it can be resent
    } finally {
      busy = false;
      send.disabled = false;
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  const forReview = () => ({ ...draft,
    allowed_hosts: (draft.allowed_hosts || []).map((host) => origins[host] || host) });
  review.addEventListener("click", () => onReview(forReview()));
  manual.addEventListener("click", () => onManual(forReview()));
  paintDraft();
  replace(body,
    h("div", { class: "eng-chat" },
      h("div", { class: "eng-chat-main" }, log, h("div", { class: "eng-chat-input" }, input, send)),
      h("aside", { class: "eng-chat-side" }, h("strong", { text: "지금까지 정리된 내용" }), summary,
        h("p", { class: "note", text: "대화로 모은 값은 제안일 뿐입니다. 저장 전에 범위를 직접 확인합니다." }),
        h("div", { class: "row" }, manual, review))));
  input.focus();
}
