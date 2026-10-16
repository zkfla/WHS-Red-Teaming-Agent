// Right-hand detail for the selected node. Only stored or published-summary fields appear.
import { h, replace } from "./dom.js";
import { PLANNER, TASK, UNKNOWN, WAIT, WORKER } from "./labels.js";
import { badge } from "./ui.js";

const rows = (pairs) => h("dl", { class: "gdl" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v === null || v === undefined || v === "" ? UNKNOWN : String(v) })]));

const RESPONSE = {
  not_sent: "아직 전송되지 않음", cancelled: "전송 전 취소", waiting: "응답 대기",
  received: "응답 수신", failed: "모델 제공자 오류", unknown: "응답 결과 미확정",
};

const PROVIDER = {
  server: "모델 제공자 서버 오류", auth: "모델 제공자 인증 오류",
  rate_limited: "모델 제공자 요청 제한", invalid_request: "모델 요청 거절",
  timeout: "모델 제공자 시간 초과", network: "모델 제공자 네트워크 오류",
  unknown: "모델 제공자 오류 — 분류 불명", success: "정상 응답",
};

const REQUEST = {
  reserved: "예약됨", dispatched: "전송됨", settled: "정산됨",
  unknown: "결과 불명", released: "예약 해제",
};

const yesNo = (value) => value === true ? "예" : value === false ? "아니요" : UNKNOWN;

function responseLabel(item) {
  if (item.response_state === "failed") {
    return PROVIDER[item.provider_outcome] || RESPONSE.failed;
  }
  return RESPONSE[item.response_state] || item.response_state || UNKNOWN;
}

function failureBreakdown(model) {
  const parts = Object.entries(model.failure_breakdown || {})
    .filter(([, count]) => count)
    .map(([kind, count]) => `${PROVIDER[kind] || kind} ${count}건`);
  return parts.join(" · ") || "없음";
}

function tokenUsage(usage) {
  const names = [["input_tokens", "입력"], ["output_tokens", "출력"],
    ["cache_read_tokens", "캐시 읽기"], ["cache_write_tokens", "캐시 쓰기"]];
  return names.map(([key, label]) => {
    const count = usage?.usage?.[key];
    return [label, count ? `확인 ${count.known} 토큰 · 미보고 ${count.unknown}건` : UNKNOWN];
  });
}

function timeline(view, taskId) {
  const events = view.events.filter((e) => !taskId || e.data.mission_task_id === taskId || e.data.task_id === taskId).slice(0, 12).reverse();
  if (!events.length) return h("p", { class: "note", text: "표시할 기록이 없습니다." });
  return h("ol", { class: "gtimeline" }, events.map((e) => {
    const bits = ["operation", "tool_id", "verdict", "evidence_id"].map((k) => e.data[k]).filter(Boolean).join(" · ");
    return h("li", {}, h("span", { class: "mono", text: e.type }), bits ? ` — ${bits}` : "");
  }));
}

function planner(view) {
  const p = view.planner;
  return [badge(PLANNER[p.state] || UNKNOWN), h("h3", { text: "Planner" }),
    p.inputs.goal ? h("p", { class: "gdetail-summary", text: p.inputs.goal }) : null, rows([
    ["상태", PLANNER[p.state]], ["판단 단계", p.stage], ["깨운 이벤트", p.trigger],
    ["목표", p.inputs.goal], ["후보 수", p.inputs.candidates], ["증거 수", p.inputs.evidence],
    ["검증 Task 수", p.inputs.tasks],
    ["미해결 질문 수", p.inputs.open_questions],
    ["직전 판단", p.last_judgment && p.last_judgment.termination],
    ["직전 판단 사유", p.last_judgment && p.last_judgment.reason],
    ["등록한 Task(누적)", p.tasks_registered],
    ["모델 요청", `${p.usage.requests}건 · 진행 ${p.usage.in_flight} · 확인된 토큰 ${p.usage.known_tokens} · 사용량 미확인 ${p.usage.unknown_usage}건`],
    ...tokenUsage(p.usage),
    ["단계별 요청", Object.entries(view.usage.by_stage || {}).map(([stage, usage]) => `${stage}: ${usage.spent}건`).join(" · ") || UNKNOWN],
    ["운영자 안내", p.guidance_in_input === true ? "플래너 입력에 포함" :
      p.guidance_in_input === false ? "플래너 판단 입력에 포함되지 않음" : UNKNOWN],
  ]), h("p", { class: "note", text: "저장되지 않는 값은 '알 수 없음'으로 표시합니다." })];
}

function task(view, id) {
  const t = view.tasks.find((x) => x.task_id === id);
  if (!t) return [h("p", { class: "note", text: "이 Task는 더 이상 목록에 없습니다." })];
  return [badge(t.wait ? `${WAIT[t.wait.reason] || t.wait.reason} 대기` : TASK[t.status] || UNKNOWN, t.wait ? "wait" : ""),
    h("h3", { text: t.title }), h("p", { class: "gdetail-id", text: t.task_id }),
    h("div", { class: "gdetail-summary" }, h("strong", { text: "완료 조건" }),
      h("p", { class: "note", text: t.completion.join(" / ") })), rows([
    ["상태", TASK[t.status]], ["담당 역할", t.role], ["현재 워커", t.worker || "없음"],
    ["후속 검증 출처", t.source_task_id || "없음"],
    ["출처 결과", t.source_result_id || "없음"],
    ["막힌 사유", t.blocking_reason || "없음"],
    ["대기", t.wait ? `${WAIT[t.wait.reason] || t.wait.reason}${t.wait.approval_ref ? ` · 승인 ${t.wait.approval_ref}` : ""}` : "없음"],
    ["증거 수", t.evidence], ["결과", t.has_result ? "저장됨" : "아직 없음"],
    ["결과 판정", t.result_outcome],
    ["모델 요청", `${t.usage.requests}건 · 확인 토큰 ${t.usage.known_tokens} · 미보고 ${t.usage.unknown_usage}건`],
    ["외부 행동 / 로컬 조회", `${t.usage.external_actions} / ${t.usage.local_reads}`],
    ["repair / transport retry", `${t.usage.repairs} / ${t.usage.transport_retries}`],
    ["provider 실패 / settlement", `${t.usage.provider_failures} / ${t.usage.settlements}`],
    ["외부 행동/모델 요청", t.usage.external_actions_per_model_request === null ? UNKNOWN : t.usage.external_actions_per_model_request.toFixed(3)],
    ...tokenUsage(t.usage),
  ]), h("h4", { text: "기록" }), timeline(view, id)];
}

function candidate(view, id) {
  const c = (view.candidates || []).find((item) => item.hypothesis_id === id);
  if (!c) return [h("p", { class: "note", text: "이 후보는 더 이상 목록에 없습니다." })];
  return [badge(c.status), h("h3", { text: c.description }),
    h("p", { class: "gdetail-id", text: c.hypothesis_id }), rows([
      ["현재 판단", c.status], ["개정", c.revision], ["신뢰도", c.confidence],
      ["판단 사유", c.reason], ["대상", c.target_node_ids.join(", ")],
      ["검증 작업", c.task_ids.join(", ") || "없음"],
      ["지지 증거", c.supporting_evidence_ids.join(", ") || "없음"],
      ["반대 증거", c.refuting_evidence_ids.join(", ") || "없음"],
      ["출처 후보", c.source_hypothesis_ids.join(", ") || "없음"],
      ["Finding 승격", c.finding_promotion?.status],
      ["승격 거절 사유", c.finding_promotion?.reason],
      ["승격 Finding", c.finding_promotion?.finding_id],
    ]), h("p", { class: "note", text: "후보 판단은 Task 완료, Finding, Mission 판정과 별개입니다." })];
}

function execution(item) {
  const program = item.executed_program || item.selected_program || UNKNOWN;
  const programPath = item.executed_program_path || item.program_executable || UNKNOWN;
  const kind = {
    generic_program_output: "일반 stdout 관측",
    canonical_protocol_receipt: "canonical protocol receipt",
    tool_result: "일반 도구 결과",
    provider_failure: "provider/tool 실패",
    not_executed: "실행 전 대기/거절",
    unknown: "실행 결과 미확정",
  }[item.result_kind] || item.result_kind || UNKNOWN;
  return h("details", { class: "gexchange", "data-key": item.invocation_id },
    h("summary", {}, h("span", { class: "gexchange-title", text: `${item.tool_id} · ${item.state}` }),
      h("span", { class: "gexchange-state", text: kind })),
    h("div", { class: "gexchange-body" }, rows([
      ["Invocation", item.invocation_id], ["선택 route", item.route],
      ["Route readiness", `${item.route_status || UNKNOWN} · ${item.route_reason_code || UNKNOWN}`],
      ["선택/실행 program", program], ["Program 경로", programPath],
      ["실제 실행 상태", item.state], ["결과 종류", kind],
      ["원본 증거", item.evidence_id], ["진단 분류", item.diagnostic_category],
      ["진단 코드", item.diagnostic_code], ["원인 범위", item.cause_scope],
      ["재개 조건", item.resume_condition],
    ]), h("p", { class: "note", text: "stdout 관측, protocol receipt, 실행 성공과 취약점 후보/Finding 판정은 서로 다른 상태입니다." })));
}

function worker(view, id) {
  const w = view.workers.find((x) => x.instance_id === id);
  if (!w) return [h("p", { class: "note", text: "이 워커는 더 이상 목록에 없습니다." })];
  const assigned = view.tasks.find((item) => item.task_id === w.task_id);
  const executions = (w.executions || []).map(execution);
  return [badge(WORKER[w.phase] || UNKNOWN, w.phase.startsWith("waiting_") ? "wait" : ""),
    h("h3", { text: w.instance_id }),
    assigned ? h("p", { class: "gdetail-summary", text: assigned.title }) : null, rows([
    ["단계", WORKER[w.phase]], ["맡은 Task", w.task_id], ["모델 요청", w.model_requests], ["도구 호출", w.tool_calls],
    ["호출 중인 도구", w.current_tool || "없음"],
    ["대기", w.wait ? `${WAIT[w.wait.reason] || w.wait.reason}${w.wait.approval_ref ? ` · 승인 ${w.wait.approval_ref}` : ""}` : "없음"],
    ["슬롯", w.slot_released ? "반납됨(요청은 보존)" : "사용 중 또는 해당 없음"],
    ["저장된 증거", w.evidence_refs.join(", ") || "없음"],
    ["외부 판단 / 로컬 조회", `${w.usage.external_tool_decisions} / ${w.usage.local_reads}`],
    ["repair / transport retry", `${w.usage.repairs} / ${w.usage.transport_retries}`],
    ["provider 실패 / settlement", `${w.usage.provider_failures} / ${w.usage.settlements}`],
    ...tokenUsage(w.usage),
    ["실제 요금", "미측정"],
  ]), h("h4", { text: "Kali · 도구 실행" }),
    executions.length ? h("div", { class: "gexchange-list" }, executions) : h("p", { class: "note", text: "저장된 실행이 없습니다." }),
    h("h4", { text: "이 Task의 기록" }), timeline(view, w.task_id)];
}

function exchange(item) {
  const title = `#${item.sequence || "?"} · ${item.stage || item.purpose || item.kind || "단계 미기록"}`;
  const response = responseLabel(item);
  const token = item.tokens?.total === null || item.tokens?.total === undefined ? "토큰 미보고" : `${item.tokens.total} 토큰`;
  const providerStatus = item.provider_status === null || item.provider_status === undefined
    ? (item.response_state === "failed" ? "미기록 — 과거 Run 또는 provider 미제공" : UNKNOWN)
    : `HTTP ${item.provider_status}`;
  const retryAfter = item.provider_retry_after === null || item.provider_retry_after === undefined
    ? UNKNOWN : `${item.provider_retry_after}초`;
  const content = (title, value, truncated, missing) => h("section", { class: "gexchange-content" },
    h("h5", { text: title }),
    value ? h("pre", { text: value }) : h("p", { class: "note", text: missing }),
    truncated ? h("p", { class: "note", text: "저장 길이 제한으로 이후 내용이 생략되었습니다." }) : null);
  return h("details", { class: `gexchange ${item.response_state || "unknown"}`, "data-key": item.request_id || title },
    h("summary", {}, h("span", { class: "gexchange-title", text: title }),
      h("span", { class: "gexchange-state", text: response })),
    h("div", { class: "gexchange-body" },
      h("h5", { text: "LLM 요청" }), rows([
        ["요청 ID", item.request_id], ["호출 주체", item.owner], ["연결 노드", item.actor],
        ["Task", item.task_id], ["단계", item.stage], ["호출 종류", item.kind],
        ["목적", item.purpose], ["시도 종류", item.attempt_kind],
        ["요청 상태", REQUEST[item.request_state] || item.request_state],
      ]),
      content("전송된 프롬프트", item.request_content, item.request_truncated,
        "이 Run에는 프롬프트 본문이 저장되지 않았습니다. 변경 이전 Run일 수 있습니다."),
      h("h5", { text: "LLM 응답" }), rows([
        ["수신 상태", response], ["제공자 분류", PROVIDER[item.provider_outcome] || item.provider_outcome],
        ["HTTP 상태", providerStatus], ["Provider code", item.provider_code],
        ["재시도 가능", yesNo(item.provider_retryable)], ["Retry-After", retryAfter],
        ["판단 결과", item.decision_outcome], ["유효한 판단", yesNo(item.decision_valid)],
        ["실제 진행 발생", yesNo(item.made_progress)], ["전체 토큰", token],
        ["입력 / 출력 토큰", `${item.tokens?.input ?? UNKNOWN} / ${item.tokens?.output ?? UNKNOWN}`],
        ["캐시 읽기 / 쓰기", `${item.tokens?.cache_read ?? UNKNOWN} / ${item.tokens?.cache_write ?? UNKNOWN}`],
      ]),
      content("수신한 공개 응답", item.response_content, item.response_truncated,
        item.response_state === "failed" ? "모델 제공자 오류로 공개 응답 본문을 받지 못했습니다." :
          "이 Run에는 응답 본문이 저장되지 않았습니다. 변경 이전 Run일 수 있습니다.")));
}

function llm(view) {
  const model = view.llm || { exchanges: [] };
  const status = model.waiting ? `응답 대기 ${model.waiting}건` :
    model.failures || model.unknown ? "확인 필요" : "응답 처리됨";
  const history = (model.exchanges || []).map(exchange);
  return [badge(status, model.waiting ? "wait" : model.failures || model.unknown ? "wait" : ""),
    h("h3", { text: "LLM 요청 · 응답" }),
    h("p", { class: "gdetail-summary", text: `제공자 전송 ${model.provider_requests || 0}건 · 응답 ${model.responses || 0}건 · 실패 ${model.failures || 0}건 · 결과 불명 ${model.unknown || 0}건` }),
    rows([
      ["전체 예약", model.reservations || 0], ["응답 대기", model.waiting || 0],
      ["실패 분류", failureBreakdown(model)], ["응답 결과 미확정", model.unknown || 0],
      ["전송 전 취소", model.cancelled || 0], ["화면 표시", `${model.shown || 0}건${model.truncated ? " (최근 기록만)" : ""}`],
    ]),
    h("p", { class: "note", text: "프롬프트와 모델의 공개 응답은 민감정보를 마스킹하고 길이를 제한해 표시합니다. 내부 추론과 provider continuation은 저장하지 않습니다. 변경 이전 Run에는 본문이 없을 수 있습니다." }),
    h("h4", { text: "최근 요청과 응답" }),
    history.length ? h("div", { class: "gexchange-list" }, history) : h("p", { class: "note", text: "기록된 LLM 요청이 없습니다." })];
}

function origin(view) {
  const o = view.origin;
  return [h("h3", { text: "증거와 발견사항" }),
    h("p", { class: "gdetail-summary", text: `증거 ${o.evidence}건 · 발견사항 ${o.findings}건 · 검증 ${o.verified_findings}건` }), rows([
    ["증거 참조", o.evidence], ["그래프 변경", o.graph_updates], ["Finding", o.findings],
    ["검증된 Finding", o.verified_findings], ["가설", o.hypotheses],
    ["남은 액션 예산", view.usage.budget_remaining],
  ])];
}

export function renderDetail(root, view, selected) {
  let body;
  if (!view || !view.available) body = [h("p", { class: "note", text: "상세를 볼 수 없습니다." })];
  else if (selected === "planner") body = planner(view);
  else if (selected === "llm") body = llm(view);
  else if (selected === "origin") body = origin(view);
  else if (selected && selected.startsWith("candidate:")) body = candidate(view, selected.slice(10));
  else if (selected && selected.startsWith("task:")) body = task(view, selected.slice(5));
  else if (selected && selected.startsWith("worker:")) body = worker(view, selected.slice(7));
  else body = [h("p", { class: "note", text: "노드를 선택하면 상세가 여기에 표시됩니다." })];
  replace(root, body);
}
