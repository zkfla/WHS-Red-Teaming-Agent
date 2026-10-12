// Engagement interview: basic -> scope -> limits -> budget -> review. The server converts,
// validates and writes; this file only collects explicit values and shows the result.
import { $, h, replace } from "./dom.js";
import { api } from "./api.js";
import { describeError } from "./labels.js";
import { fieldNode } from "./engagement_fields.js";
import { renderChat } from "./engagement_chat.js";
import { listNode } from "./engagement_list.js";
import { summaryNode } from "./engagement_summary.js";

const REVIEW = { id: "review", label: "확인", fields: [] };

export function setupEngagement(afterSave) {
  const dialog = $("engagement-dialog");
  const body = $("eng-body");
  const error = $("eng-error");
  let model = null;
  let draft = {};
  let index = 0;
  let preview = null;
  let currentId = null;
  let confirmScope = null;
  let confirmWide = null;

  const steps = () => [...model.steps, REVIEW];
  const step = () => steps()[index];

  function steps_bar() {
    return h("ol", { class: "steps" }, steps().map((item, i) =>
      h("li", { class: i === index ? "on" : i < index ? "done" : "", text: item.label })));
  }

  function savable() {
    return Boolean(preview) && Boolean(confirmScope) && confirmScope.checked
      && (!preview.scope_expanded || Boolean(confirmWide && confirmWide.checked));
  }

  function refreshSave() {
    $("eng-save").disabled = !savable();
  }

  function reviewBody() {
    const nodes = [...summaryNode(preview)];
    confirmScope = h("input", { type: "checkbox", onchange: refreshSave });
    confirmWide = preview.scope_expanded ? h("input", { type: "checkbox", onchange: refreshSave }) : null;
    nodes.push(h("label", { class: "check" }, confirmScope, "위 범위가 허용된 점검 대상임을 확인했습니다."));
    if (confirmWide) nodes.push(h("label", { class: "check" }, confirmWide, "범위가 넓어지는 변경임을 확인했습니다."));
    return nodes;
  }

  function paint() {
    const current = step();
    const nodes = [steps_bar()];
    if (current.id === "review") nodes.push(...reviewBody());
    else {
      const byName = new Map(model.fields.map((f) => [f.name, f]));
      current.fields.forEach((name) => nodes.push(fieldNode(byName.get(name), draft, () => {
        if (name === "web_policy_mode") paint();
      })));
    }
    replace(body, nodes);
    $("eng-prev").disabled = index === 0;
    $("eng-next").hidden = current.id === "review";
    $("eng-save").hidden = current.id !== "review";
    refreshSave();
  }

  async function go(delta) {
    error.textContent = "";
    const next = index + delta;
    if (next < 0 || next >= steps().length) return;
    if (steps()[next].id === "review") {
      try {
        preview = await api.previewEngagement(currentId, draft);
      } catch (failure) {
        error.textContent = describeError(failure);
        return;
      }
    }
    index = next;
    paint();
  }

  async function save() {
    error.textContent = "";
    $("eng-save").disabled = true;
    try {
      await api.saveEngagement(currentId, draft, confirmScope.checked, Boolean(confirmWide && confirmWide.checked));
      dialog.close();
      afterSave();
    } catch (failure) {
      error.textContent = describeError(failure);
      refreshSave();
    }
  }

  function setButtons(wizard) {
    for (const id of ["eng-prev", "eng-next", "eng-save"]) $(id).hidden = !wizard;
    $("eng-back").hidden = false;
  }

  async function openList() {
    error.textContent = "";
    $("eng-title").textContent = "조사 선택";
    $("eng-intro").textContent = "조사마다 범위·제한·예산이 다른 Engagement 파일을 씁니다. 사용할 조사를 직접 선택하세요.";
    setButtons(false);
    $("eng-back").hidden = true;
    try {
      replace(body, listNode(await api.engagements(), {
        activate, edit: (id) => openWizard(id), chatEdit: openChat,
        create: () => openWizard(null), chat: () => openChat(null),
      }));
    } catch (failure) {
      error.textContent = describeError(failure);
    }
    if (!dialog.open) dialog.showModal();
  }

  async function activate(id) {
    error.textContent = "";
    try {
      await api.activateEngagement(id);
      dialog.close();
      afterSave();
    } catch (failure) {
      error.textContent = describeError(failure);
    }
  }

  async function openWizard(id, options = {}) {
    error.textContent = "";
    try {
      model = await api.engagement(id);
    } catch (failure) {
      error.textContent = describeError(failure);
      return;
    }
    currentId = id;
    const status = model.status;
    $("eng-title").textContent = id ? "Engagement 수정" : "새 조사 작성";
    $("eng-intro").textContent = id
      ? "사용 중인 조사를 수정하면 저장 후 세션이 새 설정으로 다시 시작됩니다. 범위는 점검 허가 범위입니다."
      : "점검 범위와 제한을 직접 입력합니다. 저장하면 이 조사가 사용 중으로 바뀝니다. 범위는 점검 허가 범위입니다.";
    if (id && !status.editable) {
      replace(body, h("p", { text: "직접 작성된 규칙이 있어 화면에서 수정할 수 없습니다. 파일을 직접 편집하세요." }));
      setButtons(false);
      return;
    }
    setButtons(true);
    draft = options.draft || (status.draft ? JSON.parse(JSON.stringify(status.draft)) : {});
    // Chat review can skip the form: the automatic policy choice must still be
    // explicit in the review the operator confirms, rather than an invisible default.
    if (draft.web_policy_mode == null) draft.web_policy_mode = "site";
    if (draft.web_paths == null) draft.web_paths = [];
    index = 0;
    preview = null;
    paint();
    if (options.review) {
      // Skip to the confirmation; a failed preview leaves the operator on the first step.
      await go(steps().length - 1);
    }
  }

  async function openChat(id) {
    error.textContent = "";
    let baseDraft = {};
    if (id) {
      try {
        const loaded = await api.engagement(id);
        if (!loaded.status.editable) {
          error.textContent = describeError({ body: { error: "not_managed" } });
          return;
        }
        baseDraft = loaded.status.draft || {};
      } catch (failure) {
        error.textContent = describeError(failure);
        return;
      }
    }
    $("eng-title").textContent = id ? "대화로 수정" : "대화로 새 조사 작성";
    $("eng-intro").textContent = "어시스턴트가 묻는 대로 답하세요. 비밀번호·키·토큰은 입력하지 마세요.";
    setButtons(false);
    renderChat({
      body, error, id, baseDraft,
      onReview: (collected) => openWizard(id, { draft: collected, review: true }),
      onManual: (collected) => openWizard(id, { draft: collected }),
    });
  }

  async function open() {
    await openList();
  }

  $("eng-prev").addEventListener("click", () => go(-1));
  $("eng-next").addEventListener("click", () => go(1));
  $("eng-save").addEventListener("click", save);
  $("eng-cancel").addEventListener("click", () => dialog.close());
  $("eng-back").addEventListener("click", openList);
  $("engagement-form").addEventListener("submit", (event) => event.preventDefault());
  $("open-engagement").addEventListener("click", open);
  return { open };
}
