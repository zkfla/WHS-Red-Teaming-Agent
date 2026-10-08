# PRD v2 P2 — 로컬 기능 통합과 검증

> 2026-10-08 후속: 검수에서 재현된 fixture 판정 위조를 host oracle로 수정했고,
> 알려진 증거 만료의 bounded 재평가 기동을 기존 원장에 연결했다.
> [현재 후속 구현·검증·실제 모델 비교](p2-completion-20261008.md)를 참조한다.
> 아래 이전 테스트 수치는 당시 이력이며 현재 품질/출시 완료 판정을 대신하지 않는다.

작성: 2026-10-07. `WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`의
`dev-jm` 로컬 checkout, HEAD `ec1b8d9`에서 기존 P0/P1 변경을 보존하며 구현했다.
commit/push/PR, 다른 checkout 변경, 개인 실행 설정 변경은 하지 않았다.

판단 격리·동시성·예산 예약에 이어 주장별 재사용 조건과 독립 코드 검토·로컬
fixture 시험을 기존 조립/registry/store 경로에 연결했다. **로컬 기능 구현과 실제
LLM 품질/운영 출시 완료는 다르다.** 실제 모델 비교는 후속 문서에서 별도 구분하며
P3 대상 코드 실행은 미구현이다. 기본/P0/P1 모드는 바꾸지 않았다. 현재 선택
프로필은 version 6 / FixturePolicy v2이며 기존 Run의 fingerprint를 바꿔 재개하지 않는다.

## 1. 실제 연결한 흐름

```text
기존 위임 게이트와 역할 문맥 준비
  → 기존 Coordinator에서 Run 공통 판단 요청 예산 예약
  → 최대 2개의 격리된 모델 판단
  → 관련 대상·단계·전제조건 변화 검사
  → 기존 직렬 경로에서 제안 검증·로컬 결과 반영·행동 후보 생성
  → 기존 Scheduler / Control / MCP / 원본 증거 / Graph
```

모델 판단과 대상 도구 실행의 병렬성은 다른 설정이다. Planner/Atomizer와
증거 검토 단계 전체를 무조건 동시에 호출하지 않는다. 이번 fan-out의 대상은
이미 배정된 목적의 `worker_next_action`이다. 실행 배치 2는 P1 기능을 재사용한다.

## 2. 구현 위치와 책임

| 파일 | 변경 내용 |
| --- | --- |
| `orchestration/parallel_reasoning.py` | 모델 호출만 병렬화. 기존 Coordinator의 내부 레코드에서 예약/정산. 제안 반영 전 관련 상태 검사 |
| `orchestration/engine.py` | `fork_decision`으로 요청별 runtime·active delegation·token limit·event causation·client 격리. 판단 사건에 delegation/agent/step 귀속 |
| `orchestration/delegated_runtime.py` | 기존 직렬 흐름을 준비와 반영에 재사용. 명시한 설정이 1이면 기존 직렬 경로 유지 |
| `orchestration/settings.py` | 판단 동시성 기본 1, 최대 2. 병렬 설정에는 유한한 공통 요청 pool 필수 |
| `app/agent_profile.py` | durable 프로필·유한 pool 검증 및 기존 조립 경로에 설정 주입 |
| `config/agents/specialists-p2.yaml` | 명시적으로 선택하는 판단 동시성 2 / 실행 배치 2 / 요청 pool 64의 예시 프로필 |

새 queue·broker·DB를 만들지 않았다. 특정 랩 URL, 포트, 계정, 도구 이름이나
역할 ID로 중앙 실행 루프를 분기하지 않았다. 역할별 MD의 기존 버전/해시 로더를
유지하며 이번에는 MD 내용에 취약점 정답을 추가하지 않았다.

### 판단 격리

각 호출은 runtime과 delegation의 별도 복사본 및 별도 엔진을 가진다.
공유하는 것은 기존 ports/stores와 LLM 공급자다. 공급자의 요청별 usage 계약을
사용하며, 응답 순서와 관계없이 실제 요청·토큰·unknown usage를 각 목적에 귀속한다.
역할의 `max_concurrency`와 프로필의 공통 동시성 상한을 둘 다 적용한다.

병렬 phase에서는 action 실행, 경로 쓰기, local tool 적용, evidence 해석을 하지 않는다.
판단 관련 사건 발행은 허용하되 각 사건에 해당 delegation/agent/step을 연결한다.
결과를 받은 다음에만 기존 순차 반영 경로를 사용한다.

### 공통 예산

- pool은 **같은 Run의 전문 위임 판단 요청 총량**이다. Planner 등 모든 LLM 호출의
  전역 예산이라고 표현하지 않는다. 기존 다른 단계의 예산은 그대로 유지한다.
- 예약은 기존 위임의 남은 판단 한도를 확대하지 않는다. 정상 판단·repair와
  해당 turn의 후속 완료 검토는 같은 예약된 `DecisionBudget`을 사용한다.
- 실제 완료된 호출은 사용량만 소비하고 증명 가능한 미사용 예약분을 반환한다.
- 취소·crash로 모델 요청의 완료가 불명확하면 예약 상한을 계속 차감한다.
  재시작 시 자동 만료·재발급하지 않는다. 불명확한 비용을 0으로 취급하지 않는다.
- pool 소진 시 `specialist_decision_pool_exhausted`를 원본 진단으로 보존하고
  해당 위임을 종료한다. 같은 오류를 0회 사용량으로 계속 기록하지 않는다.
- engagement나 limit가 달라진 기존 pool 재사용은 거부한다. 프로필/프롬프트
  변경은 기존 fingerprint/resume 검사를 거쳐 새 Run에 적용한다.

Memory backend에서는 메모리 내 예약만 제공한다. 프로세스 재시작 내구성은
기존 SQLite Task/Path Coordinator를 함께 선택했을 때 제공하며, 예시 YAML의
`workflow: durable` 한 줄이 저장 backend를 자동으로 SQLite로 바꾸지는 않는다.

### 오래된 응답 검사

자신의 단계·직접 의존 단계·대상과 역할 문맥 관계·관련 원본 증거 digest·역할
정의와 catalog·mission·engagement 제약을 다시 읽어 fingerprint를 비교한다.
Graph 전체 revision 숫자나 관련 없는 형제 단계의 status 변화만으로 폐기하지 않는다.
자기 질문이나 근거가 달라졌다면 `specialist_decision_context_changed`로 제안을
거절하고 기존 유한 판단/재계획 경로에 넘긴다.

이 검사는 진행 중인 판단의 stale admission 검사다. 후속 통합에서 아래 6절의
주장별 현재 유효성 조회도 연결했다. 같은 대상의 실제 의미가 바뀌면 여러 관련
판단이 영향을 받을 수 있으나 무관한 주장과 원본은 보존한다.

## 3. 유지한 안전 경계

- 기존 TargetRef·principal/session·ToolId·입력 schema·exact step subject 검증.
- 기존 Control의 scope·승인·시간·쿼터. 판단 응답은 실행 허가가 아니다.
- 동일 session/browser/backend 등 실행 자원 충돌 처리와 승인 대기.
- 원본 ToolResult와 Graph/완료/claim receipt 소유 경로. 과거 원본 재작성 없음.
- mock 응답·도구 성공·코드 생성과 Finding/전체 목표 성공의 구분.
- 중단/mission admission 및 각 재시도 전 token/time 검증.

취소 시 진행 중 모델 tasks를 회수한다. 프로세스 밖의 공급자 요청 취소가
보장된다는 뜻은 아니므로 불명확한 예약은 보수적으로 보존한다.

## 4. P2-1 이전 검증 이력

아래 수치는 첫 P2-1 묶음 시점의 검증 이력이다. 현재 후속 통합의 검증은 7절과
구분한다. 두 경우 모두 실제 외부 LLM 비교 평가는 하지 않았다.

- `tests/unit/test_specialist_p2_reasoning.py`: 9개 통과. 교차 응답의 목적/토큰/사건
  격리, 관련 변화 거절, 무관한 형제 변화 보존, bounded repair 사용량,
  pool 소진 후 재호출 없음, 역할 동시성 1, 취소 회수, SQLite 재개·정산·초과 거절.
- `tests/integration/test_specialist_p1_mcp.py`: P1/P2 × 수동 배치/실제 LangGraph Send
  × 승인 필요/불필요의 8개 조합. 실제 stdio MCP·Control·일시적인 loopback HTTP 사용.
  승인 필요 시 대상 호출 0회, 승인 불필요 시 두 독립 서비스 호출의 barrier overlap,
  원본 증거·실행 횟수·미확정 Finding 상태를 검사한다.
- 통합 테스트의 모델은 명시적인 fixture다. 첫 배치까지 검증하며 전체 자연어 목표
  완료·실제 LLM의 판단 품질·승인 재개 전체를 이 한 테스트로 입증하지 않는다.
- 당시 전체 회귀: **2,985 passed / 51 skipped**, 288.73초. 건너뛴 시험은 통과로
  계산하지 않는다. 기존 모드 회귀와 위의 실제 로컬 MCP 연동 시험을 포함한다.
- CI 범위 Ruff: 통과. Mypy: **595 source files, no issues**. 기존 untyped body의
  annotation-unchecked 안내는 남아 있으며 타입 검사만으로 실행 정확성을 입증하지 않는다.
- `git diff --check`: 통과. branch/HEAD는 `dev-jm` / `ec1b8d9` 그대로다.

전체 회귀 명령은 프로젝트 루트에서 실행했다. 외부 MCP 선택을 비워 개인 설치
서버에 접속하지 않으며, 통합 시험 자체가 임시 loopback 서비스와 MCP를 조립한다.

```bash
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  ./.venv/bin/pytest -q --maxfail=1
./.venv/bin/ruff check src tests examples deploy/docker/mcp-example/server.py deploy/docker/finance-mock/server.py
./.venv/bin/mypy src/redteam tests
```

## 5. 선택과 롤백

프로젝트 루트에서 기존 승인된 engagement/MCP/LLM 설정을 준비한 **새 Run**에 적용한다.
이 명령을 이번 작업에서 사용자 세션에 적용하거나 실제 LLM을 호출하지 않았다.

```bash
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists-p2.yaml"
./.venv/bin/python -m redteam.app.cli
```

64는 명시한 예시 pool 값이지 모델 품질/운영 비용이 검증된 권장값이 아니다.
필요하면 프로필을 검토·변경하고 새 Run으로 시작한다. 모델 공급자의 요청별
structured response/usage 계약을 사용한다. usage 미제공은 unknown으로 기록하며,
token 한도가 있는 위임은 기존 안전 정지 규칙을 따른다. 실제 품질 평가 전
기본 프로필을 P2로 바꾸지 않는다.

P1로 복귀할 때는 새 Run에서 `specialists-p1.yaml`, P0는 `specialists.yaml`을
선택한다. 기존 Run의 저장 기록/증거를 지우거나 다른 fingerprint로 몰래 resume하지 않는다.

## 6. 후속 통합의 실제 연결

| 담당 경로 | 실제 구현/소비자 |
| --- | --- |
| `orchestration/evidence_validity.py` | `evidence.assess`를 기존 local-tool registry에 등록. 같은 Run/engagement의 성공 원본과 public claim receipt를 검사하고 재사용 조건을 SYSTEM 기록으로 덧붙임 |
| `orchestration/knowledge.py` | 기존 claim-level 원본 검증을 재사용한 `collect_measured_values`. 잘린 LLM 표시값이나 검증 후 다시 읽은 미입증 값을 조건으로 사용하지 않음 |
| `orchestration/completion.py` | 명시한 증거와 실제 사용될 최신 claim receipt의 현재 조건 검사. 오래된 receipt를 원본 ID로 우회 인용해도 완료 검토에 사용하지 않음 |
| `engine.py`, `delegated_runtime.py`, `parallel_reasoning.py` | core/worker 문맥, 해당 위임의 held fingerprint와 판단 admission에 현재 유효성 전달. 정확히 인용한 주장만 관련 변화 검사 |
| `reporting.py`, `app/cli/commands/report.py` | 기존 terminal report의 JSON/MD에 별도 현재 유효성 표시. 과거 Finding/원본을 삭제하거나 다시 쓰지 않음 |
| `orchestration/code_artifacts.py` | `code.prepare`에서 성공 원본을 바탕으로 작성 stage와 별도 역할의 검토 stage를 호출. `code.test`는 정확히 검토된 artifact/fixture/policy만 실행 |
| `core/ports/artifacts.py`, `core/ports/fixture_runner.py` | blob 및 network-free fixture 공개 port. 기존 filesystem artifact backend/호환 re-export 재사용. Agent가 Docker 구현을 생성하지 않음 |
| `capability/fixture_runner.py` | cached pinned image의 비특권 Docker에서만 고정 harness 실행. 대상 주소·임의 argv·호스트 경로·mount·network 옵션을 Agent가 전달할 수 없음 |
| `app/agent_profile.py`, `bootstrap.py` | opt-in field/stage/schema 검증과 기존 조립 경로의 port 주입. 기존 local registry를 복사해 선택 기능만 추가; 원래 구성에 등록을 유출하지 않음 |

### 증거 유효성의 정확한 범위

`evidence.assess`는 원본/claim receipt ID와 직접 측정된 조건 참조를 받는다.
예를 들어 세션 상태나 앱 버전이 실제 원본 관측을 통해 graph fact로 기록됐다면
그 key를 `unchanged` 조건으로 선택할 수 있다. 이름이나 특정 랩의 계정으로 판단하지 않는다.
명시적으로 관측한 timezone-aware 만료 시각은 `not_expired`로 검사하며 TTL을 추측하지 않는다.

평가 상태는 valid/stale/invalid/inconclusive이며 조건 없는 과거 기록은 unbound다.
unbound가 안전/인증 성공이라는 뜻은 아니다. 기존 원본/완료 검증은 그대로 적용한다.
valid도 **등록된 조건 아래의 재사용 가능성**일 뿐 모든 전제조건의 진실성이나
취약점/Control 허가를 보증하지 않는다. 어떤 조건이 그 주장과 관련 있는지는 연구
역할이 제안하고 코드는 해당 조건의 실제 출처를 확인한다.

binding은 기존 evidence store에 version 1로 보존한다. 동일 조건의 중복 capture는
새 기록/진전을 늘리지 않는다. 새로운 조건을 덧붙여도 오래된 조건을 지우지 않는다.
관련 주장이 stale이면 새 재관측·claim receipt로 다시 평가해야 하며 원본을 덮어써
valid로 만들지 않는다. 같은 원본의 별도 public claim receipt는 각각 조건을 가질 수 있다.
완료 검토와 코드 입력은 stale/invalid/inconclusive를 성공 근거로 거절한다.

refresh는 판단/held 상태 조회·완료 gate·fixture 입력 검사·보고 시 결정적으로
수행한다. 후속 `EvidenceExpiryWaiter`는 살아 있는 held 위임이 직접 인용한 알려진
만료만 기다린다. P2 예시는 Run 총 대기 예약 30초이며 action/decision/token/시간
한도를 초기화하지 않는다. 기존 Coordinator의 deadline/outbox와 held fingerprint로
OBSERVE에 복귀한다. 별도 timer service, 주기적 LLM 평가, 종료 Run 자동 재시작은 없다.

### 코드 작성·검토·시험

```text
기존 역할의 generic local tool 호출
  → 성공한 same-run 원본/현재 조건 확인
  → code_generation (설정에서 선택한 작성 역할)
  → canonical versioned bundle + SHA-256
  → code_review (다른 역할의 신뢰된 MD/출력 계약)
  → 기존 immutable artifact backend + SYSTEM 기록
  → 별도 code.test: scope/source/hash/fixture/policy/prompt 재확인
  → network-free Docker fixture → fixture_only SYSTEM 결과
```

두 모델 호출은 caller의 동일 DecisionBudget/guard를 사용하며 요청·토큰 소비는
기존 generic dispatch에서 집계한다. 자체 무제한 예산/새 작업 큐는 없다. 별도 역할의
검토이지 반드시 다른 모델 공급자라는 뜻은 아니며 모델 다수결을 실행 허가로 사용하지 않는다.

P2의 코드 계약은 Python **stdlib-only** `evaluate(JSON input) -> JSON output`와
synthetic cases다. 외부 dependency 설치, 네트워크/대상 접속, 실제 인증정보, target
argv, 일반 shell 실행을 받지 않는다. 이는 실제 대상 exploit runner의 대체가 아니다.
작성 모델이 fixture 기대값도 제안하므로 fixture 통과 자체가 독립 정확성 증명은 아니다.
바이트/인자/원본·목적 digest/Run/engagement/delegation/검토 prompt/정책이 바뀌면
이전 검토 바인딩을 거부한다. 변경은 새 artifact와 독립 검토를 필요로 한다.

코드는 호스트에서 AST parse만 하며 실행하지 않는다. 고정 digest image, `--pull=never`,
`--network none`, read-only root/fixture mount, non-root UID, cap-drop ALL,
no-new-privileges, 128MiB/0.5CPU/32PID, bounded stdout/stderr, 최대 30초 timeout을 강제한다.
mount는 해당 호출이 만든 임시 fixture 디렉터리뿐이다. Docker socket·홈·저장소를
컨테이너에 mount하지 않는다. timeout/취소/출력 초과 시 해당 UUID 컨테이너만 회수하며
`--rm` 경합에서도 제한 시간 내 없어졌는지 확인한다. Docker daemon 장애/컨테이너 런타임
취약점까지 방어를 보증하지 않으며 host execution으로 fallback하지 않는다.

실패/거절/missing backend는 명시적인 local-tool 오류 또는 fixture 상태다.
생성 artifact·코드 검토·fixture 성공은 TOOL 원본이나 실제 대상 Finding으로 승격하지 않는다.
source/validity/code 기록 계약은 [Evidence 계약](../contracts/evidence.md)에 정리했다.

### 장기 작업과 남은 출시 조건

기존 `LongJobAdapter`, WorkItem.job_id, lease heartbeat, 결과 조회/취소·unknown이면
재제출 금지 경로를 재사용하며 별도 job store나 실제 ZAP/Burp backend를 추가하지 않았다.
로컬 fixture는 최대 30초의 bounded 동기 작업이다. 신규 장기 backend가 필요한
실제 대상 실행은 기존 승인/원장 port를 통해 P3에서 조립해야 한다.

현재 개인 `.env`, engagement/MCP policy/실행 스크립트는 변경하지 않았다.
새 Run에서 예시 프로필을 선택하며 image를 사전에 검토·로컬 설치해야 한다.
image·경로·모델·키·대상·계정은 일반 실행 코드에 고정하지 않았다.

## 7. 후속 통합 검증

- source-grounded validity, 세션 상태/앱 버전 변경·실제 관측 만료, unknown/read 오류,
  동일 원본의 별도 주장 보존, 중복 capture·filesystem 재개·완료 gate·JSON/MD 보고.
- 실제 App 조립/registry와 generic engine local dispatch의 코드 작성→독립 검토→fixture
  기록 및 caller 요청 집계. 해시/목적/정책/원본/검토 prompt 변경·TOOL metadata 위장 거절.
- 기본/P1은 새 도구를 등록하지 않으며 raw ToolResult·Finding·target action count를 보존.
- 모델/단위 runner는 명시적인 fixture다. 실제 모델 품질을 시험한 것으로 계산하지 않는다.
- 실제 Docker 격리 시험은 별도로 5개 통과: egress/호스트 파일/read-only/non-root/capability/
  실제 cgroup memory·CPU·PID limits, 실패 구분, 무한 반복 timeout, 출력 한도, 취소 회수.
  최종 별도 실행: 5 passed, 5.83초. 이미지 다운로드·대상 스캔·실제 LLM 호출 없음.
- 최종 코드 전체 회귀: **3,009 passed / 56 skipped**, 277.44초. 외부 MCP 선택을 비운
  위 4절 명령으로 실행했다. 건너뛴 시험은 통과로 계산하지 않으며, opt-in Docker
  시험 5개는 위의 별도 실행 결과로 구분한다. 중단한 이전 검사 수치는 합산하지 않았다.
- CI 범위 Ruff 통과. Mypy: **602 source files, no issues**. 기존 untyped body의
  annotation-unchecked 안내는 남아 있다. 정적 검사만으로 운영 정확성을 보증하지 않는다.
- `git diff --check` 통과. task-owned `redteam-fixture-` 컨테이너 잔여 없음.
  branch/HEAD는 `dev-jm` / `ec1b8d9` 그대로이며 commit/push/PR은 하지 않았다.

```bash
env REDTEAM_TEST_FIXTURE_DOCKER=1 ./.venv/bin/pytest -q tests/integration/test_specialist_p2_fixture_docker.py
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  ./.venv/bin/pytest -q --maxfail=1
```

**당시 남은 검증은 실제 LLM 비교와 P3 운영 작업이었다.** 후속 승인과 실행 결과는
위의 최신 문서에서 같은 target/catalog/ROE/초기 상태의 P0/P1/P2 비교로 구분한다.
성공/blocked 이유, 원본과 무관한 Finding 여부, 모델 요청/토큰, 승인 대기, 중첩 시간과
실패 유형을 기록한다. 예산·도구·정책을 더 준 실험을 구조만의 성능 개선으로 보고하지 않는다.
현시점에는 개선율·Finance Lab 해결·실제 금융권 취약점 탐지·P2 전체 출시 완료를 주장하지 않는다.
