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
    dialog.style.maxHeight = "calc(100vh - 48px)";
    dialog.style.overflow = "auto";
    $("s-model").value = settings && settings.model ? settings.model : "";
    $("s-provider").value = settings && settings.provider ? settings.provider : "litellm";
    const effortLevels = (settings && settings.reasoning_effort_options) || [];
    const effortSelect = $("s-effort");
    replace(effortSelect, h("option", { value: "", text: "모델 기본값" }),
      effortLevels.map((level) => h("option", { value: level, text: level })));
    effortSelect.value = settings && settings.reasoning_effort ? settings.reasoning_effort : "";
    $("s-provider").dataset.initial = $("s-provider").value;
    effortSelect.dataset.initial = effortSelect.value;
    $("s-key").value = "";
    $("s-persist").checked = false;
    $("s-max").value = settings && settings.max_requests ? settings.max_requests : "";
    $("s-conc").value = settings && settings.max_concurrent_requests ? settings.max_concurrent_requests : "";
    const canReauthenticate = settings && settings.provider === "litellm"
      && settings.model && settings.model.startsWith("chatgpt/");
    $("s-reauth").hidden = !canReauthenticate;
    $("s-reauth").disabled = false;
    $("s-reauth-note").hidden = !canReauthenticate;
    $("s-reauth-note").textContent = canReauthenticate
      ? "401이 반복될 때 사용하세요. 로그인 URL과 코드는 서버를 실행한 터미널에 표시됩니다."
      : "";
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
    dialog.scrollTop = 0;
  }

  function collect() {
    const payload = {};
    const model = $("s-model").value.trim();
    const profile = $("s-profile").value;
    const max = $("s-max").value.trim();
    const conc = $("s-conc").value.trim();
    const key = $("s-key").value;
    if (model) payload.model = model;
    const provider = $("s-provider").value;
    if (provider && provider !== $("s-provider").dataset.initial) payload.provider = provider;
    const effort = $("s-effort").value;
    if (effort !== ($("s-effort").dataset.initial || "")) {
      payload.reasoning_effort = effort || "";
    }
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
  $("s-reauth").addEventListener("click", async () => {
    error.textContent = "";
    $("s-reauth").disabled = true;
    $("s-reauth-note").textContent = "재로그인을 시작했습니다. 서버 터미널의 URL과 코드를 확인하세요.";
    try {
      await api.reauthenticateChatGPT();
      dialog.close();
      afterApply("ChatGPT 재로그인 대기 중입니다. 터미널에서 인증을 완료하면 세션이 자동으로 다시 시작됩니다.");
    } catch (failure) {
      error.textContent = describeError(failure);
      $("s-reauth").disabled = false;
    }
  });
  $("open-settings").addEventListener("click", open);
  return { open };
}
