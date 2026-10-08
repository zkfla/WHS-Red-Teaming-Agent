# P0~P2 로컬 구현 검수 — 2026-10-08

> 이 문서는 수정 전 검수 이력이다. F1 위조 재현은 후속으로 수정했고 F2 알려진
> 만료 기동도 구현했다. 현재 상태·최종 시험·실제 모델 비교는
> [P2 후속 보고서](p2-completion-20261008.md)를 기준으로 읽는다.

대상 checkout: `WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`.
branch/HEAD: `dev-jm` / `ec1b8d9` + 기존 미커밋 P0/P1/P2 변경.
원격 GitHub 최신 상태를 검사한 것이 아니다. fetch/pull/commit/push/PR, 기존 소스·
개인 설정 수정, 실제 LLM·외부 사이트 호출은 하지 않았다. 검수 문서만 추가·보완했다.

## 결론

**P2 핵심 연결은 구현되어 있으나, P2 전체 완료로 판정할 수 없다.**
생성 코드의 fixture 결과 위조를 실제 Docker에서 재현했다. 또한 원래 PRD의 알려진
만료에 의한 재평가 기동과 실제 LLM 비교는 완료되지 않았다. 앞서 표현한 “로컬 기능
마무리”를 runner 결과 신뢰성까지 검증된 완료로 받아들이면 안 된다.

## 요구사항 → 코드 → 확인 수준

| 요구사항 | 실제 연결 경로 | 판정 |
| --- | --- | --- |
| P0 역할/신뢰된 프롬프트/Planner·Atomizer 분리 | `app/agent_profile.py`, `orchestration/prompts.py`, `specialist_planning.py`, `config/agents/prompts/` | 연결 확인. 고정 7개 역할 예시 구성이지 중앙 코드의 닫힌 ID 목록이 아님 |
| P1 내구성 협업·독립 실행·Control 경계 | 기존 Coordinator/outbox/work 원장, dispatch/worker/aggregate. `test_specialist_p1_mcp.py`는 P1/P2 × 격리 배치/LangGraph × 승인 유무 | 실제 임시 stdio MCP/loopback HTTP 통합시험과 기존 회귀로 검사. 모델은 fixture이며 자율 조사 성능 증명이 아님 |
| P2 요청별 판단/사건/토큰 상태 격리 | `engine.fork_decision`, `parallel_reasoning._infer`, `decide_parallel`, 원래 delegated 반영 경로 | 교차 응답·지연·취소·역할 동시성·관련 변화/무관한 변화 시험 있음 |
| P2 공유 판단 요청 pool | 기존 Coordinator에서 `reserve`/`settle`, SQLite 재개; 미확정 예약은 보수적으로 유지 | specialist 요청 총량에 한정. 모든 stage의 전역 토큰/비용 pool은 아님 |
| P2 현재 증거 유효성 | `evidence.assess` → 원래 store의 SYSTEM binding → worker 문맥/held fingerprint/판단 admission/완료 gate/보고 | 명시적으로 바인딩한 관련 주장 조회 경로 구현. 원본 보존·stale receipt 배제 확인 |
| 알려진 만료의 재평가 기동 | PRD §19.2는 결정적 timer 기동을 요구하지만 구현은 조회 시 `datetime.now` 비교 | 부분 구현. 시간 도달 자체가 사건을 만들거나 잠든 조사를 재개하지 않음 |
| P2 코드 작성·독립 검토·버전/해시 바인딩 | `code.prepare` → 다른 role의 `code_review` → 기존 artifact store → `code.test` → core FixtureRunner port | App 조립과 generic local dispatch 연결·인자/목적/원본/정책/검토 해시 변경 거절 확인 |
| 네트워크 없는 fixture runner | `capability/fixture_runner.py` → cached pinned image, non-root/read-only/network-none, CPU·메모리·PID·시간·출력 제한/취소 | 실제 격리 시험은 통과하나 **판정 결과 위조 오류 있음** |
| job 추적 | 기존 `LongJobAdapter`, `WorkItem.job_id`, execute/status/cancel/lease 계약 | port를 실제 adapter로 주입한 경우에만 활성. P2 선택만으로 ZAP/Burp 비동기 backend가 연결되지 않음; fixture는 bounded 동기 실행 |
| 실제 LLM 비교·단계별 활성화 | opt-in version 4 프로필 있음. PRD §14 P2 및 §15.2의 동일 조건 비교 자료 없음 | 미검증. 모델·호출/비용 한도 확정 후 별도 수행 필요 |

## 반드시 수정할 확인된 오류

### F1 — 생성 코드가 자신의 fixture 합격 판정을 위조할 수 있음 [우선순위 높음]

근거: `src/redteam/capability/fixture_runner.py:21`의 harness는 candidate 모듈을
자기 프로세스에 import한다. 같은 파일 `:137`에서 runner는 그 프로세스의 stdout을
JSON으로 읽고 `passed == total`이면 합격으로 반환한다. 생성 코드와 신뢰된 판정이
같은 프로세스/출력 채널을 공유한다.

실제 Docker 재현 결과:

| 합성 프로그램 | 실제 기대 조건 | runner 판정 |
| --- | --- | --- |
| `evaluate`가 False 반환 | 기대값 True → 실패 | failed / passed=0 / total=1 |
| `evaluate` 없이 합격 요약 JSON 출력 후 종료 | 함수/케이스 평가 없음 | **passed / passed=1 / total=1** |
| `evaluate`가 False 반환하도록 정의했지만 import 시 합격 요약 출력 후 종료 | 기대값 True. 함수는 실행되지 않음 | **passed / passed=1 / total=1** |

재현은 기존 DockerFixtureRunner/public port와 선택 프로필을 그대로 사용했다.
호스트에서 생성 코드를 실행하거나 대상 사이트에 요청하지 않았다.

영향: fixture 결과를 믿고 잘못된 artifact를 후속 입력으로 사용할 수 있다.
`fixture_only=True`, SYSTEM 기록, 기존 Finding/완료 원본 검증은 유지되므로 이 재현이
실제 취약점 Finding 승격이나 금융 대상 실행 승인 우회를 입증하는 것은 아니다.
다른 모델 역할의 코드 검토도 실행 결과 채널의 무결성 보장 수단은 아니다.

수정 방향:

1. 생성 코드가 쓸 수 있는 채널에서 최종 합격/실패 판정을 받지 않는다.
2. candidate의 제한된 입력별 실제 출력과 신뢰된 컨트롤러의 기대값 비교를 분리한다.
   기대값·최종 verdict를 candidate가 읽거나 덮어쓸 수 있는 구조도 검토한다.
3. 조기 종료·가짜 요약·harness 변조·출력 누락/중복은 실패/미확정 처리하고,
   정상 함수와 기존 격리/취소 시험은 유지한다. 단순 문자열 필터/nonce만으로
   동일 프로세스 신뢰 경계를 해결했다고 하지 않는다.
4. 위 세 경우를 실제 Docker 회귀에 추가한다. 생성 코드를 호스트로 옮기거나
   Control/네트워크 제한을 완화해서 해결하지 않는다.

## 빠진 기능과 검증의 구분

- 만료 비교 자체는 구현됐다. 만료 사건 → 관련 주장/위임만 재평가하는 기동은 없다.
  기존 작업·outbox·재개 계약에 최소 연결하거나 PM이 조회 시 검사만으로 범위를
  변경한다고 명시해야 한다. 모든 증거의 주기적 LLM 호출·무한 Run 재시작은 해결책이 아니다.
- `evidence.assess`의 조건은 모델/호출자가 선택한다. 조건이 없는 `unbound` 원본은
  기존 검증을 따르며 모든 인증/버전 조건의 최신성이 자동 보장되는 것은 아니다.
- P2의 `evaluate(JSON)->JSON` fixture artifact는 실제 네트워크 PoC runner가 아니다.
  실제 target/session/egress/요청 계측/운영자 승인 실행은 P3 영역이다.
- 작성 모델이 기대값도 제안하므로 현재 정상 fixture 합격조차 독립 취약점 증명은 아니다.
- 실제 모델이 역할 분업을 적절히 선택하고 임의 랩에서 목표를 달성하는지는 자동
  테스트의 fixture 모델로 검증할 수 없다. 실제 LLM 평가는 P2 원래 완료 기준에 포함돼 있다.

## 이번 검증

- 기존 실제 Docker 격리 시험: **5 passed**, 6.38초.
- 추가 runner 반례: 위 3개 사례를 관측. 두 위조 사례가 잘못된 합격으로 반환됨.
  이는 기존 회귀에서 다루지 않은 오류이며 기존 테스트 통과 수치로 덮을 수 없다.
- CI 범위 Ruff 통과. Mypy **602 source files, no issues**.
  기존 annotation-unchecked 안내는 남아 있다.
- 전체 회귀: **3,009 passed / 56 skipped**, 298.31초. 외부 MCP 선택을 비우고
  `env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' .venv/bin/pytest -q --maxfail=1`
  명령으로 실행했다. 건너뛴 시험은 성공으로 계산하지 않는다. Docker 5개는 위의
  opt-in 별도 실행이며, 위조 반례는 전체 회귀에 아직 포함되지 않은 추가 실험이다.
- 기존 Control 소스 변경 없음. 검사 과정의 fixture 컨테이너는 task-owned 이름으로 회수.

### 현재 실행 스크립트의 실제 도구 준비 상태

`data/p0-live-20261006/framework.env`를 현재 셸에만 로드해 MCP initialize/discovery와
P2 profile assembly를 확인했다. 대상 도구 실행·LLM 호출은 하지 않았다.

- 선택된 MCP 서버는 `recon-passive,web-http`, 발견된 원격 도구는 **11개**다.
- P2 local registry에 `evidence.assess`, `code.prepare`, `code.test` **3개**가 등록된다.
- Web 실행 역할의 원격 도구 조회는 11개, Mobile 실행 역할은 **0개**다.
  Mobile 역할 선언이 있다는 사실은 기기/모바일 MCP 실행 환경까지 연결됐다는 뜻이 아니다.
- 대화/Planner/Atomizer/Research/Code 정의에도 조회 metadata가 보일 수 있지만
  `schedulable=False`이고 실행 AgentSpec으로 등록하지 않는다. catalog 조회 가능성과
  대상 실행 권한을 혼동하지 않는다. `agent_profile.py` 조립과 `engine.py`의 spec 검사 경로를 확인했다.
- 이 실행 설정은 ZAP/Burp/브라우저/공개 검색/VDB 서버를 선택하지 않는다.
  다른 환경의 서버 설치 여부를 부정하는 것이 아니라 **현재 실행 catalog의 범위**다.

현재 설정은 P2 Web/Recon 로컬 시험용이다. 모든 역할·설치된 외부 MCP의 운영 준비가
완료됐다고 표현하지 않는다. Mobile 실제 실행은 원래 P3 단계에 남는다.

## 완료 판정과 다음 순서

1. F1을 capability runner 경계에서 수정·회귀로 검증한다. 중앙 Agent 이름별 예외 금지.
2. 알려진 만료 재평가의 범위를 확정하고 기존 원장/기동 경로에 연결·검증한다.
3. 같은 대상·catalog·ROE·모델·초기 상태의 P0/P1/P2 실제 비교를 승인된 예산 안에서 수행한다.

현재 판정은 **P0/P1 기존 회귀 유지 + P2 핵심 기능 연결됨 / P2 최종 완료 보류**다.
P3 확장이나 Finance Lab 정답 하드코딩부터 시작할 단계는 아니다.
