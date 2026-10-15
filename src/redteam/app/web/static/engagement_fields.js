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

function endpointBox(def, draft, onChange) {
  const origins = draft.allowed_hosts || [];
  const selected = new Map();
  origins.forEach((value) => {
    try {
      const url = new URL(value.includes("://") ? value : `http://${value}`);
      if (["http:", "https:"].includes(url.protocol)) selected.set(url.origin, url);
    } catch (_) {
      // The server reports the field error on preview; keep typing usable meanwhile.
    }
  });
  const rows = draft.kali_endpoints || (draft.kali_endpoints = []);
  for (const [origin, url] of selected) {
    if (!rows.some((item) => item.origin === origin)) {
      rows.push({ origin, port: Number(url.port) || (url.protocol === "https:" ? 443 : 80), transport: "tcp" });
    }
  }
  draft.kali_endpoints = rows.filter((item) => selected.has(item.origin));
  return h("fieldset", { class: "choices" },
    h("legend", { text: def.label }),
    h("p", { class: "note", text: "선택 대상마다 포트와 전송 방식을 지정합니다. 추가 포트는 비워 두지 말고 직접 입력하세요. 원래 허용 범위 안에서만 사용할 수 있습니다." }),
    ...[...selected.entries()].flatMap(([origin]) => {
      const endpoints = draft.kali_endpoints.filter((item) => item.origin === origin);
      return [
        h("h3", { text: origin }),
        ...endpoints.map((item) => h("div", { class: "kali-endpoint" },
          h("label", {}, "대상", h("input", { type: "text", disabled: true, value: item.origin })),
          h("label", {}, "포트", h("input", {
            type: "number", min: 1, max: 65535, step: 1, value: item.port ?? "",
            oninput: (event) => {
              const value = event.target.value.trim();
              item.port = value === "" ? null : Number(value);
              onChange();
            },
          })),
          h("label", {}, "전송 방식", h("select", {
            value: item.transport,
            onchange: (event) => { item.transport = event.target.value; onChange(); },
          }, ["tcp", "udp"].map((value) => h("option", {
            value, selected: item.transport === value, text: value.toUpperCase(),
          }))),
          endpoints.length > 1 ? h("button", { type: "button", text: "포트 제거", onclick: () => {
            draft.kali_endpoints.splice(draft.kali_endpoints.indexOf(item), 1);
            onChange(true);
          } }) : null)),
        h("button", { type: "button", text: "이 대상에 포트 추가", onclick: () => {
          draft.kali_endpoints.push({ origin, port: null, transport: "tcp" });
          onChange(true);
        } }),
      ];
    }));
}

export function fieldNode(def, draft, onChange) {
  if (def.name === "web_sessions") return sessionBox(def, draft, onChange);
  if (def.name === "kali_endpoints") return endpointBox(def, draft, onChange);
  if (def.choices) {
    if (draft[def.name] == null && def.default != null) draft[def.name] = def.default;
    const control = h("select", { name: def.name,
      onchange: (event) => {
        draft[def.name] = event.target.value || null;
        if (def.name === "web_policy_mode" && event.target.value !== "paths") draft.web_paths = [];
        if (def.name === "web_policy_mode" && event.target.value === "manual") {
          draft.web_permissions = "read_only"; draft.web_sessions = [];
          draft.kali_network_mode = "inherit"; draft.kali_network_policy_path = null;
          clearPreparation(draft);
        }
        if (def.name === "kali_network_mode") {
          if (event.target.value === "prepare") {
            draft.kali_network_policy_path = null;
          } else {
            draft.kali_network_policy_path = event.target.value === "configured"
              ? draft.kali_network_policy_path : null;
            clearPreparation(draft);
          }
        }
        if (def.name === "kali_limits_preset" && event.target.value !== "custom") clearCustomLimits(draft);
        if (def.name === "kali_sidecar_mode" && event.target.value !== "pinned") draft.kali_sidecar_image = null;
        onChange();
      },
    }, (def.name === "kali_limits_preset" || def.name === "kali_sidecar_mode"
      ? [{ value: "", label: "직접 선택하세요" }, ...def.choices] : def.choices).map((choice) => h("option", {
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
  if (def.kind === "integer" || def.kind === "number") {
    control = h("input", {
      ...common, type: "number", min: def.minimum ?? 0,
      ...(def.maximum == null ? {} : { max: def.maximum }),
      step: def.kind === "integer" ? 1 : "any",
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

function clearCustomLimits(draft) {
  draft.kali_limit_deliveries = null;
  draft.kali_limit_output_bytes = null;
  draft.kali_limit_duration_seconds = null;
  draft.kali_limit_delivery_rate_per_second = null;
}

function clearPreparation(draft) {
  draft.kali_endpoints = [];
  draft.kali_limits_preset = null;
  clearCustomLimits(draft);
  draft.kali_sidecar_mode = null;
  draft.kali_sidecar_image = null;
  draft.kali_docker_context = null;
}
