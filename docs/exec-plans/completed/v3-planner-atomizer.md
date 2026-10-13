# v3 Planner / Atomizer 역할 분리 ExecPlan

## Status

COMPLETED — 기준 dev `a8aafe39`, 사용자 작업폴더에서 구현·독립 리뷰·최종 수정·검증 완료. `AGENTS.md`와 `.agent/PLANS.md`를 적용했다. root가 계약·통합·최종 판단을 소유하고 DeepSeek 4.1 High orchestrator가 Muse 1.3 High의 충돌 없는 작업을 조정했다. 독립 리뷰는 Opus 4.8 High 및 AGY Gemini 3.8 Flash High를 사용했다.

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
- FR-007: 실제 전달·영속 journal에 계획과 작업 분해의 원본 대응을 남긴다. 기존 상태 조회는 생성된 Tasks와 원본 후속 참조를 유지한다. 새 병렬 원장이나 큐를 만들지 않는다.
- FR-008: 모든 Planner 질문은 하나 이상의 분해 작업에 대응해야 한다. 일부 질문을 조용히 버리는 분해를 거절한다. 현재 범위에서는 질문별 skip 상태를 새로 도입하지 않는다.
- FR-009: 같은 판단에서 새로 만든 후보의 code-issued ID와 제안 revision을 Atomizer 입력에 전달한다. snapshot에 아직 없는 새 후보의 probe도 최종 한 트랜잭션으로 등록할 수 있어야 한다.
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
|TASK-001|요구·계약·ExecPlan 독립 검토 / all|없음|root + Opus; 이 문서|역할/호환/예산/commit 경계 타당성|HIGH / SERIAL_ONLY / 완료|
|TASK-002|새 v3 질문·분해 계약 및 workflow / 001–005,007|001|Muse A: hypothesis_planner.py + 새 probe_atomization.py + unit tests; 정책 probe_atomization 공개 bool 사용|원본 보존, 실패에서 부분 commit 없음, targeted Ruff/mypy/tests|HIGH / SERIAL_ONLY 내부 contract→workflow / 완료|
|TASK-003|정책·profile·프롬프트 선택 / 001,003,006|001; 002 공개 계약만 의존|Muse B: core/contracts/specialists.py, app/agent_profile.py, config/agents 및 profile tests|기존 false/legacy identity 유지, 새 default split, 별도 roles, invalid combinations 거절|MEDIUM / PARALLEL_SAFE with002 / 완료|
|TASK-004|두-stage 실제 통합·재개·실패 검증 / all|002,003|Muse C: 새 integration tests; root 기존 tests migration 통합|최소 성공/coverage 누락/가설 ID/기준변경/stop·quota/partial commit/legacy resume 확인|HIGH / SERIAL_ONLY 후 bounded 병렬시험 / 완료|
|TASK-005|독립 Opus+AGY 리뷰 및 유효 지적 repair / all|004|reviewers read-only; repair 각 원 소유자|지적별 소스·실패 근거 판정, 중요 이슈 해결|HIGH / PARALLEL_SAFE review / 완료|
|TASK-006|CI 범위 검증·계약 문서·완료·push / all|005|root: docs/agent/orchestration.md, docs/contracts/central-state.md, README, 이 ExecPlan|관련/full 기본 tests, lint/type/build/Compose, 예상 파일만 stage|HIGH / SERIAL_ONLY / 완료 문서 커밋 후 정상 push|

## Test Strategy

- Unit: planner tasks 금지, 질문 identity/조건 보존, two-principal criteria, goal-required/ref/source 보존, unknown role/한도/가설 revision 거절.
- Integration: 실제 중앙 assembly의 distinct role prompts와 2회 판단, tasks 등록/worker dispatch; update-only/held 한 번 요청; two-stage shared budget; Atomizer 실패 및 사이에 stop/goal/관련 evidence 갱신 시 무부분 commit; 무관한 변경 허용; reopen identity/cursor/no duplicate registration.
- Compatibility: v2 unchanged, old v3 single judgment 및 original fingerprints, split default 변경시 기존 Run 강제 resume 거절. 실제 policy/profile 직렬화와 consumers 점검.
- Targeted 검증은 고유 basetemp와 cache로 실행. source 고정 후 Linux Git archive + 새 editable env로 full pytest/Ruff/mypy/build/Compose를 수행해 Windows 경로 import timing 문제를 분리한다. fixture/live와 skip을 구분한다.

## Progress / Discoveries

- dev 병합 및 원격 동기화 완료. 기존 Atomizer 연결 점검 완료(관련 27 fixture tests PASS).
- 기존 atomizer prompt의 dependency DAG 문구는 현 계약과 불일치. 새 v3 prompt에는 이 문구를 가져오지 않는다. 이전 bytes는 호환을 위해 유지한다.
- 새 default v5 profile, optional v3 Atomizer stage, 파생 Run policy flag와 false 직렬화 생략을 구현했다. 기존 native-probes-legacy.yaml의 Git blob은 기준 profile과 일치한다.
- Planner 질문과 Atomizer 작업의 계약을 추가하고, 후보 바인딩과 최종 commit 경로를 단일 공통 함수로 재사용한다. 실제 루프 회귀/실패/재개 검증 진행 중.
- 기존 단일 v3 피드백 시험은 legacy profile을 명시하도록 변경했으며 assertion은 보존했다. 새 default CLI 시험은 두 역할의 실제 binding을 확인한다.

## Decision Log

- D-001: 기존 v3 전체를 v2로 바꾸지 않는다. 기본 v3에 별도 Atomizer stage를 연결하고 기존 단일 v3는 호환 선택으로 유지한다.
- D-002: 판단 둘을 한 global model budget/PlanningSnapshot 아래 수행하고 후보+작업 commit을 마지막 하나로 유지한다. 중간 stage output을 실행 권한으로 사용하지 않는다.
- D-003: plan-local question ID와 source result question ID의 의미를 분리한다. 작업 분해 결과의 검토 가능성은 기존 journal에 남긴다.
- D-004: 사용자 최신 지시로 새 분리형 v3를 기본으로 선택하고 기존 v2·단일 플래너 v3 구현 및 호환 프로필을 폐기하지 않는다.
- D-005: 두 client.ask는 동일 DecisionBudget 객체를 사용하고 동일 Run accounting 범위 아래에서 요청마다 metering 및 admission을 적용한다. stage별 prompt 선택은 분리하지만 예산을 복제하지 않는다.

## Review Log / Validation Log

Opus 4.8 High 계획 독립 리뷰 승인. 새 issued 후보/revision 전달, 질문 전체 coverage, 동일 DecisionBudget 객체 사용을 FR-008/009와 D-005로 명시했다. 최종 구현 독립 리뷰도 완료했으며 아래 root 발견 오류를 수정한 뒤 추가 차단 이슈가 없음을 확인했다.

초기 root 검토에서 새 prompt의 구형 DAG 안내, 후보 검증/commit 중복, Atomizer에 전달되지 않은 structured strategy를 지적해 보완했다. 새 테스트의 임시 implementation-missing skip과 broad exception 검사는 제거했다. 실패/중단 시험은 실제 stage 경계와 관련 입력 변경을 사용하며 fixture 기능 자체를 단순 반복 검사하지 않는다.

중간 확인: Python 3.10 profile/기존 역할 관련 27 PASS(24.71초); Python 3.14 새 원자화 unit/profile 34 PASS(14.91초). 구현 중 기존 integration 수집은 파일 편집 중 SyntaxError로 중단됐고, source freeze 후 재실행해 31 PASS를 확인했다. 중간 결과를 최종 전체 검증으로 계산하지 않았다.

- source freeze 후 기존 integration 31 PASS. 최종 수정 archive에서 관련 신규/프로필 55 PASS(Python 3.10/3.14 각각), 전체 Ruff PASS, 전체 mypy 766 파일 PASS, sdist/wheel build PASS, 두 Compose config PASS. 전체 pytest matrix 결과와 최종 delta 검증은 아래에 기록했다.
- root는 `bind_plan`의 기존 완료 조건 `hypothesis_id`가 `issued.get(ref)`의 누락 fallback 때문에 None으로 바뀌는 실제 오류를 발견했다. `get(ref, ref)`로 수정하고 기존 후보 revision 2의 조건 identity 보존을 회귀 검증했다. Opus 추가 리뷰에서 이 수정의 소비 경로와 충분성을 확인했다.
- legacy 실제 Run의 같은 profile 재개 성공 및 새 기본 split profile 재개 거절을 직접 storage reopen으로 검증했다. fingerprint unit만으로 재개 완료를 주장하지 않는다.
- AGY Gemini 3.8 Flash High 첫 요청은 6분 timeout으로 결과가 없었다. 같은 conversation을 재개한 보조 리뷰는 완료했으며, 위 알려진 오류 이외 추가 차단 이슈는 없었다. Atomizer의 발급 전 local-key 거절 관찰은 발급된 ID를 사용해야 하는 공개 계약과 일치하므로 변경하지 않는다. 응답의 불필요한 문체와 전역 성공 표현은 근거로 사용하지 않았다.

## Requirement Traceability

|Requirement|실제 구현 / 소비 경로|검증 / 판정|
|---|---|---|
|FR-001/003|HypothesisPlanDraft → TaskAtomizationDraft → 기존 probe 등록/ready_workers; 새 default stage/profile/prompts|schema에서 Planner tasks 거절, distinct prompts, 실제 worker dispatch; PASS|
|FR-002/008|bind_plan / validate_atomized_tasks; 전체 질문 coverage, exact criterion identities, paired principals, revisions/required/source 보존|unit test_probe_atomization / test_hypothesis_split; 기존 후보 조건 ID 보존 회귀 포함; PASS|
|FR-004|_advance_split 두 ask의 동일 DecisionBudget와 engine.guard_reasoning, 기존 Run ledger|두 요청/repair accounting 및 quota=1/stop-between-asks integration; PASS|
|FR-005/009|issued 후보/revision 전달 및 shared finalization → register_probe_tasks 한 CAS|새 후보+probe 원자 등록, 실패 무부분 commit, related drift discard/unrelated sibling allow; PASS|
|FR-006|false 정책 직렬화 생략, 원래 profile/prompt 보존, 기존 Run binding 검사|golden fingerprint, v2/단일 v3 호환, 실제 legacy reopen 허용/new default reopen 거절; PASS|
|FR-007|기존 planner_judgment의 questions/coverage와 최종 원본 Task 등록|실제 SQL journal의 질문/coverage 저장, 기존 Task status 및 reopen/no duplicate integration; PASS. 별도 coverage UI는 추가하지 않음|
|NFR-001/002|소유 모듈의 공개 정책/stage 선택과 기존 registry/admission/persistence 재사용|root/Opus/AGY 소스 리뷰; 특정 AgentId 분기·새 원장·고정 도구 큐 없음; PASS|

## Completion Criteria

필수 FR PASS, BLOCKER/HIGH 0, 중요 MEDIUM 해결, 관련 기존+신규 테스트 PASS, build/lint/typecheck/integration PASS, 최종 diff/모듈 경계 검토, 사용자 변경 제외, 단계별 커밋 및 정상 dev push. 완료 시 completed/로 이동하고 최종 결과·검증·review/repair·Git·잔여 한계를 기록한다.

## Final Result

기본 v3는 전체 전략/가설/조사 질문을 제안하는 Planner와 질문을 bounded probe Tasks로 분해하는 Atomizer를 별도 실행한다. 원본 조건/참조 보존 검증을 통과한 후보와 Tasks를 기존 중앙 트랜잭션으로 함께 등록한다. 원래 v2 및 단일 플래너 v3 코드·프롬프트를 보존했으며, 단일 v3 선택은 native-probes-legacy.yaml로 제공한다.

## Important Decisions

새 기본 프로필 version 5에서만 분리를 활성화한다. 기존 Run을 새 프로필로 강제 재개하지 않는다. 두 단계는 한 budget/snapshot을 공유한다. 새 SQL schema, 실행 큐, dependency DAG, 특정 도구 순서를 도입하지 않았다. 통합된 후보 바인딩/최종 commit을 legacy와 split에서 재사용한다.

## Validation Summary

|Scope|Result|실제 범위 / 한계|
|---|---|---|
|전체 pytest Python 3.10|3,925 PASS / 114 SKIP / Docker 1 FAIL, 이후 해당 시험 단독 1 PASS(7.66초)|1019.51초; shell spool fetch None의 최초 실패를 숨기지 않음|
|전체 pytest Python 3.14|3,925 PASS / 114 SKIP / Docker 1 FAIL, 이후 해당 시험 단독 1 PASS(4.51초)|979.86초; global cap의 wins=0 최초 실패를 숨기지 않음|
|최종 수정 관련 unit/profile/integration|55 PASS, 0 SKIP, Python 3.10/3.14 각각|16.64초 / 14.82초; 기존 가설 criterion ID 수정 및 두 legacy reopen 신규 사례 포함|
|최종 persisted-criterion delta|통합 15 PASS(Python 3.10), 수정된 기존 후보 사례 1 PASS(Python 3.14)|11.84초 / 3.44초; 실제 저장된 Task criterion_id 및 가설 ID 유지|
|최종 journal coverage delta|기존 success/dispatch 사례 1 PASS(Python 3.10/3.14 각각)|실제 SQL journal의 questions 및 q1→t1/q2→t2 대응 검사|
|기존 single-stage/worker/CLI 통합|31 PASS|별도 legacy 선택과 새 default 실제 binding|
|Ruff / mypy|PASS / 766 파일 PASS|CI 지정 전체 범위; 최종 integration delta Ruff 추가 확인|
|Build / Compose|sdist+wheel PASS / 두 config PASS|실제 Docker 이미지 재빌드나 외부 target 실행을 의미하지 않음|

전체 matrix는 마지막 가설 ID 수정/재개 회귀 추가 전의 고정 archive에서 시작했다. 최종 source는 별도 고정 archive로 관련 전체 55개 및 마지막 persisted-criterion delta를 재검증했다. 최종 source 전체 matrix를 다시 실행했다고 주장하지 않는다. 기본 dev 의존성으로 실행해 선택적 SDK/외부 환경 등 114 skip을 유지했으며 paid LLM 또는 외부 target 검증은 하지 않았다.

두 전체 suite를 동시에 실행했을 때 변경하지 않은 Docker 작업실 테스트가 하나씩 실패했다. 두 프로세스 종료 후 실패한 사례를 차례로 재실행해 각각 통과했다. global cap 시험은 Docker daemon의 공용 workroom 수를 검사하므로 서로 다른 suite의 Docker 부분은 동시 실행하면 안 된다. spool fetch 실패의 세부 원인은 단독 재현되지 않아 동시 부하의 영향으로 추정만 남긴다. 관련 소스나 timeout을 완화하지 않았다.

## Independent Review Summary

Opus 4.8 High는 계획과 구현을 검토하고 root 발견 오류의 수정 충분성을 추가 확인했다. AGY Gemini 3.8 Flash High는 첫 timeout 후 동일 conversation 재개로 보조 리뷰를 완료했다. 최종 판단에서 별도 추가 blocker는 없었다. reviewer 보고를 그대로 성공 증거로 쓰지 않고 실제 source/consumer/tests와 대조했다.

## Issues Fixed During Review

후보 검증/최종 등록의 중복을 통합했다. Atomizer 입력에 정규화된 후보 updates·전략 reason·제안 revision을 전달했다. 모든 질문 coverage와 paired-principal 조건을 강제했다. 기존 후보의 완료 조건 ID를 잃는 fallback 누락을 수정했다. stop/관련 drift 시험의 실제 stage 경계를 보강하고 임시 skip/broad exception 검사를 제거했다. 실제 legacy resume 허용 및 새 default로 변경한 resume 거절을 추가했다.

## Requirement Status

FR-001–009 및 NFR-001–002 충족. 사용자 최신 지시인 새 v3 기본 선택과 기존 코드 보존을 반영했다. live 성능 향상이나 실제 취약점 탐지 성공은 주장하지 않는다.

## Git Summary

작업 checkout: C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent, branch dev, 기준 a8aafe39.

- 590431da: ExecPlan 계획.
- b2b95eef: v3 질문/분해 계약·workflow 및 원본 ID 보존 회귀.
- 6c339753: 새 default split 및 legacy profiles/prompts 보존.
- dbbc860d: 실제 integration/admission/legacy resume 검증.
- c58a5770: 실제 planner journal의 질문/분해 대응 저장 검증.
- 완료 문서는 completed/로 이동해 별도 commit 후 origin/dev에 정상 push한다. force push는 사용하지 않는다.

사용자 .gitignore 13행과 _blocks.json/norma.zip/tmp_* 및 앞선 audit 문서는 커밋하지 않았다.

## Remaining Risks

선택적 SDK/환경 skip과 paid LLM/외부 target 미실행은 위 범위대로 남는다. Docker 공용 자원 시험은 직렬 실행해야 하며 spool fetch의 동시 부하 실패는 재발할 경우 해당 기능 범위에서 조사한다. 이번 v3 변경과 연결된 미해결 필수 수정 사항은 없다.

Muse C가 만든 .pytest-wc* 임시 폴더의 재귀 삭제 명령이 자동 명령 정책에서 거절됐다. 우회 삭제하지 않았고 커밋에서 제외했다. 사용자 임시 파일은 보존했다.
