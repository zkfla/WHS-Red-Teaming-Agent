# 승인 경로·시험 세션 연결 결과 — 2026-10-01

작업 브랜치: capability. 최신 origin/dev `e1fc3945d4332d9d7e5f22cbfe3025e42cf2ea25`를
capability에 로컬 병합했다. dev 브랜치 변경/push/PR 생성/원격 merge는 하지 않았다.

## 구현한 내용

1. `redteam.capability.deployment`: 운영자 승인 원본에서 MCP 정책, Engagement,
   stdio 환경 설정, 해시 연결 원본을 함께 생성. 기존 파일 덮어쓰기/Scope 추가 없음.
2. `redteam.app.mcp_deployment`: CLI 로딩 및 App bootstrap에서 파일 해시, 실제 실행
   정책, 등록 메타데이터, MCP 연결 설정/도구 목록/operation/위험도/토큰 전달 대상 일치 검증.
   부모 검사 이후 변경에도 대비하여 자식 프로세스에서 정책 해시 재검사.
3. `engagement_constraints.mcp_targets`: Agent에 origin/포트/경로/메서드/query/header 키와
   사용자 세션 참조·principal·expected_access 전달. credential 값/파일 경로는 전달 안 함.
4. 생성된 정책에 세션별 allowed_paths 지정. 두 사용자 비교는 두 세션의 등록/경로/자격증명을
   모두 확인한 뒤 GET 요청을 시작. 두 번째 세션 오류로 첫 요청만 나가는 문제 방지.
5. session_list를 canonical `web.response_analysis`에 연결. ToolId 규칙 변경 없음.
6. 최신 dev LLM-only 조사 경로와 기존 참고 자료 읽기 기능 호환. 참고 자료 선택도
   accepted path + LLM action을 거치며 예전 자동 playbook 실행을 복원하지 않음.
   browser/proxy/nuclei 기존 추가 도구 테스트도 명시적 모델 fixture로 이관.

## 실제 검증

- 전체: **1904 passed, 22 skipped**, 63.48초.
- 실제 pinned 방법론 원본 체크아웃을 제공한 추가 회귀: **14 passed**, 7.21초.
- 변경 Python 파일 Ruff 검사, git diff 공백/충돌 마커 검사 통과.
- 합성 localhost 웹에서 실제 App → Agent Loop → Control → Registry/Gateway → stdio MCP 실행.
- discovery 11개 도구, Agent 선택 2개(session_list + http_compare_sessions), HTTP GET 2회,
  시험 사용자 A=200/B=403. 상태·본문 해시만 반환. 토큰/합성 개인정보 본문 비노출.
- Control 승인 필요 시 Agent 실행 0회 및 HTTP 요청 0회.
- 두 번째 사용자 토큰 누락/경로 불일치 시 HTTP 요청 0회.
- 정책/원본/Agent 등록/실행 인수/operation/위험도/실행 파일/자격증명 전달 대상/동시성
  설정 변경 및 host:port 범위 불일치 차단.

검증은 `/private/tmp/whs-capability-validation-ypfXmK/venv`의 별도 환경에서 수행했다.
최신 dev가 요구하는 networkx/tenacity/prompt-toolkit 관련 의존성을 이 환경에만 준비했으며
프로젝트 `.venv`의 설치 상태는 변경하지 않았다.
22개 skip은 미설정 선택적 외부 MCP/원본·런타임 및 일부 환경 제한 테스트이며,
실제 금융사 또는 실제 모델의 정상 동작을 통과로 간주하지 않는다.

## 처음 제기된 팀별 문제에 대한 상태

| 문제 | 이번 MCP 작업 결과 |
|---|---|
| 자연어 단계 완료 조건 | 최신 dev의 typed completion 구조 유지. 이번 작업에서 의미 판단 로직 수정 안 함 |
| 포트 포함 대상 범위 비교 | Control 문제 그대로. 포트 삭제로 우회하지 않고 불일치 차단 |
| MCP 세부 허용 경로 동기화 | 새 승인 DeploymentSpec 기반 로컬 web-http/recon-passive 경로에 구현·검증 |
| IDOR 사용자 세션 자동 생성 | **제공된 시험 세션 자동 등록/동기화만 구현**. 계정/로그인 세션 생성은 미구현 |
| 종료 후 질문의 목표 분류 | 최신 dev CLI 구조 유지. 이번 수정 범위 아님 |
| 종료 결과·지시 귀속 | 최신 dev CLI/오케스트레이션 구조 유지 및 회귀 실행. 이번 도구 수정 범위 아님 |

이 기능은 opt-in이다. 기존 수동/원격 설정 및 `/`만 만드는 기본 CLI Recon wizard를
새 승인 프로필로 자동 바꾸지 않는다. ZAP/Burp/Chrome의 원격 실행 정책 동기화도 별도다.
실제 금융 인증/계정 생성/OTP/보안키패드/우회/실거래는 구현·검증하지 않았다.
예제 시험 세션의 expected_access는 운영자 가정이며 IDOR 취약점 확인 증거가 아니다.

운영 순서와 Agent 팀 전달 계약: [승인 설정 사용 안내](../../deploy/mcp/approved/README.md).
