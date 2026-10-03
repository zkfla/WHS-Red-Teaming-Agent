# Capability 공통 코드 최종 통합 — 2026-10-03

이 문서는 `d973b36`의 **dev 병합 전 기록**이다.
최신 dev 통합 후 정의 경로·오류 구분·검증 결과는
[dev–Capability 후속 통합 결과](dev-capability-integration-result.md)를 따른다.

## 기준과 결정

- 작업 브랜치: `capability`. 원격 B·C 반영 기준 `02d8b5c`를 fast-forward했다.
- dev 병합·PR·원격 push는 이번 통합 작업에 포함하지 않는다.
- 사용자 확인: PM·Agent에서 `web.page_read`와 `web.web_search` 이름을 확정했으므로 반영한다.
- B의 기존 `command_run`·`command_job_*` 목록은 유지한다. B의 분석 활성 조건,
  실행 opt-in, Docker 격리, 고정 recipe 및 job 상태·취소 제한은 변경하지 않는다.
- 기존 미커밋 `deploy/mcp/README.md`, `tests/integration/test_analysis_mcp.py` 및
  upstream 안내·연결 상태·jadx 예제 3개는 보존하고 통합 커밋에서 제외한다.

## 바뀐 계약

지원 작업 이름 23개의 유일한 정의와 검사 함수는
`src/redteam/core/action_semantics.py`로 후속 dev 통합에서 모았다.
`core.contracts.action_naming`과 `orchestration.action_naming`은 호환 재-export다.
Capability가 Orchestration 내부를 import하지 않는다. 기존 ActionProposal,
TargetRef, ToolInvocation schema와 Core Port는 변경하지 않는다.

| 경계 | 검사/동작 |
|---|---|
| Settings | 환경·profile의 작업 연결이 공통 지원 목록인지 시작 시 확인 |
| Manifest | Agent에 노출할 최종 descriptor의 같은 목록·schema 확인 후 등록 |
| Catalog | 명시한 작업 연결과 조회 조건을 같은 공통 검사로 확인 |
| PlanStep / Compiler | 같은 검사로 후보 생성; 미지원 작업과 대상 오류를 구분 |
| LLM 후보 / 실제 Path Engine | 공통 작업 검사와 실패 코드 적용; 빈 작업 연결은 실행 메뉴에서 숨김 |
| Control / Gateway | 기존 scope·ROE·승인·도구 연결·인자 schema 검사 유지 |

실제 MCP tool_name은 실행 메커니즘이다. 설정을 생략했다고 행동 의미로 자동 승격하지 않는다.
일반 MCP의 연결이 없거나 도구 연결이 `[]`이면 발견 정보는 저장할 수 있으나
Agent 실행 후보와 승인 후 Gateway 실행 대상은 아니다. 일반 profile은
`REDTEAM_MCP_<ID>_TOOL_OPERATIONS_JSON`에 명시적 연결을 설정한다.
profile 관리 도구 중 빈 연결은 그대로 유지하며 임의 작업에 억지로 연결하지 않는다.
예제 `ping`은 이제 `service_discovery`에 명시적으로 연결한다.

오류:

- `capability_unsupported`: 미지원 namespace.
- `operation_invalid_format`: lower_snake_case 형식 오류.
- `operation_unsupported`: 공통 목록 밖 작업.
- `no_tool_for_operation`: 후속 통합에서 분리한, 지원 작업의 도구 연결 부재.
- `unknown_target_node` / `unresolved_target` / `target_type_mismatch`: 대상 관련 오류.

Settings·Manifest의 실패에는 서버·도구·operation 정보가 포함된다. 원격 응답 본문,
검색어나 인증정보를 그 오류에 추가하지 않는다.

## A·B·C 연결 의미

- A 파일·방법론·VDB: 기존 명시적 연결을 유지한다. 읽기와 변경 권한은 분리한다.
- B Execution: 기존 4개 공통 이름을 유지한다. Agent bridge는 기본 off이며
  opt-in일 때 고정된 `line_count` recipe의 `command_run`만 선택 가능하다.
  job 도구 3개를 자동으로 Agent 메뉴에 추가하지 않는다.
- C 페이지 읽기: `web.page_read` → `web.fetch_page_text`, 알려진 UrlTarget,
  운영자 허용 공개 HTTPS origin 및 서버 한도에 한정한다.
- C 검색: `web.web_search` → `web.web_search`, 공급자 키·활성 설정이 별도로 필요하다.
  query가 TargetRef가 되거나 Tavily endpoint가 시험 대상이 되지는 않는다.
  Agent는 기존 알려진 TargetRef를 조사 맥락으로 사용하며 Control은 그 대상·작업을 검사한다.
- 검색 결과 URL을 자동 방문·scope 추가하지 않는다. 페이지를 읽으려면 별도의
  알려진 대상과 `web.page_read` ROE 및 페이지 서버 origin 허용이 필요하다.

등록 성공은 실행 승인이나 취약점 확정을 뜻하지 않는다. 공개 정보가 포함된 본문과
검색 결과는 불신 관측 자료이며 운영 개인정보 정책·비용·egress 검토는 별도이다.

## 검증

`tests/contract/test_action_binding_consistency.py`:

- 23개 지원 operation 모두 Manifest·Catalog·PlanStep·Compiler의 기준 일치.
- 형식만 맞는 `future_intent`, `some_remote_tool`, `nmap_scan`을 등록 연결에서 거부.
- ToolId/remote tool_name이 우연히 공통 이름과 같아도 암묵적으로 연결하지 않음.
- 미지원 작업과 알려지지 않은 대상의 오류 구분.
- 검증되지 않은 model_copy 경로에서도 잘못된 작업을 대상 오류로 오진하지 않음.

`tests/integration/test_common_action_pipeline.py`:

- 두 C profile을 실제 MCP discovery로 하나의 Gateway에 함께 등록.
- scripted LLM 후보 생성 → Compiler → 실제 Control → Gateway → MCP.
- 작업 ROE 거절, scope 거절, 사람 승인 대기에서 외부 공급자 호출 0회.
- 실제 LLM-led Path Engine의 계획 검토/승인 → 도구 실행 → Evidence 기록.
- 네트워크 공급자는 HTTPX mock / Fake Tavily이며 외부 API·금융 사이트 접근 없음.

기존 MCP timeout·health·concurrency·출력 한도·dry-run 검사는 삭제하지 않았다.
종전 암묵적 ping 연결을 명시적 canonical 연결로 바꿨다. stdio 예제 서버 ID는
실제 mobile-local/analysis-local production manifest와 겹치지 않도록 분리했다.

최종 검증:

- 전체 unit/integration/contract: **2345 passed, 22 skipped, 0 failed**, 185.26초.
- 최종 공통 계약·C 경로·settings/manifest 집중 검사: **80 passed**, 1.81초.
- 추가된 공통 계약 29개 + C pipeline/Path Engine 10개 = 신규 39개 시험.
- 변경 Python 파일 Ruff 검사, 신규/정리 파일 포맷 검사, `git diff --check` 통과.
- 전체 시험은 기존 미커밋 Analysis 테스트를 포함한 현재 작업 트리에서 실행했다.
  미커밋 파일은 이번 커밋에서 제외하므로 clean checkout 시험과 동일하다고 주장하지 않는다.
- skip 22개: live mobile discovery 3, apktool/jadx 설치 시험 3, 실제 APK/ADB/mobile
  E2E 3, Docker Execution 9, 실제 Execution LLM 1, 외부 웹 MCP 1, Tavily 직접/MCP 2.
  실제 Docker·LLM·Tavily·ZAP/Burp 접속을 새로 검증했다고 주장하지 않는다.

## 운영 팀에 필요한 후속 설정

1. 기존 일반 MCP 환경에서 빠진 `TOOL_OPERATIONS_JSON` 연결을 명시적으로 보완한다.
   이름만 발견됐다고 Agent에 실행 가능 도구로 표시하지 않는다.
2. 운영 검색 config·Tavily 키와 페이지 config·공개 origin을 설정한다.
   키는 로컬 비밀 환경에서 제공하고 repository나 채팅에 넣지 않는다.
3. 조사 맥락 TargetRef와 공개 페이지 UrlTarget을 operator/Agent 계약에 따라 등록하고
   Control ROE에 필요한 `web.web_search` / `web.page_read`만 허용한다.
4. 실제 LLM 선택·운영 Tavily 품질/비용·승인된 대상·실제 Docker와 외부 MCP를
   별도의 opt-in 시험으로 검증한다. 로컬 synthetic 성공을 운영 검증으로 간주하지 않는다.
