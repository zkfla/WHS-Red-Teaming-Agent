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

export function fieldNode(def, draft, onChange) {
  if (def.choices) {
    if (draft[def.name] == null) draft[def.name] = def.default;
    const control = h("select", { name: def.name,
      onchange: (event) => {
        draft[def.name] = event.target.value;
        if (def.name === "web_policy_mode" && event.target.value !== "paths") draft.web_paths = [];
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
