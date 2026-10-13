# v3 Planner / Atomizer 역할 분리 ExecPlan

## Status

PLANNING — 기준 dev `a8aafe39`, 사용자 작업폴더에서 구현. `AGENTS.md`와 `.agent/PLANS.md`를 읽고 적용한다. root가 계약·통합·최종 판단을 소유하고 DeepSeek 4.1 High orchestrator가 Muse 1.3 High의 충돌 없는 작업을 조정한다. 독립 리뷰는 Opus 4.8 High 및 AGY Gemini 3.8 Flash High를 사용한다.

## Goal

v3의 가설·증거·probe·병렬 워커 기능을 유지하면서 전체 전략을 제안하는 Planner와 작업을 분해하는 Atomizer를 명확히 분리한다. 기존 원자화 기능을 v3의 증거·가설 버전·후속 원본 계약으로 보강하고, 구현·리뷰·관련 검증 후 단계별 커밋과 dev push를 완료한다.

## Context

DB를 dev로 fast-forward하고 원격 dev도 `a8aafe39`로 갱신했다. v2는 실제 strategy → atomization → 중앙 Task 등록 → 워커 배정 경로가 있고 v3는 후보 updates + tasks를 한 `HypothesisDecision`으로 제안한다. 사용자는 v3에서 두 역할을 분리하길 요청했다. 기존 `.gitignore` 13행 변경, 사용자 임시 파일 및 앞선 점검 문서는 변경/스테이징하지 않는다.

## Requirements

- FR-001: 새 기본 v3는 Planner가 가설/전략/조사 질문만 제안하고, Atomizer가 실행 가능한 작은 probe 작업을 제안한다. Planner 출력에 worker 작업 목록을 허용하지 않는다.
- FR-002: Atomizer는 원래 질문의 대상·principal·완료 조건·관련 가설 revision·required 의미와 원본 후속 참조를 보존한다. 생성된 question ID는 계획 내부 참조이며 원본 result question ID와 혼동하지 않는다.
- FR-003: Atomizer는 역할/도구 가용성, 증거, 이전 조사 결과, profile 한도를 받아 분해한다. 도구 호출 순서·고정 PathPlan·의존성 DAG를 도입하지 않는다. 실제 등록/워커 배정은 기존 코드가 담당한다.
- FR-004: 두 판단은 동일 PlanningSnapshot, issued 후보 ID와 기존 Run 요청/decision 예산을 사용한다. 각 요청 직전 stop/시간/예산을 확인한다.
- FR-005: 두 판단이 성공하고 최종 관련 snapshot/CAS가 유효할 때만 후보+작업을 기존 중앙 트랜잭션으로 등록한다. Atomizer 실패/예산 소진/stale/stop에서 일부 후보만 선등록하지 않는다. 무관한 형제 갱신은 전체 폐기 근거가 아니다.
- FR-006: 기존 v2와 Atomizer 없는 기존 v3 프로필은 계속 지원한다. 선택한 역할/stage·prompt·정책 fingerprint가 저장·재개에서 의미를 보존하고, 기존 Run을 새 모드로 강제 재해석하지 않는다.
- FR-007: 실제 전달·영속 journal·상태 조회에서 계획과 작업 분해의 원본 대응을 확인할 수 있다. 새 병렬 원장이나 큐를 만들지 않는다.
- NFR-001: 중앙 engine/Control에 특정 AgentId·모델·MCP 이름 분기를 추가하지 않는다. 공개 정책으로 선택된 stage만 사용한다.
- NFR-002: 검증 부담을 불필요하게 늘리지 않는다. 기존 대상·원본·완료 계약과 질문 보존 검사로 필요한 경계만 확인한다. 실제 성능 우월성은 측정 없이 주장하지 않는다.

## Constraints

- C-001: 현재 dev에서만 구현하며 사용자 로컬 변경을 보존한다. root만 스테이징/커밋/push한다. force push 없음.
- C-002: 유료 모델·외부 대상·사용자 DB migration은 수행하지 않는다. fake/fixture 시험을 명시한다.
- C-003: 기존 v2 prompt bytes와 기존 단일 v3 prompt를 보존한다. 새 stage용 프롬프트를 별도 제공하여 예전 fingerprint를 불필요하게 바꾸지 않는다.
- C-004: 새 기본 구성은 분리형이며, 기존 single-stage v3를 선택할 호환 프로필을 제공한다. 변경된 구성으로 원본 Run을 force-resume하지 않는다.

## Confirmed Facts

- `specialist_planner.py:168`은 `probe_limits` 유무로 v2/v3 의미를 선택한다.
- `hypothesis_planner.py`는 `capture_snapshot`, `bind_decision`, `related_reads`, `register_probe_tasks`를 사용한다. updates와 tasks는 마지막에 함께 commit한다.
- `agent_profile.py`의 v3 검증은 현재 `task_atomization`을 금지한다.
- `AgentPrompts`는 공개 binding/compose 계약만 제공하며 내부 stage dict를 읽으면 안 된다.
- 기존 `.github/workflows/ci.yml`은 pytest, Ruff, mypy, Compose 및 별도 SDK/컨테이너 검증을 정의한다.
- 직전 고정 dev의 기본 Linux suite 3,882 passed/114 skipped는 baseline이며 이번 구현의 검증으로 계산하지 않는다.

## Unknowns / Assumptions

- A-001: 새 기본 profile의 version/stages 변경은 새 Run에 적용한다. 기존 profile/정책 선택을 보존하면 예전 의미의 resume를 지원할 수 있다. 실제 저장된 binding/config 경로까지 테스트로 확인한다.
- U-001: legacy test double들이 기본 profile에 한 번의 updates+tasks 응답을 기대한다. 이를 단순 weakening하지 않고 기존 single-stage 시험과 새 default split-stage 시험으로 명확히 나눈다.
- U-002: 관련 입력은 두 stage가 읽은 질문/가설/Task/result refs의 합집합이다. 최종 CAS와 stop/시간 실패 경로에서 실제 검증한다.

## External Research

추가 SDK/API/라이브러리를 도입하지 않는다. 설계 근거는 현재 저장소 계약/호출자/테스트이므로 외부 조사는 현재 필요하지 않다. 새 외부 API 의존이 발견되면 이 판단을 갱신한다.

## Current System / Target System

현재: `hypothesis_planning(updates + tasks)` → 기존 원자적 등록 → common workers.

목표: `hypothesis_planning(updates + investigation questions)` → `task_atomization(question_id + probe tasks)` → 질문 보존/관련 snapshot 확인 → 기존 원자적 등록 → common workers.

Empty/held/update-only 판단은 불필요한 Atomizer 요청 없이 처리한다. 워커의 native ReAct는 그대로 유지한다. 조건이 많아도 도구 호출 하나씩으로 강제 분해하지 않는다.

## Implementation Strategy

1. v3 조사 질문/Atomizer 출력 계약과 분해 보존 규칙을 orchestration 소유 모듈에 둔다. 기존 probe task 모델 및 subject/criterion 검사 재사용.
2. profile stage에서 파생되는 `SpecialistRunPolicy.probe_atomization` 선택을 추가한다. 기본 false는 직렬화에서 생략해 기존 정책 identity를 보존한다. v2에서 활성화 금지; v3 optional stage는 planner와 별도 비실행 역할이어야 한다.
3. 분리 모드만 새 planner schema를 사용한다. 기존 단일 `HypothesisDecision`은 유지한다. 새 질문은 planner가 역할/도구 호출을 고정하지 않고 목적·subjects·criteria·관련 refs를 제공한다.
4. Atomizer 출력의 plan-local question_id는 대응 검증/judgment 기록에만 쓰고 기존 source_question_id를 덮어쓰지 않는다. 최종 probe task/persistence 계약과 기존 binding/CAS를 재사용한다.
5. 질문 보존과 두-stage 의미를 기존 `planner_judgment`에 기록한다. 새 SQL schema/큐/approval 저장소를 추가하지 않는다.

## Task DAG

|Task|Goal / FR|Dependencies|Ownership / Interface|Acceptance / Validation|Risk / Execution / Status|
|---|---|---|---|---|---|
|TASK-001|요구·계약·ExecPlan 독립 검토 / all|없음|root + Opus; 이 문서|역할/호환/예산/commit 경계 타당성|HIGH / SERIAL_ONLY / 진행|
|TASK-002|새 v3 질문·분해 계약 및 workflow / 001–005,007|001|Muse A: hypothesis_planner.py + 새 probe_atomization.py + unit tests; 정책 probe_atomization 공개 bool 사용|원본 보존, 실패에서 부분 commit 없음, targeted Ruff/mypy/tests|HIGH / SERIAL_ONLY 내부 contract→workflow / 대기|
|TASK-003|정책·profile·프롬프트 선택 / 001,003,006|001; 002 공개 계약만 의존|Muse B: core/contracts/specialists.py, app/agent_profile.py, config/agents 및 profile tests|기존 false/legacy identity 유지, 새 default split, 별도 roles, invalid combinations 거절|MEDIUM / PARALLEL_SAFE with002 / 대기|
|TASK-004|두-stage 실제 통합·재개·실패 검증 / all|002,003|Muse C: 새 integration tests; root 기존 tests migration 통합|최소 성공/coverage 누락/가설 ID/기준변경/stop·quota/partial commit/legacy resume 확인|HIGH / SERIAL_ONLY 후 bounded 병렬시험 / 대기|
|TASK-005|독립 Opus+AGY 리뷰 및 유효 지적 repair / all|004|reviewers read-only; repair 각 원 소유자|지적별 소스·실패 근거 판정, 중요 이슈 해결|HIGH / PARALLEL_SAFE review / 대기|
|TASK-006|CI 범위 검증·계약 문서·완료·push / all|005|root: docs/agent/orchestration.md, docs/contracts/central-state.md, README, 이 ExecPlan|관련/full 기본 tests, lint/type/build/Compose, 예상 파일만 stage|HIGH / SERIAL_ONLY / 대기|

## Test Strategy

- Unit: planner tasks 금지, 질문 identity/조건 보존, two-principal criteria, goal-required/ref/source 보존, unknown role/한도/가설 revision 거절.
- Integration: 실제 중앙 assembly의 distinct role prompts와 2회 판단, tasks 등록/worker dispatch; update-only/held 한 번 요청; two-stage shared budget; Atomizer 실패 및 사이에 stop/goal/관련 evidence 갱신 시 무부분 commit; 무관한 변경 허용; reopen identity/cursor/no duplicate registration.
- Compatibility: v2 unchanged, old v3 single judgment 및 original fingerprints, split default 변경시 기존 Run 강제 resume 거절. 실제 policy/profile 직렬화와 consumers 점검.
- Targeted 검증은 고유 basetemp와 cache로 실행. source 고정 후 Linux Git archive + 새 editable env로 full pytest/Ruff/mypy/build/Compose를 수행해 Windows 경로 import timing 문제를 분리한다. fixture/live와 skip을 구분한다.

## Progress / Discoveries

- dev 병합 및 원격 동기화 완료. 기존 Atomizer 연결 점검 완료(관련 27 fixture tests PASS).
- 기존 atomizer prompt의 dependency DAG 문구는 현 계약과 불일치. 새 v3 prompt에는 이 문구를 가져오지 않는다. 이전 bytes는 호환을 위해 유지한다.

## Decision Log

- D-001: 기존 v3 전체를 v2로 바꾸지 않는다. 기본 v3에 별도 Atomizer stage를 연결하고 기존 단일 v3는 호환 선택으로 유지한다.
- D-002: 판단 둘을 한 global model budget/PlanningSnapshot 아래 수행하고 후보+작업 commit을 마지막 하나로 유지한다. 중간 stage output을 실행 권한으로 사용하지 않는다.
- D-003: plan-local question ID와 source result question ID의 의미를 분리한다. 작업 분해 결과의 검토 가능성은 기존 journal에 남긴다.

## Review Log / Validation Log

계획 리뷰·구현 검증·독립 리뷰 결과를 수행 후 기록한다. 아직 이번 구현 완료/검증을 주장하지 않는다.

## Requirement Traceability

모든 FR은 위 Task에 배정했다. 완료 시 실제 source/test/review 근거와 PASS 여부를 기록한다.

## Completion Criteria

필수 FR PASS, BLOCKER/HIGH 0, 중요 MEDIUM 해결, 관련 기존+신규 테스트 PASS, build/lint/typecheck/integration PASS, 최종 diff/모듈 경계 검토, 사용자 변경 제외, 단계별 커밋 및 정상 dev push. 완료 시 completed/로 이동하고 최종 결과·검증·review/repair·Git·잔여 한계를 기록한다.
