// Shared presentation primitives. All runtime content remains plain text.
import { h, replace } from "./dom.js";

const PATHS = {
  target: ["M12 3v3m0 12v3M3 12h3m12 0h3", "M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0", "M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"],
  plus: ["M12 5v14M5 12h14"],
  chat: ["M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-5 3V6a2 2 0 0 1 2-2Z"],
  graph: ["M8 7l8 4M8 17l8-4", "M8 6a3 3 0 1 1-6 0 3 3 0 0 1 6 0M8 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0M22 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0"],
  shield: ["m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z", "m8 12 3 3 5-6"],
  alert: ["m12 3 10 18H2Z", "M12 9v5m0 3v.1"],
  file: ["M14 3H5v18h14V8Z", "M14 3v5h5M8 12h8M8 16h6"],
  settings: ["M4 7h16M4 17h16M8 4v6m8 4v6"],
  arrow: ["M12 19V5m-6 6 6-6 6 6"],
  chevron: ["m9 5 7 7-7 7"],
  check: ["m5 12 4 4L19 6"],
  terminal: ["m4 6 6 6-6 6m9 0h7"],
  pause: ["M8 5v14M16 5v14"],
  play: ["m8 4 12 8-12 8Z"],
  stop: ["M6 6h12v12H6Z"],
  pulse: ["M2 12h5l3-8 4 16 3-8h5"],
};

export function icon(name) {
  const ns = ["http:", "", "www.w3.org", "2000", "svg"].join("/");
  const svg = document.createElementNS(ns, "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.6", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: "ui-icon" })) svg.setAttribute(key, value);
  for (const d of PATHS[name] || PATHS.file) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export function hydrateIcons() {
  for (const node of document.querySelectorAll("[data-icon]")) node.replaceChildren(icon(node.dataset.icon));
}

export const badge = (text, tone = "") => h("span", { class: `pill ${tone}`, text });
export const dot = (tone = "") => h("span", { class: `status-dot ${tone}`, "aria-hidden": "true" });
export const textValue = (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
export function timestamp(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
}
export function emptyState(name, title, description) {
  return h("div", { class: "empty-state" }, h("span", { class: "empty-icon" }, icon(name)), h("strong", { text: title }), h("p", { class: "note", text: description }));
}

// Stream updates must not close expanded records or move keyboard focus.
export function replaceLive(root, ...children) {
  const opened = new Set([...root.querySelectorAll("details[open][data-key]")].map((node) => node.dataset.key));
  const focused = root.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  replace(root, ...children);
  for (const node of root.querySelectorAll("details[data-key]")) node.open = opened.has(node.dataset.key);
  if (focused) [...root.querySelectorAll("[data-focus]")].find((node) => node.dataset.focus === focused)?.focus({ preventScroll: true });
}
