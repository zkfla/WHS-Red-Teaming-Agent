// Field widgets for the engagement interview. Values live in a plain draft object.
import { h } from "./dom.js";

const LONG = new Set(["description", "objective", "allowed_hosts", "denied_operations", "notes"]);

export function listText(values) {
  return (values || []).join(", ");
}

export function parseList(text) {
  return text.split(",").map((item) => item.trim()).filter(Boolean);
}

function capabilityBox(def, draft, onChange) {
  if (draft[def.name] == null && Array.isArray(def.default)) draft[def.name] = [...def.default];
  const chosen = new Set(draft[def.name] || []);
  return h("fieldset", { class: "choices" },
    h("legend", { text: def.label }),
    def.options.map((option) =>
      h("label", { class: "check" },
        h("input", {
          type: "checkbox",
          checked: chosen.has(option),
          onchange: (event) => {
            if (event.target.checked) chosen.add(option);
            else chosen.delete(option);
            draft[def.name] = def.options.filter((item) => chosen.has(item));
            onChange();
          },
        }),
        option)));
}

function sessionBox(def, draft, onChange) {
  const sessions = draft.web_sessions || (draft.web_sessions = []);
  const origins = draft.allowed_hosts || [];
  return h("fieldset", { class: "choices" }, h("legend", { text: def.label }),
    h("p", { class: "note", text: "비밀번호·토큰 값 대신 환경변수 이름 또는 로그인 상태 파일을 지정하세요. 실제 로그인 여부는 실행 시 확인됩니다." }),
    sessions.map((session, index) => h("fieldset", {},
      h("legend", { text: `계정 ${index + 1}` }),
      [
        ["session_ref", "Session 이름"], ["principal_ref", "사용자 이름 (권한 비교용)"],
        ["origin", "대상 사이트 URL"], ["paths", "인증을 사용할 경로 (쉼표로 구분)"],
        ["bearer_env", "Bearer 토큰 환경변수 이름"],
        ["storage_state_path", "로그인 상태 파일의 절대 경로 (Bearer와 둘 중 하나)"],
      ].map(([key, label]) => h("label", {}, label,
        h("input", { type: "text", autocomplete: "off",
          value: key === "paths" ? listText(session.paths) : session[key] || "",
          oninput: (event) => {
            session[key] = key === "paths" ? parseList(event.target.value) : event.target.value.trim() || null;
            onChange();
          } }))),
      h("button", { type: "button", text: "계정 제거", onclick: () => {
        sessions.splice(index, 1); onChange(true);
      } }))),
    h("button", { type: "button", text: "계정 추가", onclick: () => {
      sessions.push({ session_ref: "", principal_ref: "", origin: origins.length === 1 ? origins[0] : "",
        paths: draft.web_policy_mode === "paths" ? [...(draft.web_paths || [])] : ["/"],
        bearer_env: null, storage_state_path: null, expected_access: "unspecified" });
      onChange(true);
    } }));
}

export function fieldNode(def, draft, onChange) {
  if (def.name === "web_sessions") return sessionBox(def, draft, onChange);
  if (def.choices) {
    if (draft[def.name] == null) draft[def.name] = def.default;
    const control = h("select", { name: def.name,
      onchange: (event) => {
        draft[def.name] = event.target.value;
        if (def.name === "web_policy_mode" && event.target.value !== "paths") draft.web_paths = [];
        if (def.name === "web_policy_mode" && event.target.value === "manual") {
          draft.web_permissions = "read_only"; draft.web_sessions = [];
          draft.kali_network_mode = "inherit"; draft.kali_network_policy_path = null;
        }
        onChange();
      },
    }, def.choices.map((choice) => h("option", {
      value: choice.value, selected: draft[def.name] === choice.value, text: choice.label,
    })));
    return h("label", {}, def.label, control);
  }
  if (def.options) return capabilityBox(def, draft, onChange);
  if (def.kind === "boolean") {
    return h("label", { class: "check" },
      h("input", {
        type: "checkbox",
        checked: draft[def.name] === true,
        onchange: (event) => {
          draft[def.name] = event.target.checked;
          onChange();
        },
      }),
      def.label);
  }
  const common = { name: def.name, autocomplete: "off", spellcheck: "false",
    disabled: def.name === "web_paths" && draft.web_policy_mode !== "paths" };
  let control;
  if (def.kind === "integer") {
    control = h("input", {
      ...common, type: "number", min: 0, step: 1,
      value: draft[def.name] ?? (def.default ?? ""),
      oninput: (event) => {
        const text = event.target.value.trim();
        draft[def.name] = text === "" ? null : Number(text);
        onChange();
      },
    });
    if (draft[def.name] == null && def.default != null) draft[def.name] = def.default;
  } else if (def.kind === "array") {
    control = h("input", {
      ...common, type: "text", value: listText(draft[def.name]),
      oninput: (event) => {
        draft[def.name] = parseList(event.target.value);
        onChange();
      },
    });
  } else {
    const tag = LONG.has(def.name) ? "textarea" : "input";
    control = h(tag, {
      ...common, ...(tag === "input" ? { type: "text" } : { rows: 3 }),
      value: tag === "input" ? draft[def.name] || "" : null,
      oninput: (event) => {
        draft[def.name] = event.target.value;
        onChange();
      },
    });
    if (tag === "textarea") control.value = draft[def.name] || "";
  }
  return h("label", {}, def.label, control);
}
