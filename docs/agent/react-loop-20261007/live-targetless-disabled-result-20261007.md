# targetless live 결과 — production web-search, external search disabled

작성일: 2026-10-07. 담당: /root/coordination. 실행 1회. 제품 코드 수정 없음. key/provider 대체 없음. 성공(양성) 주장 없음.

## 1. 가용성 판정

**가능하며 실행했다.** Tavily key 없이 실제 production stdio MCP가 targetless 도구를 등록하고, 모델이 query-only 호출을 시도했으며, 서버가 `external_search_disabled`로 정직하게 거부했고, 그 원본이 보존됐다. 이후 모델 후속 판단과 task.settle 미해결 정산, report 투영까지 이어졌다. 이것은 성공적 검색이 아니라 실제 targetless 오류·정산 사슬의 검증이다.

## 2. 소스 근거

- `tests/integration/test_web_search_mcp.py`: `test_actual_production_stdio_disabled_without_key`가 실제 production stdio 등록·invoke에서 `success=False`, `error.code == "mcp_tool_error"`, `"external_search_disabled" in output`을 단언한다. `test_search_config_is_pinned_and_disabled_needs_no_key`는 disabled config에서 `process_environment == {}`(key 불필요)를 확인한다.
- `src/redteam/capability/registration.py`의 `apply_web_search_profile`: `<PREFIX>_SEARCH_CONFIG`를 읽어 sha를 고정하고, stdio 인자(`-m redteam.capability.servers.web_search_mcp --config <path> --sha256 <sha>`)를 스스로 구성하며, disabled면 `process_environment={}`(key 미전달)로 둔다. profile이 kind/transport/risk/allowed_tools/operations를 검증한다.
- `src/redteam/capability/servers/web_search.py`: `SearchConfig.external_search_enabled` 기본 False, `SearchErrorCode.EXTERNAL_SEARCH_DISABLED`, `WebSearchReader.search`가 provider 호출 전에 disabled를 거부한다.
- `src/redteam/capability/servers/web_search_mcp.py`: `main(--config, --sha256)` → `load_web_search_config`(sha 일치 검증) → `create_server`. 도구는 `ToolError`로 거부한다.
- `src/redteam/core/scope_rules.py:354-361`: action.target이 None이고 선택 도구의 `requires_target is False`면 scope를 허용한다(targetless admission).

## 3. 등록 결과 (실제)

- descriptor: `web.web-search.web_search`, operations `["web_search"]`, target_types `[]`, **requires_target false**, health healthy.
- 등록 도구 수 1. engagement scope는 비어 있다(대상 도구 없음).
- 설정: `data/react-loop-20261007/targetless/search_disabled.json` = `{"external_search_enabled": false}`.
- env는 기존 규격 그대로: `REDTEAM_MCP_WEB_SEARCH_{KIND=web_api,TRANSPORT=stdio,COMMAND=<venv python>,RISK_LEVEL=low,PROFILE=web_search_tavily,SEARCH_CONFIG=<위 파일>}`. `.env` 미변경, 다른 산출물과 격리된 evidence/audit/approvals 디렉터리 사용.
- `tavily_key_present_in_process` = **False**. key 값은 어디에도 출력·저장하지 않았다.

## 4. 실행 결과 — run_e3e9fecb5ccb

거부 원본 2건(모델이 query 도구를 2회 시도):

- `ev_8911abf9c854`: success=false, error code `mcp_tool_error`, message/output `Error executing tool web_search: web_search: external_search_disabled`, work `work_695a88d33a87`
- `ev_6fa0f917c359`: 동일, work `work_49ad89b3d3d6`

이벤트: RunStarted/MissionCreated/TaskCreated/TaskStarted/AgentStarted 각 1, ActionProposed 4, PolicyEvaluated 2, ActionApproved 2, ActionDenied 2, ToolExecutionStarted 2, **ToolExecutionFailed 2**, EvidenceCreated 2, ObservationRecorded 2, GraphUpdated 4, ReplanTriggered 4, MissionUpdated 1, ReportProjected 1, RunFinished 1. TaskCompleted 없음.

정산/상태: run_outcome `unresolved`, mission_status `blocked`, task blocked 1, completed 0, verified findings 0, unresolved_evidence `evidence_8376f776b733`. graph nodes 0(대상 없음), 원장은 failed 2/stale 2.
final: phase finished, **termination_reason `stalled_no_graph_progress`**, model_requests 4, usage unknown 4, actions_executed 2.

checks(기록된 event/증거에서 파생, 재실행 없음):

- targetless_tool_registered true, query_tool_call_attempted true, **query_refused_honestly true**, original_evidence_stored true, followup_model_turn true, task_completed_event false, mission_updated_event true, terminal_report_projected true, run_finished true.
- `complete` = false. 정직한 거부이므로 완료로 보고하지 않는다.

## 5. 산출물

- `data/react-loop-20261007/targetless/live-targetless-20261007T160257Z.json` (전체)
- `...normalized.json` (checks·checks_detail·calls·settlement·events)
- `...md` (기존 reporter 렌더, markdown_source=project_reporter)
- 격리 원본: `data/react-loop-20261007/targetless/evidence/run_e3e9fecb5ccb.jsonl`, `data/reports/run_e3e9fecb5ccb.json`
- 하네스: `data/react-loop-20261007/live_targetless_harness.py`, 설정 `targetless/engagement.yaml`·`search_disabled.json`

## 6. 하네스 정정

- canonical 도구 id는 서버 id를 포함한다(`web.web-search.web_search`). 첫 파생 checks가 `web.web_search` 상수 비교로 `query_refused_honestly`를 false로 표기했다. 필터를 web 네임스페이스 prefix 기준으로 고치고, 기록된 event/증거에서 checks를 재파생했다(추가 모델 실행 없음). 거부 사실 자체는 원본 message로 확인된다.
- `TerminationReason`은 Enum이 아니라 문자열 상수 클래스다(`src/redteam/orchestration/state.py:46-54`). 하네스가 `getattr(reason, "value", "")`로 읽어 `final.termination_reason`이 빈 값으로 정규화됐다. 두 하네스(`live_harness.py`, `live_targetless_harness.py`)를 `getattr(reason, "value", reason)`로 고쳤다.
- **안전한 정본 필드**: 제품 투영 `TerminationReportProjection.termination_reason`(`src/redteam/orchestration/reporting.py:396`; `runtime.termination_reason`을 그대로 전달, `reporting.py:541`). targetless `final.termination_reason`을 이 필드에서 재파생했다(`stalled_no_graph_progress`). 산출물에 provenance 표시를 남겼고 새 라이브 실행은 하지 않았다. 제품 코드는 변경하지 않았다.

## 7. 한계

- 이것은 외부 provider를 대체한 성공 검증이 아니다. Tavily key 부재는 그대로이며, 활성 검색(양성 결과)은 여전히 미검증이다.
- model usage tokens는 chatgpt 경로에서 unknown(4건)으로 보고된다.
- long job 경로는 사용되지 않았다(job-bound 0).
