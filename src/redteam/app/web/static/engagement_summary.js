// Read-only review of the engagement a save would write.
import { h } from "./dom.js";
import { FIELD } from "./labels.js";

function row(label, value) {
  return [h("dt", { text: label }), h("dd", { text: value || "—" })];
}

function join(values) {
  return values && values.length ? values.join(", ") : "";
}

export function summaryNode(preview) {
  const s = preview.summary;
  const parts = [h("dl", { class: "review" },
    row("이름", s.name), row("설명", s.description), row("목표", s.objective),
    row("허용 호스트", join(s.scope.allowed_hosts)),
    row("허용 도메인", join(s.scope.allowed_domains)),
    row("허용 네트워크", join(s.scope.allowed_networks)),
    row("웹 탐색 범위", s.web_policy
      ? s.web_policy.mode === "site" ? "선택한 사이트의 모든 경로"
        : `지정 경로만: ${join(s.web_policy.paths)}` : "기존 MCP 설정 사용"),
    row("대상 사이트", s.web_policy ? join(s.web_policy.origins) : ""),
    ...(s.web_policy ? row("웹 요청", "비로그인 읽기 요청: GET, HEAD, OPTIONS") : []),
    row("허용 기능", join(s.allowed_capabilities) || "제한 없음"),
    row("금지 기능", join(s.denied_capabilities)),
    row("금지 행동", join(s.denied_operations)),
    row("가용성 영향", s.availability_impact_allowed ? "허용" : "허용 안 함"),
    row("추가 제약", s.notes),
    row("중점 영역", join(s.focus_areas)),
    row("작업 / 도구 호출 / 시간(초)",
      [s.budget.max_actions, s.budget.max_tool_calls, s.budget.max_duration_seconds]
        .map((v) => (v == null ? "—" : v)).join(" / ")))];
  const changes = Object.entries(preview.scope_diff || {});
  if (changes.length) {
    parts.push(h("div", { class: "change" },
      h("strong", { text: "범위 변경" }),
      changes.map(([name, diff]) => h("p", {
        text: `${FIELD[name] || name}: 추가 ${join(diff.added) || "없음"} · 제거 ${join(diff.removed) || "없음"}`,
      }))));
  }
  if ((preview.unenforced_operations || []).length) {
    parts.push(h("p", {
      class: "note",
      text: `자동 집행되지 않고 기록만 되는 제약: ${preview.unenforced_operations.join(", ")}`,
    }));
  }
  return parts;
}
