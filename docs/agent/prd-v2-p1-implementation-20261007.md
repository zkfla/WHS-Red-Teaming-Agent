# PRD v2 P1 — 로컬 구현 안내

작성: 2026-10-07. 대상은 현재 `dev-jm` 로컬 worktree다.
기준 P0는 `dev-orchestration`의 `6a2cba4`를 재통합한 버전이다.
이번 작업에서 fetch/pull/commit/push/PR은 하지 않았다.
Finance Lab의 서버·취약점·계정·ROE·개인 실행 설정도 변경하지 않았다.
기존 로컬 승인 연결 수정은 보존했다.

이 문서의 검사 수치는 초기 P1 구현 당시의 기록이다. 이후 푸시 전 검수에 따른
수정·최종 회귀 결과는 [검수 지적 수정 보고서](prd-v2-p1-review-fixes-20261007.md)를
확인한다.

## 1. 이번에 가능한 것

P0의 역할별 MD, Planner/Atomizer 분리, 목적 위임에 다음 기능을 연결했다.

- 명시적인 P1 프로필로 같은 활성 경로의 독립 실행 작업을 최대 2개씩 보낼 수 있다.
- Planner가 경로 변경을 제안하면 Planner → Atomizer → 기존 의미 검토 → 최신 상태/CAS
  검사를 거쳐 새 경로를 채택한다. 모델이 경로를 직접 덮어쓰는 지름길은 막는다.
- 관련 질문의 변경은 원자 작업 정의에 반영해야 한다. 무변경 재계획, 관계없는 작업의
  ID/정의/역할 변경은 거부한다. 영향 없는 완료 상태와 기존 증거 참조는 PathStore가 보존한다.
- 경로 저장과 `PathAccepted` 사건을 같은 coordination 트랜잭션에 기록한다.
  저장 직후 중단돼도 다음 판단 전에 변경된 분기의 미실행 WorkItem을 무효화한다.
- 동일 결과 재전달은 실행/재시도/모델 사용량을 두 번 집계하지 않는다.
  같은 action ID의 다른 결과는 덮어쓰지 않고 충돌로 보존하며 실행 오류로 처리한다.
- 한 배치에서 승인이 필요하면 기존 단일 승인 연결을 유지하고 아직 승인 검사를 받지 않은
  형제 작업은 같은 WorkItem으로 대기시킨다. 미승인 요청을 실행시키지 않는다.

이는 새로운 LLM 에이전트를 워커마다 생성하는 기능이 아니다.
Planner/Atomizer/실행 역할의 다음 행동 판단은 여전히 직렬이고,
판단 결과로 승인된 구체적 도구 호출만 기존 격리 워커로 병렬 실행한다.

## 2. 흐름과 책임

```text
사용자 요청·기존 Mission/Task 기록
  → Planner의 검증 질문
  → Atomizer의 작업 DAG·질문↔단계 대응
  → 구조·참조·의미 검토·CAS
  → PathStore + 같은 트랜잭션의 outbox 사건
  → 사건 cursor/lease/dedup으로 변경 작업 무효화·복구
  → Web/Mobile 역할의 도구 선택·인자 검증
  → 기존 WorkStore lease·의존·자원·예산 검사
  → Control admission → 격리 실행 워커 최대 2개
  → 원본 증거·Attack Graph → 중복/충돌 검사·집계
  → 기존 Research/Evidence 검토·재계획·종료 보고
```

별도 작업 DB, 외부 broker, 병렬 모델 판단 큐를 새로 만들지 않았다.
역할별 claim은 기존 WorkItem의 agent/path/delegation 정보와 lease를 재사용한다.
모든 역할이 독립 daemon으로 사건을 구독하며 상시 깨어 있는 구현은 아니다.

## 3. 병렬성과 안전 제한

P1 프로필은 `workflow: durable`, `worker_batch_size: 2`를 선언한다.
이는 기존 `OrchestrationSettings.max_actions_per_turn`에 명시적으로 적용하며,
전역 동시성 상한보다 크게 만들지 않는다. P0 프로필과 미선택 기본 모드는 그대로다.
프로필의 배치 설정이 선언된 경우 별도로 전달한 `max_actions_per_turn`보다 우선한다.
배치를 1로 시험하려면 별도 P1 프로필에서 `worker_batch_size: 1`을 선택한다.

Control의 승인·위험도·범위·쿼터, MCP 서버 동시성, 역할별 한도,
동일 대상 동시성은 해제하지 않는다. 배치 2는 반드시 호출 2개가 동시에 진행된다는 뜻이 아니다.

P1의 충돌 키는 실제 catalog와 인자에서 계산한다.

| 공유하는 것 | 처리 |
| --- | --- |
| 같은 MCP `server_id` | 직렬. 서버의 숨은 가변 자원에 대해 읽기 안전성을 추측하지 않음 |
| 같은 네트워크 authority, 즉 host와 port | URL/path가 달라도 직렬 |
| 같은 비네트워크 대상 identity | 직렬 |
| 동일한 `*_ref` 자원 인자 | 이름이나 서버가 달라도 같은 불투명 참조의 해시로 충돌 처리 |
| DAG 선행 작업 | 성공 전 후속 claim/호출 금지 |
| 전역/대상/역할/실행·재시도 예산 | 기존 제한을 함께 적용 |

GET 이름, retry-safe 선언, 낮은 위험도 하나만으로 자원 공유가 안전하다고 판단하지 않는다.
서버별 더 세밀한 read/write 자원 metadata는 아직 없으므로 같은 서버는 보수적으로 직렬화한다.
서로 다른 서버가 알려지지 않은 브라우저 프로필/기기/파일을 공유하는 배포를 안전하다고
자동 판정하지 않는다. 그러한 배포는 별도 자원 계약·검증 전 병렬 설정을 활성화하면 안 된다.
이번 실제 통합 검증은 서로 다른 backend와 서로 다른 authority의 합성 읽기 요청이다.

## 4. 저장·재전달·재시작

- `coordination.py`의 기존 원장/outbox를 사용한다. 새 내부 소비자의 cursor/effect 기록도
  같은 coordination records에 저장한다.
- public EventBus 전달 상태와 내부 경로 무효화 cursor는 별개다. 공개 전달 완료만으로
  내부 효과가 처리됐다고 가정하지 않는다.
- 처리권은 시간 제한 lease로 확보한다. 다른 소비자 소유, 잘못된 engagement binding,
  같은 dedup key의 상충 payload는 실패 원인을 보존하고 거부한다.
- WorkStore는 별도 트랜잭션이므로 효과 적용 후 ack 전에 중단되면 효과를 재전달한다.
  이 효과는 미실행 작업 무효화로 멱등적이다. **분산 exactly-once 보장이나 도구의 자동 재실행이 아니다.**
- 다음 모델 판단 전에 cursor를 복구한다. 외부 도구·LLM 호출은 원장 잠금을 잡은 채 하지 않는다.
- 한 소비 처리 상한은 200개 사건이다. 더 남으면 명확한 진단으로 중단하며 계속 실행하지 않는다.
- 단계 완료/미해결 기록도 `PathStepSettled` 사건을 원장에 남긴다.
  역할의 완료 제안만으로 Mission 성공을 선언하지 않는다.
- RuntimeState에 additive 기본값인 결과 receipt/충돌 필드를 추가했다.
  이전 checkpoint는 빈 값으로 읽힌다. 충돌 해시는 종료 보고에도 전달한다.
- 변경한 프로필/프롬프트는 fingerprint가 달라지므로 이전 P0 Run에 P1을 끼워 넣어 resume하지 않는다.

## 5. 변경 위치

| 파일 | 책임 |
| --- | --- |
| `config/agents/specialists-p1.yaml` | 명시적 P1 workflow/배치 선택과 초기 역할 동시성 |
| `app/agent_profile.py`, `app/bootstrap.py` | 설정 검증·선택된 기존 런타임 조립, P0 유지 |
| `orchestration/specialist_planning.py` | 2단계 재계획·영향 없는 단계 보존·무변경 거부 |
| `orchestration/engine.py`, `path_events.py` | 경로/단계 저장과 사건 원자 기록·판단 전 복구 |
| `orchestration/outbox.py` | 기존 outbox 내부 consumer cursor/lease/dedup |
| `orchestration/resource_conflicts.py` | provider 이름별 분기 없는 보수적 자원 충돌 키 |
| `orchestration/dispatch.py`, `nodes/aggregate.py` | 순서 독립 결과 병합·중복 계상 방지·충돌 보존 |
| `orchestration/state.py`, `reporting.py` | receipt 저장·기존 checkpoint 호환·보고 투영 |
| `tests/unit/test_specialist_p1.py` | 재계획/원장 crash/cancel/중복/충돌/lease 검사 |
| `tests/integration/test_specialist_p1_mcp.py` | fixture 판단 + 실제 Control/stdio MCP/로컬 HTTP 병렬 시험 |

기존 WorkStore/Scheduler, 격리 worker, Control, graph/evidence 경로를 재사용했다.
Finance Lab, MCP 정책, 계정 등록, Core 계약과 Control 코드에는 이번 P1 변경이 없다.

## 6. 로컬 활성화

현재 수정한 프로젝트 루트에서:

```bash
source .venv/bin/activate
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists-p1.yaml"
python -m redteam.app.cli
```

기존의 승인된 engagement와 MCP 실행 설정, 실제 LLM 설정은 계속 필요하다.
새 Run을 시작한다. 이전 실행 데이터를 삭제하거나 승인 정책을 완화할 필요는 없다.
개인 시작 스크립트에서 P0 프로필을 다시 export하면 위 선택이 덮어써지므로 실제 실행 설정을 확인한다.
이번 작업은 개인 `.env`나 `data/p0-live-20261006/start.zsh`를 자동으로 변경하지 않았다.

CLI의 durable 실행 경로는 기존 SQLite backend를 사용한다.
라이브러리에서 memory backend를 명시적으로 선택하면 프로필 이름이 durable이어도
프로세스 종료 후 저장 상태를 보장하지 않는다. durable 저장은 실제 backend 설정으로 결정된다.

P0로 돌아가기:

```bash
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists.yaml"
```

기본 모드로 돌아가려면 `unset REDTEAM_SPECIALIST_PROFILE`.
이는 새 실행의 선택이며 진행 중인 Run의 프로필 변경 명령이 아니다.

## 7. 검증과 한계

명시적인 scripted LLM fixture와 로컬 임시 저장소를 사용했다.
실제 LLM, 금융 사이트, 기존 Finance Lab, 설치된 ZAP/Burp에는 요청하지 않았다.
통합 시험의 MCP transport/Control/HTTP 호출은 실제 구현이다.

```bash
env -u REDTEAM_SPECIALIST_PROFILE REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  .venv/bin/pytest -q tests/unit/test_specialist_p1.py tests/integration/test_specialist_p1_mcp.py
```

두 HTTP handler의 barrier로 두 요청이 동시에 in-flight인 것을 확인한다.
승인 필요 사례는 요청 0회, 대기 승인 1건, 형제 WorkItem 대기를 확인한다.
기존 승인 재개·중단·원장/경로 트랜잭션·부분 결과·checkpoint·CLI 회귀도 함께 검사한다.
아래는 최종 코드의 로컬 검사 결과다.

- P1 집중 검사: **21 passed**. Planner/Atomizer 재계획, 실제 MCP 독립 읽기 2개,
  미승인 대상 요청 0회, cursor crash/cancel 복구, 결과 중복/충돌,
  P1 사건/종료 보고 순서를 포함한다.
- 전체 회귀: **2,953 passed / 51 skipped**, 266.31초. 건너뛴 검사는 통과로 계산하지
  않았으며 실제 외부 LLM·외부 금융 대상·운영 Mobile 환경 검증 완료를 의미하지 않는다.
- 정적 검사: CI 범위의 Ruff 통과, Mypy `src/redteam tests` **592 source files** 통과,
  `git diff --check` 통과. Python **3.11** 로컬 검증이며 다른 Python 버전은 실행하지 않았다.

첫 전체 검사에서 기본 모드의 사건 순서/최소 의존성 fixture와 결과 채널 ID fixture 문제가
발견됐다. 내구성 경로 사건·복구는 P1 선택 시에만 연결해 기본 모드를 보존했고,
P1 사건이 종료 보고 뒤에 늦게 전달되는 문제도 회귀 시험으로 확인했다.
결과 채널 시험은 실제 action ID를 키로 사용하도록 수정했다. 잘못된 ID와 상충 결과를
허용해서 통과시킨 것이 아니라 별도 거부 검사를 유지했다.

아직 하지 않은 것:

- P2의 전문 역할 판단 동시성, 요청별 가변 엔진 상태 격리, 주장별 최신 유효성 원장,
  코드 artifact 작성/검토/로컬 sandbox와 P3의 실제 Mobile/승인 runner.
- 모든 역할의 독립 상시 event subscriber, 여러 MissionTask 동시 실행, 다중 승인 큐.
- 실제 LLM의 탐지율·비용·속도 향상 측정, 특정 Finance Lab 취약점 해결 보장.
- 알 수 없는 공유 자원이나 arbitrary mutation의 병렬 안전성 보장.

따라서 P1 실행·복구 연결의 로컬 검증과 PRD v2 전체 완료를 구분해야 한다.
단일 서버/단일 대상의 종속 조사에서 병렬성이 생기거나 기존 `investigation_held` 원인이
자동으로 사라진다고 보장하지 않는다.
