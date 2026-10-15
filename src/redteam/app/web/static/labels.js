export const PHASE = {
  idle: "입력 대기",
  starting: "시작 중",
  running: "조사 중",
  waiting_approval: "승인 대기",
  paused: "일시 중단",
  settling: "결과 정리 중",
  closing: "종료 중",
  closed: "종료됨",
};

export const HOST = {
  needs_engagement: "조사 선택 필요",
  needs_settings: "설정 필요",
  starting: "시작 중",
  ready: "준비됨",
  stopped: "세션 종료됨",
  error: "오류",
  closed: "서버 종료됨",
};

export const PLANNER = { deciding: "판단 중", waiting: "대기", held: "보류", exhausted: "탐색 중단", finished: "종료" };

export const TASK = {
  pending: "대기", ready: "준비됨", running: "진행 중", blocked: "막힘",
  completed: "완료", failed: "실패", cancelled: "취소",
};

// Glyph + word: a status is never carried by color alone.
export const GLYPH = {
  deciding: "◐", waiting: "◌", finished: "■", pending: "·", ready: "○", running: "▶",
  blocked: "!", completed: "✓", failed: "✕", cancelled: "✕", unknown: "?",
};

export const WORKER = {
  model: "모델 판단 중",
  tool: "도구 실행 중",
  waiting_approval: "승인 대기",
  waiting_job: "작업 완료 대기",
  waiting_resource: "자원 대기",
  waiting_review: "결과 확인·재개 대기",
  outcome_unknown: "결과 불명 — 확인 필요",
  returned: "슬롯 반납",
  reserved: "시작 준비",
  between: "판단·도구 사이(정리 포함)",
};

export const WORKER_GLYPH = {
  model: "◐", tool: "▶", waiting_approval: "!", waiting_job: "…", waiting_resource: "…",
  waiting_review: "!", outcome_unknown: "?", returned: "◌", reserved: "○", between: "·",
};

export const GRAPH_UNAVAILABLE = {
  no_run_yet: "아직 시작된 조사가 없습니다.",
  state_unconfigured: "이 세션의 저장 위치를 알 수 없어 실행 상태를 읽지 못합니다.",
  state_unreadable: "저장된 실행 상태를 읽을 수 없습니다.",
  central_original_run_missing: "이 조사의 저장된 상태를 찾지 못했습니다.",
  central_inspection_schema_unsupported: "저장 형식이 이 화면과 맞지 않습니다.",
};

export const UNKNOWN = "알 수 없음";

export const WAIT = { approval: "승인", job: "작업 완료", resource: "자원" };

export const RECEIPT = {
  queued: "전달 대기",
  classifying: "분류 중",
  dispatched: "전달됨",
  handled: "처리됨",
  deferred: "보류 — 현재 Run 정리 후 시작",
  awaiting_clarification: "확인 필요",
  not_applied: "적용되지 않음",
  unknown: "처리 여부 확인 필요",
};

export const VERDICT = {
  ALLOW: "허용",
  DENY: "차단",
  REQUIRE_APPROVAL: "승인 필요",
};

export const FIELD = {
  provider: "LLM 제공자",
  model: "LLM 모델",
  reasoning_effort: "추론 강도 (effort)",
  api_key: "API 키",
  profile_id: "에이전트 프로필",
  llm_request_policy: "LLM 요청 한도",
  max_requests: "LLM 요청 상한",
  max_concurrent_requests: "동시 요청 수",
  max_retries: "자동 재시도 횟수",
  persist_key: "키 저장",
  storage: "저장소",
  settings: "설정",
  body: "요청",
  draft: "입력",
  name: "작업 이름",
  description: "작업 설명",
  objective: "작업 목표",
  allowed_hosts: "허용 호스트",
  allowed_domains: "허용 도메인",
  allowed_networks: "허용 네트워크",
  web_policy_mode: "웹 탐색 범위",
  web_paths: "지정 경로",
  kali_network_mode: "Kali 대상 통신",
  kali_network_policy_path: "Kali 네트워크 정책",
  kali_endpoints: "Kali 대상 포트",
  kali_limits_preset: "Kali 네트워크 예산",
  kali_limit_deliveries: "최대 패킷 수",
  kali_limit_output_bytes: "최대 출력 바이트",
  kali_limit_duration_seconds: "최대 실행 시간",
  kali_limit_delivery_rate_per_second: "초당 최대 패킷 수",
  kali_sidecar_mode: "네트워크 sidecar",
  kali_sidecar_image: "고정 sidecar 이미지",
  kali_docker_context: "Docker context",
  allowed_capabilities: "허용 기능",
  denied_capabilities: "금지 기능",
  denied_operations: "금지 행동",
  availability_impact_allowed: "가용성 영향 허용",
  notes: "추가 제약",
  focus_areas: "중점 영역",
  max_actions: "최대 작업 횟수",
  max_tool_calls: "최대 도구 호출 횟수",
  max_duration_seconds: "최대 실행 시간",
  scope: "범위",
  roe: "교전 규칙",
};

export const CODE = {
  missing: "입력이 필요합니다",
  invalid: "형식이 올바르지 않습니다",
  invalid_type: "값의 종류가 올바르지 않습니다",
  invalid_choice: "목록에서 선택하세요",
  web_policy_invalid: "대상 URL·경로·HTTP 메서드·query·header·인증 참조·Kali 정책을 확인하세요",
  invalid_endpoint_selection: "Kali 대상과 포트 선택을 확인하세요",
  kali_endpoint_origin_invalid: "선택한 사이트에 연결된 유효한 URL을 지정하세요",
  kali_endpoint_selection_incomplete: "각 선택 대상에 포트와 전송 방식을 하나씩 지정하세요",
  kali_endpoint_origin_not_selected: "선택된 대상의 포트만 지정할 수 있습니다",
  kali_endpoint_duplicate: "같은 대상·포트·전송 방식은 한 번만 지정할 수 있습니다",
  kali_endpoint_too_many: "대상별 Kali 포트 선택 수가 한도를 넘었습니다",
  kali_endpoint_outside_scope: "선택 포트가 Engagement의 허용 범위에 없습니다",
  kali_limits_selection_conflict: "예산 preset 또는 직접 입력 중 하나만 선택하세요",
  kali_custom_limits_required: "직접 입력 예산의 모든 필수 값을 지정하세요",
  kali_custom_limits_invalid: "유한한 Kali 예산의 범위와 값을 확인하세요",
  kali_custom_limits_not_selected: "직접 입력을 선택한 경우에만 예산 값을 입력할 수 있습니다",
  kali_limits_selection_required: "Kali 네트워크 예산 preset 또는 직접 입력을 선택하세요",
  kali_sidecar_selection_required: "managed 또는 고정 sidecar 방식을 선택하세요",
  kali_sidecar_image_required: "이미 설치된 sidecar의 고정 이미지를 지정하세요",
  kali_sidecar_image_unpinned: "sidecar 이미지는 sha256 digest로 고정해야 합니다",
  kali_sidecar_image_not_selected: "고정 이미지 방식에서만 sidecar 이미지를 지정할 수 있습니다",
  kali_docker_context_invalid: "Docker context 이름을 확인하세요",
  kali_configured_policy_conflict: "새 준비 방식에서는 기존 Kali 정책 파일을 지정하지 마세요",
  kali_prepare_fields_not_selected: "준비 전용 값은 Kali 준비 모드에서만 지정할 수 있습니다",
  kali_network_policy_not_selected: "Kali 정책 파일을 사용하려면 검토된 정책 모드를 선택하세요",
  automatic_policy_required: "요청 권한과 인증 계정을 지정하려면 정책 자동 생성을 선택하세요",
  invalid_session_reference: "계정의 이름·대상·경로·인증 참조를 확인하세요. 토큰 값은 입력하지 마세요",
  out_of_range: "허용 범위를 벗어났습니다",
  unknown_profile: "목록에 없는 프로필입니다",
  unknown_field: "알 수 없는 항목입니다",
  unsupported: "지원하지 않는 제공자입니다",
  removed: "제거된 설정입니다. .env에서 REDTEAM_ORCHESTRATION_MAX_RETRIES를 삭제하고 서버를 다시 시작하세요",
  requires_api_key: "API 키를 입력해야 저장할 수 있습니다",
  write_failed: "저장하지 못했습니다",
  scope_required: "허용 호스트, 도메인, 네트워크 중 하나 이상이 필요합니다",
  invalid_address: "대상 URL 또는 호스트 주소 형식이 올바르지 않습니다",
  unknown_capability: "목록에 있는 기능만 선택할 수 있습니다",
  too_long: "입력이 너무 깁니다",
  too_many: "항목이 너무 많습니다",
  not_stated_by_operator: "입력하지 않은 범위라 제외했습니다",
};

export const ERROR = {
  session_not_ready: "세션이 아직 준비되지 않았습니다. 설정을 완료하세요.",
  input_not_accepted: "입력을 받을 수 없습니다(대기열이 가득 찼거나 종료 중입니다).",
  approval_changed: "승인 대상이 바뀌었습니다. 화면을 새로 확인하세요.",
  run_mismatch: "승인 대상 Run이 현재 세션과 다릅니다.",
  run_active: "진행 중인 조사가 끝난 뒤에 변경할 수 있습니다.",
  session_starting: "세션을 시작하는 중입니다. 잠시 후 다시 시도하세요.",
  text_too_long: "입력이 너무 깁니다(12000자 이하).",
  network: "서버에 연결할 수 없습니다.",
  unknown_engagement: "목록에 없는 Engagement입니다.",
  engagement_not_ready: "이 Engagement는 규칙 검사를 통과하지 못해 선택할 수 없습니다.",
  settings_required: "LLM 설정이 필요합니다. 왼쪽의 '설정'에서 모델과 API 키를 입력하세요.",
  session_expired: "대화가 만료되었습니다. 새로 시작하세요.",
  message_required: "메시지를 입력하세요.",
  message_too_long: "메시지가 너무 깁니다(2000자 이하).",
  turn_limit: "대화 횟수 한도에 도달했습니다. 지금까지의 초안으로 검토하거나 새로 시작하세요.",
  assistant_failed: "어시스턴트 응답을 받지 못했습니다. 다시 시도하거나 직접 입력하세요.",
  not_managed: "이 파일은 직접 작성된 규칙을 포함해 화면에서 수정할 수 없습니다. 파일을 직접 편집하세요.",
  unreadable: "Engagement 파일을 읽을 수 없습니다.",
  write_failed: "Engagement 파일을 저장하지 못했습니다.",
};

export function describeError(error) {
  const body = error.body || {};
  if (body.error === "invalid_engagement") {
    const list = (body.problems || []).slice(0, 6).map((p) => `${FIELD[p.field] || p.field}: ${CODE[p.code] || p.message || p.code}`);
    return list.join(" / ") || "입력값을 확인하세요.";
  }
  if (body.error === "confirmation_required") return "범위 확인에 체크해야 저장할 수 있습니다.";
  if (body.error === "invalid_settings") {
    return `${FIELD[body.field] || body.field}: ${CODE[body.code] || body.code}`;
  }
  const detail = body.detail ? ` [${body.detail}]` : "";
  if (ERROR[body.error]) return ERROR[body.error] + detail;
  if (!body.error && error.status === 404) {
    return "서버가 이 요청을 모릅니다(HTTP 404). redteam web을 최신 코드로 다시 시작했는지 확인하세요.";
  }
  return `요청을 처리하지 못했습니다(${body.error || `HTTP ${error.status}`})${detail}.`;
}
