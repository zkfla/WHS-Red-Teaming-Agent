# live run 결과 — native 모델 + recon MCP

작성일: 2026-10-07. 담당: /root/coordination. 실행 2회. 제품 코드 수정 없음. 비밀·전체 프롬프트·원본 출력 본문 기록 없음.

- 1회차(수정 전): 종단 성공 아님 — mission blocked. 원인이 된 gate 거부를 기록.
- 2회차(evidence-driven 수정 후, account1): **종단 성공 — mission completed / goal_achieved.**

## 1. 공통 실행 조건

- 계정: account1(`~/.config/litellm/chatgpt`, 하네스가 프로세스 env로 선택; `.env` 미변경).
- 모델: `chatgpt/gpt-6-luna` (provider litellm). MCP: `recon-passive` 6 도구 등록.
- 진입: 제품 `run_engagement` + `AsyncExitStack`으로 MCP 게이트웨이 enter/exit(CLI와 동일; `build_application`은 코루틴이라 호출자가 stack 소유).
- 대상: 정책이 허용한 localhost(`http://localhost`, allowed_paths `["/"]`)로만 1회씩. 부작용 제한.
- targetless는 별도: profile `web_search_tavily`가 유일한 `requires_target=False`이고 `TAVILY_API_KEY` 부재(코드 부재 아님, 외부 provider 부재).

## 2. 1회차 (수정 전) — run_b4ae10c6e94b, blocked

- 사슬은 끝까지 돌았다: graph 시드 → 도구 4회(`dns_resolve`·`http_probe 200`·`html_inventory` 성공, `tls_inspect` `mcp_tool_error`) → 원본 4건 → 후속 턴 9회 → task.settle → report.
- 정산 실패: TaskCompleted 없음, mission `blocked`, run_outcome `unresolved`, termination_reason `mission_blocked`.
- 완료 게이트 거부 근거(코드 문자열):
  1. `evidence 'ev_ae2a1fccac46' records a failed original result and cannot support success` — 모델이 성공 근거로 실패 원본(tls_inspect)을 인용. 타당한 거부(gate 유지 대상).
  2. `evidence 'ev_b6a65f0cd9e9' targets a different object` — http_probe 원본의 기록 target은 `host:localhost`인데 검토 claim target은 `endpoint:localhost:80 GET /`였다. 완료 검토 LLM은 `accepted=true`였으나 코드 gate가 막았다.
- 이 2번이 root가 수정한 부분이다(recon_http mapper direct measured host→endpoint HOSTS relation snapshot + completion target mismatch grounding이 요청 target에 대해 기존 `claim_target_is_grounded`를 직접 사용).

## 3. 2회차 (수정 후) — run_1f1cc6fb1ad8, complete

목표(일반 authorized local HTTP observation, 고정 순서·강제 read 없음):

> Authorized local HTTP observation of the configured localhost target. Observe that local HTTP service with the registered tools that fit and preserve the original sources of what you observe. Settle the current task and then the mission from those stored originals once they support the outcome. Choose your own tool order; no separate read step is required.

원본 호출(저장된 tool 원본, has_tool_result=True):

- `recon.recon-passive.dns_resolve` success=true target=`host:localhost` evidence `ev_61d3de36e927` mapping=mapped
- `recon.recon-passive.http_probe` success=true target=`host:localhost` evidence `ev_92661bd3f6af` mapping=mapped (status 200)

이 회차는 `tls_inspect`를 시도하지 않아 tool_failed=false다. 1회차와 달리 성공 경로만 밟았다.

도달 결과:

- complete=`true`. checks 전부 통과: tool_executed, original_evidence_stored, followup_model_turn, task_completed_event, mission_updated_event, terminal_report_projected, run_finished.
- settlement: run_outcome `goal_achieved`, mission_status `completed`, task completed 1, verified_finding_count 0, unresolved_evidence_ids 없음.
- events: RunStarted/MissionCreated/TaskCreated/TaskStarted/AgentStarted 각 1, ActionProposed 2, PolicyEvaluated 2, ActionApproved 2, ToolExecutionStarted 2, ToolExecutionFinished 2, EvidenceCreated 2, ObservationRecorded 2, GraphUpdated 5, GraphMutationApplied 2, ReplanTriggered 4, AgentContextPrepared 2, AgentTurnDecided 2, TaskCompleted 1, MissionUpdated 1, ReportProjected 1, RunFinished 1. 총 증거 13건.
- graph: nodes 3(host 1, service 1, endpoint 1), edges 3, hypotheses 0, findings 0.
- work: succeeded 2, job-bound 0.
- final: phase finished, termination_reason `goal_achieved`, model_requests 6, tokens 0, usage unknown 6, actions_executed 2.
- 제품 reporter MD 판정: termination reason `goal_achieved`, ready_for_release True, success criterion `[MET]` (evidence: 2).

## 4. 산출물

2회차(성공):

- `data/react-loop-20261007/live-report-20261007T142931Z.json` (전체, terminal_report 포함)
- `data/react-loop-20261007/live-report-20261007T142931Z.normalized.json` (checks·checks_detail·calls·settlement·events)
- `data/react-loop-20261007/live-report-20261007T142931Z.md` (기존 reporter 렌더, markdown_source=project_reporter)
- `data/reports/run_1f1cc6fb1ad8.json`, `data/evidence/run_1f1cc6fb1ad8.jsonl`

1회차(blocked): `live-report-20261007T141348Z.json`/`.normalized.json`/`.md`, `data/reports/run_b4ae10c6e94b.json`.

## 5. 하네스 정정 (제품 아님)

- 이벤트 enum 값이 CamelCase라 checks 키를 양쪽 표기 허용으로 고쳤다(1회차 파생 정정).
- `TerminationReason`은 Enum이 아니라 문자열 상수 클래스다(`src/redteam/orchestration/state.py:46-54`). 하네스가 `getattr(reason, "value", "")`로 읽어 `final.termination_reason`이 빈 값이었다. 두 하네스를 `getattr(reason, "value", reason)`로 고치고, **안전한 정본 필드** `TerminationReportProjection.termination_reason`(`src/redteam/orchestration/reporting.py:396`)에서 저장 산출물의 값을 재파생했다(1회차 `mission_blocked`, 2회차 `goal_achieved`; 새 라이브 실행 없음). 제품 코드는 변경하지 않았다.
- 2회차 `calls` 배열은 필터가 넓어 processing start/done·task.settle/goal.settle 같은 보조 행까지 포함한다. 실제 tool 원본은 `has_tool_result=true`로 식별된다(`ev_61d3de36e927`, `ev_92661bd3f6af`). 하네스 필터를 `has_tool_result` 기준으로 좁혀 다음 실행부터는 보조 행을 제외한다. 저장된 2회차 산출물은 그대로 두었다(추가 재실행 없음).
- `build_application`은 contextmanager가 아니라 코루틴이며, MCP 게이트웨이는 CLI와 동일하게 `AsyncExitStack`으로 enter/exit해야 한다. 하네스가 그렇게 한다.

## 6. 남은 관찰·한계

- verified_finding_count 0은 정상이다. 이 목표는 취약점 판정이 아니라 로컬 HTTP 관찰·정산이다.
- 모델 usage tokens는 chatgpt 경로에서 unknown으로 보고된다(2회차 6건). 종단 판정에는 영향 없다.
- long job(`reconcile_job`)은 두 회차 모두 사용되지 않았다(job-bound 0). 별도 계획 문서 참조.
- targetless(web_search) 라이브는 Tavily provider 확보 전까지 보류.
