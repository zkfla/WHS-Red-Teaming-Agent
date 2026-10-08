# PRD v2 P2 후속 구현·검증 — 2026-10-08

최신 판정은 **§5**다. P2 로컬 구현의 병렬 완료·목표 정산 경로는 검증했으나,
실제 모델 품질 게이트는 미통과다. 최신 재시험까지 승인된 호출은 누적 **81/100회**
사용했고 P2는 공급자 HTTP 429로 계획 생성 단계에서 막혔다. §1–4는 첫 비교와
이전 수정의 시점별 기록이며 당시 수치를 최신 누적 수치로 덮어쓰지 않았다.

작업 위치: `WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`.
브랜치/기준 커밋: `dev-jm` / `ec1b8d9` + 기존 미커밋 P0/P1/P2 변경.
이번에도 fetch/pull/checkout/commit/push/PR은 하지 않았다. 개인 실행 설정·기존
launcher·Finance Lab·Control 정책을 바꾸지 않았다. 이 파일은 PRD나 이전 검수의
“구현 예정”을 사실로 옮긴 문서가 아니라 실제 수정과 검증의 후속 기록이다.

## 1. 완료한 구현

### 생성 코드가 자기 시험 합격 숫자를 위조하던 오류

수정 전 후보 모듈이 `passed=1,total=1` JSON을 출력하고 조기 종료하면 함수가
실행되지 않아도 runner가 합격으로 처리했다. 수정 후 Docker에는 source와 **입력만**
보내며 기대값은 호스트에 남긴다. 컨테이너의 제한된 JSON 출력값을 호스트가
canonical JSON으로 비교해 counts/verdict를 계산한다. `true`와 `1`도 다르다.
가짜 합격 요약·누락/추가 출력·잘못된 JSON·비정상 종료는 합격이 아니다.

경계: `core/ports/fixture_runner.py` → App 주입 → `capability/fixture_runner.py`.
FixturePolicy는 **v2**, 최종 P2 선택 프로필은 **version 6**이다. 기존 검토/Run의
정책 해시·프로필 fingerprint를 바꿔 몰래 재사용하지 않는다.

Docker의 pinned cached image / pull 금지 / network-none / read-only / non-root /
cap-drop / CPU·memory·PID·시간·출력 제한 및 task-owned 컨테이너 회수는 유지했다.
일반 shell/MCP allowlist를 늘리거나 생성 코드를 호스트에서 실행하지 않았다.

한계: 임의 프로그램이 실제 출력값을 맞췄다는 것은 유한한 black-box fixture 검사다.
특정 함수가 실행됐다는 attestation, 일반 알고리즘의 정확성, 실제 취약점 증명이 아니다.
작성 모델이 기대값도 제안하며 독립 검토는 완전한 oracle 검증이 아니다. 성공도 여전히
SYSTEM / `fixture_only=true`이고 원본 ToolResult·Finding·대상 실행 허가가 아니다.

### 알려진 증거 만료가 관련 위임의 재평가를 깨우는 연결

`evidence_expiry.py`가 명시적으로 관측한 `not_expired` 조건만 검사한다.
소비자는 generic `ConditionWaiter`이고 중앙 루프에 특정 tool/server/role ID 분기를
추가하지 않았다. 선택한 App profile이 구현을 주입하며 P0/P1 기본 동작은 유지한다.

흐름:

```text
성공한 원본 → evidence.assess binding → 관련 held 위임의 직접 인용
  → 기존 Coordinator에 deadline·총 대기 예약 기록
  → 시간 도달 + 원본/조건/mission/path 재검사
  → 같은 원자 변경에서 timer fired + outbox EvidenceValidityExpired
  → REPLAN이 종료 대신 OBSERVE로 복귀
  → 기존 held fingerprint 소비자가 관련 위임만 재평가
```

P2 예시의 총 대기 예약은 Run당 30초다. 이를 매번 새로 주지 않으며 취소/crash 때도
예약을 보존한다. 실제 시간·ROE·위임 action/token/decision 잔여량·step 상한을
넘겨 기다리지 않는다. 대기 중 LLM/대상 도구 호출은 없고 운영자 stop/pause가 오면
기존 OBSERVE 입력 처리로 복귀한다. 만료를 모르거나 조건/출처가 바뀌면 추측하지 않는다.

같은 binding/위임/경로의 사건은 하나다. 이전 held checkpoint에서 crash 복구하면
그 사건을 재사용하고, 재평가 후 새 held fingerprint에서 같은 사건이 또 모델을
깨우지 않는다. 원본·Finding·Graph를 삭제/재작성하지 않는다.

새 queue·broker·DB·상시 timer 서버는 없다. 기존 runner의 `EventWakeup`은 이미
조용해진 Run의 외부 사건 조정을 담당한다. 이 provider는 종료 전에 알려진 만료만
유한하게 기다리는 접점이다. 끝난 Run을 자동 재시작하거나 모든 증거를 주기적으로
LLM에 보내지 않는다. 장기간 만료를 상시 감시하는 운영 서비스는 구현 범위가 아니다.

### 실제 비교에서 드러난 증거·Control 문맥 전달 공백

P1 모델은 `unbound`를 원본 검증 불가로 해석하고 Control에게 증거 유효성 확정을
요구했다. 그러나 기존 완료 검증 코드는 `unbound` 원본에 대해 원래의 출처·실행·
주장 검사를 계속하며, 모든 관측에 새로운 재사용 binding을 강제하지 않는다.
이 사실을 `current_validity`의 반환 문맥과 local tool 설명에 명시했다.
`unbound`를 `valid`로 바꾸거나 실패·만료 검사를 없앤 수정이 아니다.

P2 모델의 보류 기록에는 기존 Control admission 기록 부족이 명시됐다. 실제 audit에는
해당 두 action의 ALLOW가 있지만 `delegated_runtime.py`가 전체 런타임의
`runtime_observations`를 워커에게 전달하지 않았다. 이제 소유 위임의 work 또는
직접 인용한 같은 Run/engagement 원본 action으로 제한해 기존 기록을 전달한다.
완료 검토도 인용한 evidence의 action 기록과 유효성 문맥만 받는다. 다른 형제의
ALLOW·사건은 자동으로 전달하지 않고 기존 secret 마스킹을 재사용한다.

과거 action의 ALLOW는 **새 action의 허가가 아니다**. Control·scope·원본·Finding·
완료 게이트는 그대로다. 기록이 bounded window에서 빠졌다는 사실도 DENY나
실행 부재를 증명하지 않는다. 표본에서 요구한 request method 증빙 등 모든 모델의
미해결 주장까지 이 수정만으로 해결됐다고 보장하지 않는다.

실제 비교는 이 보완 전 profile v5로 실행했고, 보완 후 profile v6는 fixture 회귀로
검사했다. HTTP 429가 관측돼 실제 모델을 다시 호출하지 않았다. 따라서 **최종 코드의
실제 모델 목표 완료는 아직 미검증**이다.

## 2. 검증 구분

- 실제 Docker: **10 passed**, 9.07초. 이전 위조 두 사례·oracle 파일 비노출·JSON
  boolean/number 구분·부분 합격 counts와 기존 격리/timeout/output/cancel 확인.
- 실제 MCP/Control/임시 HTTP 통합: **8 passed**, 25.31초. P1/P2 × isolated/LangGraph
  × 승인 유무. 이 시험의 LLM은 fixture이며 실제 모델 품질 시험과 구분한다.
- 증거 만료: memory/SQLite 재개·원본 보존·관련성·예약·취소·stop·하드 상한·실제
  held 소비자의 1회 재평가·예산 끝난 형제의 비간섭을 단위/통합 경로로 검사했다.
- 평가 모델 호출 계량: 실제 호출 전에 병렬 예약, trial 변경으로 전체 상한 초기화
  금지, 실패도 차감, unknown usage를 0으로 기록하지 않음을 별도 fixture로 검사했다.
- 후속 관련 검사: **60 passed**, 5.04초. 위 만료/코드/격리와 `unbound`의 성공·실패
  원본 구분, 직렬/병렬 워커의 action별 Control 문맥, 완료 검토의 무관 승인 제외·
  마스킹·입력 불변성, 비교 계량을 포함한다. 이때 LLM은 명시적인 fixture다.

최종 코드 전체 회귀: **3,032 passed / 61 skipped**, 274.35초 (Python 3.11).
앞선 중간 전체 회귀 3,025개와 합산하지 않는다. 건너뛴 테스트는 성공이 아니다.
GitHub CI나 Python 3.10/3.14 전체 회귀를 실행했다는 뜻도 아니다.

- CI 범위 Ruff 통과: `src tests examples`와 두 Docker server 파일.
- CI 명령 Mypy 통과: `src/redteam tests`, 606개 source 파일. 기존의 untyped-body
  안내 note는 남아 있으며 모든 함수 본문의 strict typing 검사를 했다는 뜻은 아니다.
- `git diff --check`, 기존 P2 launcher의 `zsh -n`, 로컬 문서 링크 검사 통과.
- 최종 상태 조회 때 Docker 데몬에는 연결되지 않았다. 위 10개는 데몬 가동 중
  완료한 실제 시험 기록이며, 그 이후 runner 코드를 바꾸지 않았다. 현재 데몬이
  켜져 있다거나 최종 시점의 컨테이너 상태까지 조회한 것으로 표현하지 않는다.

재현 명령 (현재 checkout 루트):

```bash
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  ./.venv/bin/pytest -q --maxfail=1
./.venv/bin/ruff check src tests examples deploy/docker/mcp-example/server.py deploy/docker/finance-mock/server.py
./.venv/bin/mypy src/redteam tests
```

Docker를 켜고 프로필의 고정 image가 로컬에 있는 경우에만 다음 격리 시험을 실행한다.
image 자동 pull·실제 대상 접근·LLM 호출을 포함하지 않는다.

```bash
REDTEAM_TEST_FIXTURE_DOCKER=1 ./.venv/bin/pytest -q tests/integration/test_specialist_p2_fixture_docker.py
```

## 3. 실제 모델 비교의 조건과 한계

사용자 허용: 현재 모델, 최대 100회, 추가 비용 0달러.
`examples/specialist_live_comparison.py`는 현재 `.env`의 모델을 읽고 기존 SDK resolver가
ChatGPT 구독 경로인 경우만 실행한다. SDK의 구독 endpoint를 명시하고 재시도 0,
유료 API/다른 모델 fallback 없음, 최대 총 100회·trial당 30회 예약을 적용한다.
추가 결제·크레딧 구매·사용량 reset을 실행하지 않았다.

구독도 포함 사용량을 소모하며 제공자 금액/토큰이 미보고되면 **unknown**이다.
금액을 계측해 실제 청구서의 0달러를 확인했다는 뜻이 아니다. 계정의 무료 포함
사용량을 넘겨 유료 계정이나 API로 계속하는 기능은 이 스크립트에 없다.
계정 확인 당시 Plus 포함 사용량 사용 가능, 구매 크레딧 잔액 0이었다.
이는 Codex 앱에 로그인한 계정 조회 결과이며, LiteLLM의 OAuth 계정과 동일하다는
확인이나 청구서 검증을 대신하지 않는다.
[OpenAI의 공식 사용량·크레딧 설명](https://learn.chatgpt.com/docs/pricing)을 확인했다.

환경은 Finance Lab과 다른 임시 loopback HTTP 서비스 2개다. 모든 응답은 합성이며
목표는 원본 응답 상태/가시적 보안 헤더를 관측·비교하는 것이다. 취약점 정답, 계정,
로그인 우회 경로를 모델에 주지 않았다. scope는 동적으로 할당된 로컬 host:port와
`GET /`만 포함한다. 같은 catalog·ROE·목표·초기 서버 상태를 P0/P1/P2에 적용하고,
각 Run에는 새 Graph/evidence/control 원장을 사용한다.

### 첫 비교 실행 결과 (문맥 보완 전 P2 v5)

실제 모델은 `chatgpt/gpt-6.1-sol`, 프로필마다 1회다. 총 **43회** 모델 요청 중
38회 응답 수신, 5회 provider failure였다. 실패 호출도 한도에서 차감했다.
금액·토큰 계량은 제공되지 않아 unknown이며 청구 0달러를 확인한 것은 아니다.
아래 2 action은 각 Run에서 서로 다른 두 서비스의 `GET /` 실제 MCP 실행이다.

| 프로필 | 모델 호출 | 대상 action | 걸린 시간 | 종료 | 목표 |
| --- | ---: | ---: | ---: | --- | --- |
| P0 | 15 | 2 | 270.932초 | `stalled_no_graph_progress` | blocked |
| P1 | 13 | 2 | 269.748초 | `investigation_held` | blocked |
| P2 (v5) | 15 | 2 | 181.076초 | `planning_error` | blocked |

세 Run의 catalog hash는 동일했다:
`85ff8a8c06f10a2924f0d7786117eb892662bcc1fa621ea6ced989856123fcdf`.
초기 조건 hash는
`9a8a402162a937777ee8947b8b1fe97e38cbf197fe259b3241871e1654950cd0`다.
세 Run 모두 Finding 0개이며 없는 취약점을 성공으로 만들지 않았다.
P2의 짧은 시간은 실패로 조기 종료한 시간이지 성능 향상 증거가 아니다.

막힌 지점:

- P0: 모델이 원본의 mock 여부/당시 정책 증빙 공백을 미해결로 남겼다.
- P1: 모델이 원본 `unbound` 상태를 최종 연결 검증 불가로 해석했다.
- P2: 두 원본 관측 후 Planner가 추가 증거를 읽으려는 `next_action`에서 공급자가
  HTTP 429로 거절했다. 해당 진단에는 4회 rate-limit 실패와 repair 예산 소진이
  기록됐다. 전체 모델 요청 상한 100회 소진이나 MCP 접속 실패가 아니다.

공급자 제한이 요청률/계정 quota 중 어느 종류인지 이 기록만으로 단정할 수 없다.
429를 판단 예산 확대·유료 API 우회·계속 재시도로 해결하지 않았다.

이것은 실제 모델 → Agent → Control → MCP → HTTP의 **smoke 비교**다.
HTTP/HTTPS·인증·승인/재개·장기 작업·다른 취약점 환경을 각각 5회 이상 검증한
품질 평가가 아니다. 속도/품질 개선율과 p50/p95를 이 표본으로 주장하지 않는다.
PRD §15.2의 전체 품질 회귀 없음/v2 출시 게이트와 Mobile·대상 코드 실행(P3)은 별도다.

### 완료 판정과 남은 조건

P2의 로컬 기능·안전 경계 보완과 실제 모델 **비교 시도**는 수행했다. 하지만
최종 코드가 실제 모델로 목표를 완수한다는 조건은 입증하지 못했다. 그래서
**P2 운영 품질/완전한 v2 출시 완료 판정은 보류**한다.

1. 공급자 제한 해제와 포함 사용량을 확인한 뒤, 남은 승인 한도 내에서 v6의
   동일 조건 재시험. 이번 승인에서 이미 쓴 43회를 새로 0회로 초기화하지 않는다.
   추가 유료 API를 사용하지 않는다.
2. 원본 내용·출처·같은 action의 Control 이력을 모델이 정확히 활용하는지,
   성공한 관측을 위임 → 단계 → task → goal 정산으로 연결하는지 확인.
   추가 조사 없이 충분한 원본은 활용하되 실패·stale·fixture를 성공으로 넘기지 않는다.
3. PRD §15.2의 다른 환경/승인/재개/인증/장기 작업 조건과 반복 표본 평가.
   그때 품질·오탐·unknown usage·실패 원인과 baseline 차이를 비교한다.

## 4. 현재 적용/실행

새 Run에만 선택한다. 기존 profile fingerprint가 다른 Run은 그대로 resume하지 않는다.

```bash
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists-p2.yaml"
```

기존 사용자용 launcher는 보존했다. 현재 checkout의
`data/p0-live-20261006/start-p2.zsh`가 기존 실행 설정을 쓰면서 P2를 선택한다.
P2 선택만으로 미설치 Mobile/ZAP/Burp/브라우저 서버가 자동 설치되지는 않는다.

보고서 원본: `data/p2-live-comparison-20261008-v2/results.json`.
초기 `data/p2-live-comparison-20261008`는 사전 hash 직렬화 오류로 모델 호출 전에
중단됐고, 코드 수정 후 별도 디렉터리에서 새로 실행했다. 두 기록을 합산/덮어쓰지 않았다.

## 5. 병렬 완료 충돌 수정과 최종 재시험

### 5.1 직접 재현한 문제와 수정 경계

서로 독립인 두 워커가 같은 경로 상태에서 판단한 뒤 순서대로 완료하면,
첫 완료가 저장 상태를 바꾼다. 두 번째 완료 검토가 배치 시작 당시 경로를
사용하면 `StateConflict: completion_path_snapshot_changed`가 발생했다.
실제 병렬 판단과 기존 완료 검토 코드를 사용하는 회귀 테스트로 수정 전에 재현했다.
이는 모델에게 취약점 정답이 없어서 생긴 문제가 아니라 상태 전달의 구현 오류다.

`src/redteam/orchestration/parallel_reasoning.py`에서 반영 직전에 최신 canonical
경로를 읽도록 수정했다. Run/path/version/mission/task binding과 해당 위임의
관련 상태가 그대로인 독립 응답만 최신 진행 상태로 완료 검토한다.
다른 경로·버전·task로 바뀌었거나 관련 상태가 바뀐 응답은
`specialist_decision_context_changed`로 거절한다.

기존 완료 검토의 review token과 원자적 CAS는 유지했다. 검토 도중 진짜 상태가
바뀌면 여전히 거절한다. 형제 완료를 이유로 모델 응답을 재호출하거나 대상 요청을
재실행하지 않는다. 특정 Lab/경로/도구 이름 분기, Control 완화, 예산 증액,
원본 증거·Finding·Graph 재작성은 추가하지 않았다.

추가 회귀는 memory/SQLite 저장소와 두 반영 순서에서 모두 완료되는지,
SQLite 재개 결과가 같은지, path/version/task 변경 응답이 거절되는지를 검사한다.
원본 증거 보존·요청 정산·재실행 없음도 함께 확인한다.

### 5.2 전체 완료 흐름의 검증

`tests/integration/test_specialist_p1_mcp.py`에 P1/P2 각각 다음 흐름을 추가했다.

```text
App → 전문 워커 판단 → Control → 실제 stdio MCP → 임시 HTTP 서비스 2개
    → 원본 ToolResult 저장 → 두 단계 완료 → required task 정산
    → mission/goal 완료 → 저장 보고서 goal_achieved
```

모델은 명시적인 fixture다. HTTP 서비스의 barrier로 두 요청의 실제 겹침을
확인하고, 두 GET·두 action·성공한 원본 두 개를 인용한 task 영수증·목표 완료를
검사한다. Finding은 0개다. 합성 관측 목표 완료를 취약점 발견으로 바꾸지 않는다.
보고서는 테스트 소유 임시 디렉터리에 저장하며 기존 사용자 Run을 수정하지 않는다.
이 시험은 연결과 정산 구현의 검증이지 실제 모델의 자율 조사 품질 증명이 아니다.

### 5.3 최종 코드 검증 결과

| 검사 | 실제 결과 | 구분 |
| --- | --- | --- |
| 병렬 판단·완료·MCP·원자 검토 관련 회귀 | 48 passed, 35.56초 | fixture 모델 / 실제 MCP·HTTP |
| 전체 테스트 | 3,055 passed / 61 skipped, 293.54초 | 명시적 외부 연결 설정 제거 |
| 실제 Docker fixture 격리 | 10 passed, 9.12초 | cached pinned image, 자동 pull 없음 |
| 후속 비교 계량 테스트 | 9 passed, 재확인 2.10초 | 전체 실행 뒤 추가한 6개 사례 포함 |
| 후속 P1/P2 보고서 저장까지 검증 | 2 passed, 재확인 7.90초 | 전체 실행 뒤 추가한 보고서 assertion 포함 |
| Ruff | 통과 | src/tests/examples 및 기존 CI의 Docker 서버 범위 |
| Mypy | 통과, 607 source files | 기존 unchecked annotation 안내는 유지 |
| `git diff --check` | 통과 | 기존 dirty 변경을 보존 |

전체 3,055개 실행은 마지막 6개 계량 사례와 보고서 저장 assertion 추가 전이었다.
최종 추가분은 위 후속 검사로 확인했다. 전체 3,061개를 재실행했다고 주장하지 않는다.
Docker 시험은 네트워크 없음·비루트·읽기 전용·자원/시간/출력 제한과 취소 시
컨테이너 정리를 검사하며 실제 대상에 생성 코드를 실행하지 않는다.
후속 재확인의 첫 시도는 sandbox가 loopback socket bind를 막아 setup 오류가 났다.
이번 턴의 네트워크 권한을 받은 뒤 같은 코드·명령으로 2개 모두 통과했다.
이 setup 오류를 제품 코드 실패나 통과 기록으로 계산하지 않았다.

재현 명령은 §2와 같고 후속 검사만 별도로 다음을 사용했다.

```bash
./.venv/bin/pytest -q tests/unit/test_specialist_comparison_meter.py
./.venv/bin/pytest -q tests/integration/test_specialist_p1_mcp.py -k full_loop
```

### 5.4 최종 실제 모델 비교 — 목표 완료는 미입증

최종 runtime 수정 후 현재 모델 `chatgpt/gpt-6.1-sol`로 같은 로컬 읽기 전용
조건의 P0/P1/P2 비교를 다시 실행했다. 각 프로필 1회이므로 품질 통계가 아니다.
첫 비교의 43회를 `--prior-results`로 이월해 잔여 57회만 허용했고,
프로필당 19회 상한·추가 API 비용 상한 0·유료 fallback 없음으로 실행했다.
이번 호출은 **38회**, 누적 **81/100회**다. 이번 19회는 응답을 받았고
19회는 `LLMProviderFailure`였다. 기록되지 않은 토큰/금액은 unknown이다.
구독 사용량을 소비했으며 실제 청구서의 0달러를 계측했다는 뜻은 아니다.

| 프로필 | 이번 모델 호출 | 대상 action | 시간 | 종료 | 목표 |
| --- | ---: | ---: | ---: | --- | --- |
| P0 | 15 | 2 | 207.490초 | `stalled_no_graph_progress` | blocked |
| P1 | 19 | 0 | 100.086초 | `stalled_no_graph_progress` | blocked |
| P2 v6 | 4 | 0 | 5.143초 | `planning_error` | blocked |

P0의 관측 두 단계는 성공 완료됐지만 후속 증거 검토가 mock 여부·당시 정책
증빙 등의 공백을 남겨 required task가 blocked였다. 헤더 값·브라우저에서의
보안 효과처럼 실제 관측하지 않은 것은 취약점/성공으로 추정하지 않았다.
이 보류 전부가 타당한 필요 조건인지 또는 과도한 모델 요구인지는 추가 품질
분석 대상이며, Control을 약화하거나 완료를 강제하는 근거가 아니다.

P1은 경로 단계가 pending인 채 모델 호출을 소모했고 대상 요청은 없었다.
P2는 `attack_path_proposal`에서 **4회 HTTP 429**가 발생해
`decision_repair_budget_exhausted`로 종료됐다. 경로가 생성되지 않았고 MCP
action도 없었다. 이는 앞에서 수정한 완료 snapshot 충돌이나 MCP 연결 실패가
아니다. 빠른 실패 시간을 병렬 성능 향상으로 계산할 수 없다.

세 catalog hash는 첫 비교와 동일하다. 첫 비교/이번 비교의 개별 보고서는 보존했다.
최신 원본은 `data/p2-live-comparison-20261008-snapshot-fix/results.json`이다.
스크립트는 이전 보고서의 누적 사용량을 검증하고 새 RunId에 출력 디렉터리를
포함해 이전 평가 Run 보고서를 덮어쓰지 않는다.

### 5.5 남은 게이트와 진행 조건

**P2 로컬 구현·안전·완료 연결 검증은 수행했다. P2 실제 모델 품질 및 전체 v2
출시 완료 판정은 아직 보류다.** HTTP 429의 정확한 요청률/계정 quota 원인은
현재 결과만으로 단정하지 않는다. 남은 19회가 있다는 이유로 연속 재시도하지 않는다.

1. 공급자 제한과 포함 사용량이 해소된 후 기존 승인 잔여량 안에서 실제 모델
   목표 완료·위임/단계/task/goal 연결을 재검증한다. 누적 81회를 초기화하지 않는다.
2. PRD §15.2의 HTTP/HTTPS·인증·승인/재개·경로 제한·장기 작업·별도 환경
   평가에서 품질 회귀/오탐/미해결 판단과 호출량을 비교한다. 이번 smoke로 대체하지 않는다.
3. 실제 모델이 충분한 원본과 같은 action의 admission 기록을 활용하는지
   확인한다. fixture·실패·stale 결과를 실제 성공 증거로 채택하지 않는다.

개인 `.env`, 기존 실행 launcher, Lab의 취약점/계정/허용 정책과 사용자 Run은
보존했다. GitHub push/PR, commit, 원격 변경은 하지 않았다.
