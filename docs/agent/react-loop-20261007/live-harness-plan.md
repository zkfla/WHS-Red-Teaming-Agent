# live harness 계획 — native 모델 + recon MCP 종단 실행

작성일: 2026-10-07. 담당: /root/coordination. 상태: 하네스 준비 완료. **실행하지 않음 — root ready 신호 대기.**

## 1. 목적

실제 모델(account1)과 실제 등록 MCP 도구(recon-passive)로 native ReAct 루프를 한 번 끝까지 돌려, graph 시드 → 실제 도구 실행 → 원본 증거 저장 → 후속 모델 판단 → task/mission 정산 → report 도달을 확인한다. tool-only 통과로 축소하지 않는다.

## 2. 산출물 (제품 코드 아님, `/data/`는 gitignored)

- `data/react-loop-20261007/live_harness.py` — 실행 하네스. 제품 진입점 `run_engagement`를 사용한다(직접 gateway 호출 아님).
- `data/react-loop-20261007/live_engagement.yaml` — 최소 engagement. `scope.allowed_hosts=["localhost"]`, `metadata.target_refs={localhost: localhost}`, `approval_policy.auto_approve=true`, 예산 actions 6.
- `data/react-loop-20261007/live_www/index.html` — 로컬 정적 대상(하네스가 없으면 생성).
- 실행 시 생성: `live-report-<UTC>.json`(정규화 요약 + event id), `live-report-<UTC>.md`.

## 3. 실행 방법

저장소 루트에서 venv 인터프리터로 실행한다.

```
.venv/Scripts/python.exe data/react-loop-20261007/live_harness.py
```

- 모델 계정: 하네스가 `.env` 로드 **전에** `CHATGPT_TOKEN_DIR`를 account1(`~/.config/litellm/chatgpt`)로 설정한다. `REDTEAM_LIVE_TOKEN_DIR`로 덮어쓸 수 있다.
- `.env` 파일은 수정하지 않는다(`load_dotenv(override=False)`).
- 모델·MCP 설정은 기존 `REDTEAM_LLM_*` / `REDTEAM_MCP_SERVERS`(recon-passive)를 그대로 쓴다.
- 부작용 제한: `127.0.0.1:80` 임시 정적 서버 + 정책이 허용한 localhost 대상만. 포트가 점유되어 있으면 기존 리스너를 쓴다.

## 4. 보고 — 기존 프로젝트 reporter 사용

- Markdown은 제품의 기존 reporter 렌더러(`redteam.app.cli.commands.report._render_markdown`)로 `TerminalReportProjection`을 렌더한다. `run_engagement`가 종료 시 `app.report_store.save_terminal_report`로 `data/reports/<run_id>.json`을 이미 저장하므로, 하네스는 `load_terminal_report`로 읽어 그대로 렌더한다. terminal report가 없으면(비종료 run) 지어내지 않고 정규화 요약으로 대체하고 그 사실을 기록한다.
- JSON은 정규화 요약 + event id 목록이며 `terminal_report` 투영을 포함한다.

## 5. 기록 원칙

- 저장 금지: secrets(토큰/키 값), 전체 프롬프트, 도구 원본 출력 본문.
- 저장: event id와 종류, event 종류별 개수, 증거 id·source_type·metadata 키 목록·mapping_status·redacted 요약, 그래프 노드/엣지/가설/finding 개수, 원장 상태 분포·job 결합 개수, 최종 phase/termination/카운터, 기존 reporter 투영.
- 자유 텍스트는 기존 `redact_secret_material`로 마스킹한다.

## 6. 종단 관측 지점 (report checks)

- `tool_executed`: TOOL_EXECUTION_FINISHED 발생.
- `original_evidence_stored`: metadata에 tool_result가 있는 증거 존재.
- `followup_model_turn`: 모델 요청 수 > 1.
- `task_settled` / `mission_settled`: TASK_COMPLETED / MISSION_UPDATED 발생.
- `terminal_report_projected`: 기존 reporter 투영 존재.
- `run_finished`: RUN_FINISHED 발생.

## 7. targetless 경로 — 별도 표기

targetless는 코드 부재가 아니라 **외부 provider 자격증명 부재**로 보류된다. report의 `targetless` 항목에 따로 기록한다.

- profile: `web_search_tavily` (저장소에서 `requires_target=False`를 선언하는 유일한 profile).
- missing_external_provider: Tavily(`TAVILY_API_KEY`가 `.env`와 프로세스 env에 없음).
- live_ready: false.

## 8. 선행 조건과 현재 상태

- `src/redteam/orchestration/nodes/verify.py` 구문 오류는 해소됐다(`ast.parse` 통과).
- 하네스 컴파일 통과, `live_engagement.yaml` 기존 로더로 정상 파싱, 기존 reporter 렌더러 import 확인.
- **실행 보류.** root가 최종 테스트 준비를 알릴 때까지 돌리지 않는다. root가 지정한 대상 파일만 수정 중이며, 반쯤 편집된 트리에서 실제 모델을 호출하면 결과가 오해를 만든다.

## 9. 실행 절차 (root ready 신호 후)

1. root의 ready 신호를 받는다.
2. `live_harness.py`를 1회 실행한다(account1).
3. checks로 종단 도달을 판정한다. 실패 시 어느 check에서 멈췄는지와 provider 응답 종류/상태만 기록한다.
4. targetless는 Tavily key가 확보될 때만 별도 실행한다.
