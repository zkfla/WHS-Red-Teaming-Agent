# dev-jm-native-specialists 브랜치 안내

기준 dev: `153265e32e029481628e8c057fe0ea476d939d64`. 갱신일: 2026-10-11.
구형 `dev-jm`은 보존한다. 이 브랜치는 최신 dev 실행 기반으로 전문 기능을
선택 이식한 preview이며, 두 브랜치의 전체 병합이나 PRD 완료를 뜻하지 않는다.

공개 위치: [dev-jm-native-specialists](https://github.com/zkfla/WHS-Red-Teaming-Agent/tree/dev-jm-native-specialists).
`dev`/`dev-jm`을 직접 갱신하거나 PR을 생성하는 작업이 아니다. 새 동료는 이 문서,
아래 역할 설정, [모듈별 구현·검증 기록](dev-first-specialists-implementation-20261010.md)
순서로 읽으면 된다. 기록의 과거 “push 없음/다음 작업”은 당시 상태이며 최신 현황은
이 안내와 구현 기록의 마지막 절을 따른다.

현재 이후 작업은 **Web/API·Recon 중심이며 Mobile은 제외**한다. 기존 Mobile
코드와 전체 프로필은 호환성 보존용이다. 새 Run에는 아래 Web 전용 프로필을
선택하며, 기존 Run을 다른 구성으로 강제 재개하지 않는다.

## 구성과 실제 동작

사용자 목표 → 전략 플래너 → 작업 분해 → 기존 MissionTask/outbox → 기존
InstanceDriver → native MODEL/TOOLS/SETTLE → Control/MCP/증거/Graph → 조건 검토
→ 후속 Task와 새 실행 instance. 새 작업 큐나 대체 실행 엔진은 만들지 않았다.

역할은 [Web 전용 설정](../../config/agents/native-specialists-web.yaml)과 그 파일 기준의
version/hash 관리 MD로 조립한다. 역할 이름을 중앙 루프에서 비교하지 않는다.

| 논리 역할 | 책임 |
| --- | --- |
| 대화 | 사용자 입력과 Run 귀속, 승인 추정 금지 |
| 플래너 | 현재 목표·관측에 기반한 조사 질문과 전략 개정 |
| 작업 분해 | 질문·완료 조건·의존성을 기존 Task에 보존 |
| Web 실행 | 자기 Task와 실제 Web/API·Recon 도구를 native 루프로 선택·실행 |
| 연구·증거 | 원본·가설·현재 증거 유효성을 구분하고 완료 근거 검토 |
| 코드 작성 | 증거 참조 코드 artifact 작성, 다른 역할의 독립 검토 |

현재 프로필은 여섯 논리 역할이며 Web만 대상 실행 역할로 등록한다. Web 작업
인스턴스는 기존 동시성 한도 안에서 여러 개 실행할 수 있다. 나머지 역할은 전문
판단 stage로 연결하므로 여섯 개의 상시 프로세스가 필요하지 않다. 기존
[전체 프로필](../../config/agents/native-specialists.yaml)은 일곱 역할 구성을 보존한다.
Control, ROE, 실제 도구 준비 상태는 역할 등록과 별개다.

구현한 주요 계약:

- Task v2 완료 조건·영수증 hash, 실패·중복 원본의 완료 거절.
- 현재 관련 입력 CAS와 제안 멱등성, Ready 기동과 후속 조사.
- Run 공통 LLM 호출 원장, 명시적 총 요청 횟수 무제한과 유한 repair/429.
- provider metadata 기반 공유 자원, 미정산 실행 marker와 원래 요청 재개.
- 로컬 Run 실행 소유권, 승인/cohort 재개, 미진입 증명이 있는 zero-checkpoint 복구.
- 원본과 정확히 일치하는 불변 증거 append의 저장 완료 응답 유실 복구.
  기존 전문 cohort 재개에서만 확인하며 Task claim/도구 실행 marker 해제는 하지 않는다.
- 새 Graph 변경의 원자적 저장 영수증에 기반한 응답 유실 복구.
  현재 Graph 값 비교나 재적용을 하지 않는다. SQLite의 실제 commit을 확인하며,
  구형 proof 부재·receipt 부재/손상·지원하지 않는 provider는 계속 보류한다.
- `status --activation-db`의 읽기 전용 기동/호출 진단과, 명시적 Work/Evidence
  경로를 추가한 저장 원본 교차 진단. provider 조회·재실행·해제 허가는 아님.
- 증거 현재 유효성 소비자, 코드 artifact 작성·독립 검토·불변 저장.
  작성/조회는 미실행 DATA이며, 명시적 정책을 선택할 때만 `code.validate`가
  기존 network-none 격리 workroom에서 합성 JSON case를 검사한다.
  실패·불명/취소 결과는 재실행하지 않으며 fixture 성공을 대상 증거로 인정하지 않는다.
- `agent.readiness`/planner의 역할 준비 진단은 실제 조립 catalog의 정책/health를
  사용한다. Mobile descriptor 전체 조회도 같은 기존 승인/hash 정책을 적용한다.
  등록·health/적격성 관측과 실제 기기 실행·Control 허가는 구분한다.
- 활성 cohort에서 관측된 증거 만료/실제 유효성 변화 → 기존 planner/Task 기동.
  별도 주기적 모델 호출·큐 없이 같은 Driver의 대기를 사용한다. 보류·종료·부분 재개는
  자동 기동하지 않으며 현재 원본·Control·공유 예산 검사를 유지한다.
- entered + checkpoint 부재 actor의 명시적 원본 수집 API. 기존 recovery port와
  동일한 submission/원본 검증을 일반 재개에도 사용한다. 결과만 append-only 보존하며,
  모델/Graph/정산/claim 해제/자동 재개는 하지 않는다. status 조회는 계속 provider를
  호출하지 않는다. 명시적 `collect-originals RUN_ID engagement.yaml [--json]` CLI로
  기존 원본만 수집할 수 있다. 수집 성공은 워커 재개/Task 완료 허가가 아니다.
- 기존 점유 정리 API는 canonical 종료 Task만 소유권 확인 후 정리한다. 미완료
  Task의 인계는 거절하며 Work 자원·모델 요청 비용/slot은 풀지 않는다. 요청 원장의
  손상된 identity/state/usage를 빈 예산으로 처리하지 않도록 공통 검사를 적용했다.

## 설정과 비용 없는 확인

저장소 루트에서 명시적으로 선택한다. 상대 MD 경로는 프로필 디렉터리 기준이다.

```sh
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/native-specialists-web.yaml"
export REDTEAM_LLM_REQUEST_POLICY_JSON='{"mode":"bounded","max_requests":100,"max_concurrent_requests":2}'
```

모델 공급자·키·승인된 engagement·MCP 실행/정책 설정은 기존 실행 설정이 별도로
필요하다. 키를 Git에 저장하지 않는다. 위 두 값만으로 실환경 실행 준비가 끝나지 않는다.
MD/profile/정책이 달라진 Run은 새 구성으로 강제 재개하지 않는다.
프로필은 역할 구성이지 MCP 서버 목록 필터가 아니다. `capability.mcp_servers`와
`REDTEAM_MCP_SERVERS`도 실제 사용할 승인된 Web/Recon 공급자만 선택해야 한다.
서버 ID는 운영자 배포 설정을 따르며 이 문서에서 이름을 추측해 고정하지 않는다.
기존 engagement/Run의 scope·정책을 자동 수정하지 않는다.

`unlimited_requests`를 명시하고 `max_requests:null`로 설정하면 **LLM 총 요청 수만**
무제한이다. 무료를 뜻하지 않으며 동시성·repair·429·전략/기동 횟수·시간·Control
제한은 유지한다. 누락/0/-1은 무제한이 아니다.

```sh
python3 -m venv .venv
./.venv/bin/pip install -e '.[dev]'
umask 077  # 합성 Workspace/VDB 입력도 소유자 전용 파일 정책을 충족
./.venv/bin/pytest -q tests/unit/test_native_specialist_profile.py tests/integration/test_native_specialist_closed_loop.py tests/integration/test_specialist_resume.py tests/integration/test_zero_checkpoint_resume.py
```

이 시험은 scripted 모델과 합성 도구를 명시적으로 사용한다. 실제 금융권 성능이나
라이브 MCP 설치 완료를 증명하지 않는다. 전체 검증·모듈별 근거는
[구현 기록](dev-first-specialists-implementation-20261010.md)을 확인한다.

## 아직 완료하지 않은 작업

- entered인데 checkpoint가 없는 실행의 전체 상태/계량 재구성·
  명시적 claim handoff와 나머지 writer/orphan 복구.
  새 증거 append와 Graph mutation의 검증된 응답 유실은 복구한다. 구형 proof 부재,
  구형 물리 edge 삭제 및 원본 불명은 계속 보류한다.
  Work/저장 원본의 읽기 전용 진단만으로 claim을 해제하지 않는다.
- 승인된 실제 Web LLM 비교 평가. 기존 100회·0달러 조건에서는 유료 호출하지
  않는다. 요청 횟수 무제한 설정이 비용 허용을 뜻하지 않는다. Mobile 평가는
  이번 범위에서 제외했으므로 남은 작업이나 완료 차단 조건으로 계산하지 않는다.
  보류·종료 Run의 독립 timer service도 구현 범위 밖이다.
- gated install 운영 경로 검증: Docker Desktop의 quota 부재는 유지되지만,
  별도 Lima Linux VM에서 실제 합성 OPEN/proxy/DENY/계량·정산 시험 5건이 통과했다.
  운영 mirror pin·이미지 신뢰·승인 정책과 실제 환경 평가는 별개로 남는다.
  shell 회귀와 이전 환경 실패는 16~17절에, VM 검증은 18절에 구분한다.
  quota/Control을 완화하거나 기존 실패를 지워 성공으로 계산하지 않았다.
- 최종 전체 인계 및 운영 검증. 실제 대상 생성 코드 실행 규격은 미확정·비활성이다.

기동/원본 교차 진단의 사용법과 조회 범위는 [복구 계약](../contracts/activation-recovery.md)의
5.2~5.8절을 확인한다. 진단/수집 모드의 exit 0은 조회/수집 성공이며
Run 완료/재개 허가가 아니다.

M5의 로컬 구현·검증은 완료했다. 이는 unknown/unsafe 복구를 강제 허용하거나
M6/운영 준비까지 완료한 뜻이 아니다. offline fixture는 기본 비활성이고,
`REDTEAM_CODE_FIXTURE_POLICY_PATH` 또는 동일 설정 필드와 선택된 작성/독립 검토
역할이 필요하다. 정책 변경은 기존 Run/artifact binding을 무시하고 재개하지 않는다.
정확한 설정·실행/보류 의미는 [fixture 계약](../contracts/code-fixtures.md)을 확인한다.

## 검증 결과를 읽는 방법

M5 로컬 완료 근거는 구현 기록 15절이다. shell 취소·정리 오류 수정은 16절,
Docker Desktop의 quota 부재 진단은 17절, 같은 소스/이미지를 사용한 별도 Lima
Linux VM의 실제 gate **5 passed / 0 skipped**와 관련 unit **63 passed**는 18절이다.
이전 Desktop의 4건 실패를 삭제하거나 안전 정책을 완화한 것이 아니라, 필요한
커널 기능이 있는 다른 환경에서 별도 입증했다. Mac의 Docker context/실행 설정은
자동 전환하지 않았다. 합성 설치 프록시 검증은 실제 registry 신뢰나 금융권
취약점 발견을 입증하지 않는다. 새 Web 전용 프로필·공개 전 검증 결과는 19절이다.
최종 Linux 검증은 전체 **4,379 passed / 1 예제 복사 누락 실패 / 62 skipped**,
누락된 추적 예제를 복사한 후 해당 실패만 **1 passed**다. 따라서 통과 사례 집합은
4,380개이며 62개는 미실시다. 수정 전 실패·준비 누락·focused 중복을 합산해서
단일 전체 green 출력으로 표시하지 않는다. Ruff, 전체 mypy(743 파일), CI YAML과
두 Compose 설정 검사도 통과했다. 실제 LLM 성능/운영 승인까지 입증한 것은 아니다.

실행 준비 절차는 [gate 안내](../../deploy/docker/gate-sidecar/README.md),
[shell 안내](../../deploy/docker/shell-workroom/README.md),
[offline fixture 계약](../contracts/code-fixtures.md)에 있다. 키·개인 `.env`·실제
세션·Run 데이터·로컬 검증 XML은 Git에 올리지 않는다. 이 브랜치의 push는 현재
워크플로 설정상 dev 전용 CI를 자동 실행하지 않으므로 로컬 통과와 GitHub CI
통과를 같은 의미로 사용하지 않는다.
