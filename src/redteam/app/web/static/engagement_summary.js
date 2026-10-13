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
    ...(s.web_policy ? [
      ...row("웹 요청 권한", s.web_policy.permissions === "full" ? "대상 범위 내 전체 웹 요청 허용"
        : s.web_policy.permissions === "custom" ? "부분 허용" : "읽기 위주"),
      ...row("허용 메서드", s.web_policy.permissions === "full" ? "GET, HEAD, OPTIONS, POST, PUT, PATCH, DELETE"
        : join(s.web_policy.methods || ["GET", "HEAD", "OPTIONS"])),
      ...row("Query", s.web_policy.permissions === "full" || s.web_policy.allow_all_query_keys
        ? "모든 이름 허용" : join(s.web_policy.query_keys) || "차단"),
      ...row("요청 header", s.web_policy.permissions === "full" || s.web_policy.allow_all_request_headers
        ? "사용자 지정 header 허용 (보호된 header 제외)" : join(s.web_policy.request_headers) || "차단"),
      ...row("JSON·form 본문", s.web_policy.permissions === "full" || s.web_policy.request_body_enabled ? "허용" : "차단"),
      ...row("응답 본문 분석", s.web_policy.response_body_max_chars ? `마스킹 후 최대 ${s.web_policy.response_body_max_chars}글자` : "본문 제공 안 함"),
      ...row("테스트 계정", (s.web_policy.sessions || []).map((item) => `${item.session_ref} (${item.principal_ref})`).join(", ") || "없음"),
      ...row("Kali 대상 통신", s.web_policy.kali_network_mode === "configured" ? "검토된 대상 통신 정책 사용 — 실제 실행 준비는 Run 시작 시 확인"
        : s.web_policy.kali_network_mode === "closed" ? "차단" : "기존 실행 환경 설정 유지"),
      ...row("실행 승인", "현재 조사에 설정된 승인 규칙 적용"),
    ] : []),
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
