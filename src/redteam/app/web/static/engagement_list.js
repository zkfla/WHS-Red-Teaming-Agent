// Engagement picker: one file per investigation. Selecting is explicit; nothing is auto-chosen.
import { h } from "./dom.js";

export function listNode(data, handlers) {
  const items = data.items || [];
  const rows = items.map((item) => {
    const active = item.id === data.active;
    return h("li", { class: active ? "eng-item on" : "eng-item" },
      h("div", { class: "eng-main" },
        h("strong", { text: item.name }),
        h("span", { class: "note", text: item.hosts.length ? item.hosts.join(", ") : "범위 없음" }),
        item.valid ? null : h("span", { class: "note", text: "규칙 검사 실패 — 선택할 수 없음" })),
      h("div", { class: "eng-actions" },
        active ? h("span", { class: "pill", text: "사용 중" })
          : h("button", { type: "button", disabled: !item.valid, text: "선택", onclick: () => handlers.activate(item.id) }),
        h("button", { type: "button", class: "ghost", disabled: !item.editable, text: "수정", onclick: () => handlers.edit(item.id) }),
        h("button", { type: "button", class: "ghost", disabled: !item.editable, text: "대화로 수정", onclick: () => handlers.chatEdit(item.id) })));
  });
  return [
    items.length ? h("ul", { class: "eng-list" }, rows) : h("p", { class: "note", text: "아직 조사가 없습니다. 새 조사를 작성하세요." }),
    h("div", { class: "row" },
      h("button", { type: "button", class: "ghost", text: "직접 입력으로 작성", onclick: handlers.create }),
      h("button", { type: "button", text: "대화로 새 조사 작성", onclick: handlers.chat })),
  ];
}
