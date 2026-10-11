# 조정 조사 결과 — 라이브 경로와 ARTEX/job 접점

작성일: 2026-10-07. 담당: /root/coordination. 개정: 계정 프로필 조사 추가, 429 해석 정정.
범위: dev-orchestration 미커밋 ReAct 전환 작업본의 (1) 실제 모델·MCP 라이브 가용 경로와 계정 전환 경로, (2) ARTEX 대응 long job·공급자 failure·실행 불명확 경계의 실제 소스 구현 구분, (3) 소형 종단 검증 계획.
코드 수정·커밋·푸시 없음. `.env` 원본 변경 없음. credentials 값·전체 프롬프트·응답 본문·전체 env 덤프 기록 없음. 계정은 경로·존재·만료 여부로만 판정했다.

## 0. 정정 — 429는 인증 유효성의 증명이 아니다

앞선 기록에서 provider 429 응답을 "인증 정상"으로 해석한 것은 잘못이다. 429는 provider가 보낸 응답 사실일 뿐이며 자격증명 유효성을 증명하지 않는다. 실제로 다른 계정 프로필은 같은 모델로 성공했다(2절). 429는 "provider 응답 = rate_limited"라는 사실로만 기록한다.

## 1. 방법

- 소스·문서 읽기 전용 조회(`rg`/`Get-Content`).
- 실행은 저장소 `.venv`(Python 3.14.6, litellm 1.104.0, redteam import 정상)에서만 수행.
- 라이브: 실제 로컬 MCP 서버 등록·도구 1회 호출, 실제 모델 호출(계정2 프로필 1회 + 계정1 프로필 1회). 모델명만 바꾼 반복은 하지 않았다.
- 계정 프로필은 경로 존재·토큰 필드 존재·만료 여부·account_id 동일 여부만 확인했고 값은 출력하지 않았다.

## 2. 실제 모델 라이브 경로

구성(비밀 아님):

- `.env`: `REDTEAM_LLM_PROVIDER=litellm`, `REDTEAM_LLM_MODEL=chatgpt/gpt-6-luna`, `CHATGPT_TOKEN_DIR=C:/Users/qkrgu/.config/litellm/chatgpt-account2`.
- API 키 변수(`LLM_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `REDTEAM_LLM_API_KEY`)는 `.env`와 프로세스 환경 어디에도 없다(존재 여부 False).
- `chatgpt/` provider는 API 키가 아니라 OAuth device-flow 토큰을 토큰 디렉터리의 `auth.json`에 캐시한다(`examples/chatgpt_login.py:1-8`, `src/redteam/app/cli/commands/interactive.py:210-213`). 어댑터 경로는 `src/redteam/llm/litellm.py:16-33`의 `litellm.aresponses` 스트리밍이다.

현존 계정 프로필(경로·존재·만료만):

- account1: `C:\Users\qkrgu\.config\litellm\chatgpt\auth.json` — 존재, access/refresh 토큰 필드 존재, 만료 아님. litellm 기본 디렉터리다.
- account2: `C:\Users\qkrgu\.config\litellm\chatgpt-account2\auth.json` — 존재, access/refresh 존재, 만료 아님. `.env`의 `CHATGPT_TOKEN_DIR`가 이 디렉터리를 가리킨다.
- 두 파일의 `account_id`는 서로 다르다(별개 계정). 값은 출력하지 않았다.
- 참고: `C:\Users\qkrgu\.codex\auth.json`도 존재하지만 Codex 앱 자체 자격증명이며 redteam/litellm 경로가 아니다.

런타임 계정 전환 경로(`.env` 원본 변경 없이):

- litellm chatgpt 인증기는 프로세스 환경변수 `CHATGPT_TOKEN_DIR`(기본 `~/.config/litellm/chatgpt`)과 `CHATGPT_AUTH_FILE`을 읽어 토큰 파일을 찾는다(`.venv/Lib/site-packages/litellm/llms/chatgpt/authenticator.py`; 이 venv 빌드는 식별자가 난독화되어 클래스명이 `n`으로 보인다). 인스턴스는 transformation 구성 시점에 생성된다(같은 패키지 `chat/transformation.py`, `responses/transformation.py`의 `self.n = n()`).
- 즉 실행 프로세스의 `CHATGPT_TOKEN_DIR`만 account1 디렉터리로 지정하면 계정이 전환된다. 파일 수정은 하지 않았다.

관측(라이브):

- account2(`.env` 현재값)로 `chatgpt/gpt-6-luna`: provider 응답 `rate_limited`, status 429.
- account1(`CHATGPT_TOKEN_DIR`를 런타임에 account1로 지정, 동일 모델) 1회: 성공(비어 있지 않은 응답 수신). 프롬프트·결과 본문은 기록하지 않는다.
- 판정: 실모델 라이브 경로는 account1 프로필로 가용하다. account2는 현재 429이며 그 원인(계정 쿼터 또는 라우트)은 미확인이다. 모델명만 바꾼 3종은 모두 같은 429였으므로 반복하지 않았다.

## 3. 실제 MCP 도구 라이브 경로

등록 설정:

- `.env` `REDTEAM_MCP_SERVERS=recon-passive`. 서버는 저장소 로컬 stdio 서버다: `.venv/Scripts/python.exe -m redteam.capability.servers recon-passive --policy mcp-recon-policy.json`.
- `open_mcp_tool_gateway(["recon-passive"], health_refresh_enabled=False)` 등록 결과 도구 6개, 전부 `health_status=healthy`:
  - `recon.recon-passive.dns_resolve`, `http_probe`, `tls_inspect` → operation `service_discovery`
  - `recon.recon-passive.openapi_inventory`, `oidc_inventory`, `html_inventory` → operation `endpoint_discovery`
  - 전부 `requires_target=True`, `target_types=[host, service, endpoint]`.
- 정책 `mcp-recon-policy.json`: `targets.localhost`(origin `http://localhost`, allowed_paths `["/"]`), `allowed_networks` `127.0.0.1/32`, `::1/128`. 로컬 대상만 허용한다.
- 라이브 호출: 127.0.0.1:80에 임시 정적 서버를 띄우고 `ToolInvocation(tool_id="recon.recon-passive.http_probe", operation="service_discovery", arguments={"target_ref":"localhost","path":"/"})` 1회. 결과 `success=True`, `status_code=200`. 호출 후 서버 종료. 부작용은 로컬 1회 GET으로 제한했다.

targetless(대상 없는) 도구 가용성:

- 저장소 프로필 7종(`vdb_readonly`, `vdb_readonly_extended`, `page_text_readonly`, `execution`, `web_search_tavily`, `workspace_read`, `workspace_write`) 중 `requires_target=False`를 선언하는 것은 `web_search_tavily` 하나뿐이다(`src/redteam/capability/registration.py:608-658`, `:545`). 나머지는 asset/url 등 대상 바인딩을 갖는다.
- 외부 MCP가 metadata로 `redteam.requires_target`을 선언하는 파싱 경로는 있다(`src/redteam/capability/adapters/mcp/streamable_http.py:143-148`). 그러나 저장소에 실제 등록된 서버 중 그렇게 선언한 것은 없다.
- 도구 이름을 바꿔 `requires_target`을 강제 override하는 방식은 사용하지 않았다.
- `.env`와 프로세스에 `TAVILY_API_KEY`가 없다. `tests/integration/test_web_search_mcp_live.py:23-25`가 이 키를 요구한다. 따라서 targetless 라이브는 현재 불가이며, 기존 프로필 중 대체 targetless 도구도 없다. in-process 대역(`tests/integration/_web_search_stdio_fake.py`)은 라이브 대체가 아니다.

## 4. 소형 종단 검증 계획 (변경 없는 준비 상태)

선행 차단 요인(확인):

- `src/redteam/orchestration/nodes/verify.py:137`에 열린 호출이 남아 파일이 구문 오류 상태다(`ast.parse` 실패: line 137 `'(' was never closed`). 이 상태에서는 엔진/CLI 종단 실행이 불가하다. 수정은 이번 범위(코드 수정 금지)에서 수행하지 않았다.
- 계정2 프로필은 429이고, 계정1 프로필은 가용하다.

계획(코드 통합 후 root와 실행):

1. 코드 담당이 `verify.py:137`의 잔여 라인을 제거해 구문 오류를 해소한다.
2. 로컬 종단: 정책이 허용하는 localhost 정적 서버를 띄우고, recon-passive만 등록한 최소 engagement로, 실행 프로세스의 `CHATGPT_TOKEN_DIR`를 account1로 지정해 실제 모델 1회 실행. 관측 지점은 모델의 도구 선택 → Control admission → 실제 로컬 실행 → evidence 저장 → 정산 이벤트다.
3. targetless 종단: `TAVILY_API_KEY`가 제공되면 web_search 프로필을 등록해 query-only 경로를 1회 실행한다. 키가 없으면 보류한다.
4. 실행은 root와 함께 수행하고, 실패하면 provider 응답 종류/상태만 기록한다.

## 5. ARTEX/job 접점 — 실제 소스 구현 구분

별도 read-only 감사에서 확인한 결과를 요약한다(파일·행 근거).

### 5.1 Long job

- 구현됨: 상태·계약 `LongJobState`(pending/running/succeeded/failed/cancelled/unknown)와 `LongJobAdapter`(submit/status/result/cancel), 위임 매니저 `src/redteam/orchestration/long_jobs.py:40-155`. 실행 중 제출·바인딩·폴링과 `UNKNOWN`/`마감 초과` 분기 `src/redteam/orchestration/nodes/execute.py:180-211`. 제출 영수증 `execution_submission` 보존 `src/redteam/orchestration/nodes/execute.py:340-376`. 사후 수집 `reconcile_job` `src/redteam/orchestration/engine.py:2565-2652`. 원장 `job_id`/`bind_job`/`reconcile` `src/redteam/orchestration/work/models.py:60`, `work/store.py:17,71-72`, `work/sqlite.py:170-197,452-459`, `work/memory.py:80-89,225-227`, `work/rules.py:199-230`.
- 부분·미완성: 기본 조립에 어댑터가 없다. `src/redteam/app/bootstrap.py:453`(`long_job_adapter=None`) → `:730` 전달, `src/redteam/orchestration/context.py:64` 기본 `None`. 따라서 기본 실행은 동기 `invoke` 경로이며, 장기 작업은 조립자가 어댑터를 주입하기 전까지 동작하지 않는다. 마감 초과 후 자동 수집이 아니라 명시적 `reconcile_job` 판단이 필요하다(`src/redteam/orchestration/engine.py:1943-1944`, `nodes/execute.py:211`).
- 미구현: 실제 백엔드 어댑터(`submit/status/result` 구현체)와 범용 background 실행기. `long_job_adapter` 검색에서 조립점 외 구현체를 찾지 못했다.

### 5.2 공급자 failure 후 회수

- 구현됨(모델): `LLMProviderFailure`(kind/status/code/retryable/retry_after) `src/redteam/core/ports/llm.py:79-118`, 분류·정규화·재시도 힌트 `src/redteam/llm/litellm.py:450-536`, 인지 계층 재시도 `src/redteam/orchestration/cognition.py:774-821`.
- 구현됨(도구): 경계 재시도는 `src/redteam/orchestration/nodes/execute.py:292-310`에서만 열린다. 조건은 `retry_retryable_tool_failures`(`src/redteam/orchestration/settings.py:33-37`, 기본 True), LOW 위험, `retry_safe`/`retry_policy`, `InvocationAdmissionControl`, `long_job_adapter is None`이다. 재승인 후 수행 `nodes/execute.py:393-427`, 계수 `dispatch.py:52-53`.
- 구현됨(재생): 저장 원본 재처리는 도구를 재실행하지 않는다(`src/redteam/orchestration/recovery.py:1-15`). unsafe replay 거부 `src/redteam/orchestration/runner.py:44-45`, 승인 재개 durable claim `runner.py:94-108,604-671,802-931`.
- 미구현: 모델·공급자 자동 전환·대체·에스컬레이션. `fallback_model`/`alternate_model`/`switch_model` 검색에서 구현체가 없다. `escalat` 적중은 사람 승인 경로뿐이다(`src/redteam/control/service.py:269,329-331,1448-1460`).

### 5.3 실행 불명확(execution-unknown) 경계

- 구현됨: `LongJobState.UNKNOWN`(`long_jobs.py:46`), 원장 상태·`outcome_reason`(job_reconciled, lease_lost_before_execution, lease_lost_during_execution, gateway_exception:*, policy_admission_denied), 모호성 판정 `is_execution_ambiguous`(`src/redteam/orchestration/recovery.py:57-68`). 재시도 금지 `src/redteam/orchestration/nodes/execute.py:209`("do not resubmit"), `src/redteam/orchestration/engine.py:2497`. 수집 상태값 `no_registered_job/missing_original_action/unrelated_job_result/result_collected_ledger_unsettled/collected` `engine.py:2574-2646`.
- 부분·미완성: 전송 오류 종류별 전용 코드가 없고 timeout·취소·임대 상실 코드가 분산돼 있다. 승인 재개 in-flight 불명확 자동 판정 대신 durable claim과 unsafe replay 거부가 그 자리를 대신한다.
- 미구현: `timeout`/`Connection closed`/승인 후 재개/in-flight unknown 전용 구분 코드. `Connection closed` 문자열은 소스 계약에 없다.

### 5.4 ARTEX 대응 기능 대조

- worker claim·실행·기록: ARTEX식 영속 worker pool과 frontier claimNext는 없다. 대응물은 작업 원장 임대다(`scheduler.py:17-82`, `work/store.py:30-60`, `work/rules.py`, `dispatch.py:52-71`).
- 그래프 변화 기반 Planner 재기동: 변화·heartbeat가 Planner를 깨우는 루프는 확인되지 않았다. 있는 것은 `replan_task_graph`(`replanning.py:79-234`)와 스케줄러 `graph_revision`(`scheduler.py:21,41`)뿐이다.
- 영속 todolist: 같은 지속 todo 객체는 없다. `WorkItem.prerequisite_ids`와 규칙 `prereqs_succeeded/blocked_for_dependency`(`work/rules.py`), `MissionTaskStore` 영속 선택지(`app/bootstrap.py:408-423`, 기본 인메모리 `context.py:79`)가 부분 대응이다.
- 문서 주의: `docs/proposals/langgraph-react-loop-proposal-20261007.md`가 링크하는 `il-runtime-limitations-20261007.md`는 실제로 존재하지 않는다. 실제 파일은 `research_notes/레드팀 에이전트 실행 구조 비교/artex-runtime-limitations-20261007.md`다. 문서의 ARTEX 설명을 구현 사실로 취급하지 않았다.

## 6. 미확인·한계

- account2 프로필이 429인 원인(계정 쿼터인지 라우트인지)은 미확인이다. provider 응답 사실로만 기록한다.
- 동일 프로세스 안에서 실행 도중 계정을 바꾸는 것은 인증기 인스턴스 생성 시점에 묶여 있어 미확인이다. 프로세스 시작 시 `CHATGPT_TOKEN_DIR` 지정은 확인했다.
- targetless 라이브는 `TAVILY_API_KEY` 미비로 미검증이며, 기존 프로필에 대체 targetless 도구가 없다.
- `verify.py` 구문 오류가 해소된 최신 작업본의 전체 검사·종단 결과는 없다(이번 조사는 코드를 수정하지 않았다).
- 5절의 "미확인"으로 남긴 `semantic_completion` 연계 범위와 도구 불건강 자동 대체 범위는 이번 읽기만으로 확정하지 않았다.
