import { $, h, replace } from "./dom.js";
import { api } from "./api.js";
import { CODE, FIELD, describeError } from "./labels.js";

function intro(settings) {
  if (!settings || !settings.problems.length) return "설정이 완료되었습니다. 값을 바꾸면 새 조사부터 적용됩니다.";
  const missing = settings.problems.map((p) => `${FIELD[p.field] || p.field}(${CODE[p.code] || p.code})`);
  return `다음 항목을 입력하세요: ${missing.join(", ")}`;
}

export function setupSettings(getStore, afterApply) {
  const dialog = $("settings");
  const error = $("settings-error");

  async function open() {
    const settings = getStore().settings;
    error.textContent = "";
    $("settings-intro").textContent = intro(settings);
    $("s-model").value = settings && settings.model ? settings.model : "";
    $("s-key").value = "";
    $("s-persist").checked = false;
    $("s-max").value = settings && settings.max_requests ? settings.max_requests : "";
    $("s-conc").value = settings && settings.max_concurrent_requests ? settings.max_concurrent_requests : "";
    let profiles = [];
    try {
      profiles = (await api.profiles()).profiles;
    } catch (failure) {
      error.textContent = describeError(failure);
    }
    const select = $("s-profile");
    replace(select, h("option", { value: "", text: profiles.length ? "선택하세요" : "사용 가능한 프로필이 없습니다" }),
      profiles.map((p) => h("option", { value: p.id, text: p.label })));
    select.value = settings && settings.profile_id ? settings.profile_id : "";
    if (!dialog.open) dialog.showModal();
  }

  function collect() {
    const payload = {};
    const model = $("s-model").value.trim();
    const profile = $("s-profile").value;
    const max = $("s-max").value.trim();
    const conc = $("s-conc").value.trim();
    const key = $("s-key").value;
    if (model) payload.model = model;
    if (profile) payload.profile_id = profile;
    if (max) payload.max_requests = Number(max);
    if (conc) payload.max_concurrent_requests = Number(conc);
    if (key) {
      payload.api_key = key;
      if ($("s-persist").checked) payload.persist_key = true;
    }
    return payload;
  }

  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = collect();
    $("s-key").value = ""; // the key never stays in the page after it is sent
    error.textContent = "";
    $("settings-save").disabled = true;
    try {
      const result = await api.saveSettings(payload);
      dialog.close();
      const notes = [];
      if (result.overrode_environment.length) notes.push("환경 변수로 이미 지정된 값을 이번 실행에서 바꿨습니다. 다음 시작 때는 환경 변수 값이 우선합니다.");
      if (result.key_persisted && result.env_gitignored === false) notes.push(".env가 .gitignore에 없습니다. 커밋 전에 추가하세요.");
      afterApply(notes.join(" "));
    } catch (failure) {
      error.textContent = describeError(failure);
    } finally {
      $("settings-save").disabled = false;
    }
  });
  $("settings-cancel").addEventListener("click", () => dialog.close());
  $("open-settings").addEventListener("click", open);
  return { open };
}
