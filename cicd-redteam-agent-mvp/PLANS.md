# CI/CD Red Team Agent 구현 계획

> **문서 상태: v0.1.0 구현 이력**
>
> 현재 사용법은 [README](README.md), 공개 계약은
> [docs/CONTRACTS.md](docs/CONTRACTS.md), 완료 범위는
> [docs/PLAN_COVERAGE.md](docs/PLAN_COVERAGE.md)를 기준으로 합니다.

## Goal

GitHub Actions 저장소를 안전하게 분석하고, 개별 규칙의 사실을 Evidence 기반 전역 공격
경로로 조합하며, fake LLM으로 Agent 계약과 종료 동작을 재현할 수 있는 `v0.1.0`
프로토타입을 완성한다. 동일한 hackingBuddyGPT Single Agent를 LiteLLM provider key 또는
ChatGPT 로그인 기반 Codex LLM adapter로 실행하고 실제 로그인 smoke까지 검증한다.

## Deliverables

- deterministic parser, attacker-control, identity, scenario rule, global path composer
- Evidence 제한과 ordered edge를 검증하는 원자적 AnalysisProposal 처리
- COMPLETE/PARTIAL/FAILED 및 CLI exit code 계약
- 정상·오류·예산·반복 호출·prompt injection 회귀 테스트
- 설치, 아키텍처, 공개 계약, 테스트 명세

## Constraints

- 대상 workflow, action, script를 실행하지 않는다.
- 외부 관찰은 reasoning mode의 명시적 read-only opt-in에서만 수행하며 외부 action은 실행하거나
  다운로드하지 않는다.
- 실제 Secret 값과 redaction 전 파일 내용을 저장하지 않는다.
- 정적 Attack Path는 `CONFIRMED`가 될 수 없다.
- 실제 workflow·script·action 실행은 real-model smoke에서도 금지한다.
- 분석 대상 저장소의 `.env`는 인증 입력으로 사용하지 않는다.

## Architecture decisions

- 확실한 사실과 연결은 domain의 deterministic 규칙이 생성한다.
- 개별 규칙은 Finding과 국소 AttackPath를 만들고, global composer가 기존 Evidence만으로
  경로를 결합한다.
- LLM은 향후 후보 우선순위와 추가 조사에만 사용하며 사실, Evidence 또는 최종 상태를
  임의로 만들 수 없다.
- 외부 action의 mutable tag만으로 Finding을 만들지 않는다. 민감 권한과 결합된 경우에만
  `CANDIDATE`로 남긴다.

## Phase map

### Phase 1: 계약과 명세 동결

- **Objective**: 구현 범위, graph 연결, 검증 및 종료 계약을 문서화한다.
- **Validation**: 문서와 공개 모델·CLI가 모순되지 않는다.
- **Exit criteria**: `docs/ARCHITECTURE.md`, `docs/CONTRACTS.md`, `docs/TESTING.md`가 존재한다.

### Phase 2: 전역 공격 경로

- **Objective**: 국소 Finding·Path·Identity를 Evidence 기반 전체 경로로 조합한다.
- **Validation**: PR 시작점에서 token 또는 cloud identity까지 연결되는 fixture와 단절
  fixture가 모두 통과한다.
- **Exit criteria**: 확실한 edge만 `SUPPORTED`, 누락 근거가 있으면 `CANDIDATE`다.

### Phase 3: Validator와 강건성

- **Objective**: ordered edge, node, Evidence, 중복과 원자적 submit 계약을 강화한다.
- **Validation**: malformed proposal, invented Evidence, invalid edge, duplicate submit을 거부한다.
- **Exit criteria**: 실패가 state를 부분 변경하지 않는다.

### Phase 4: 종료·부분 결과

- **Objective**: 반복 호출, no-progress, 모델 오류 및 limits를 안정적으로 PARTIAL 처리한다.
- **Validation**: fake Agent/limits로 각 종료 사유와 보존된 deterministic 결과를 검사한다.
- **Exit criteria**: exit code와 report status가 계약과 일치한다.

### Phase 5: 통합과 freeze

- **Objective**: 전체 fixture, CLI, lint, 문서와 저장소 위생을 동결한다.
- **Validation**: `uv sync`, 전체 pytest, Ruff, deterministic CLI smoke.
- **Exit criteria**: deterministic 분석과 fake LLM 회귀가 통과한다.

### Phase 6: Provider와 Codex LLM 연결

- **Objective**: trusted `.env`, 명시적 provider, ChatGPT 로그인 기반 Codex LLM adapter를
  기존 hackingBuddyGPT Agent loop에 연결한다.
- **Validation**: 다중 key 모호성, provider/model 불일치, 대상 저장소 `.env` 거부,
  두 provider 경로가 동일한 `run_agent_analysis`로 합류하는지 정적으로 검사한다.
- **Exit criteria**: 키나 로그인 토큰을 노출하지 않고 두 인증 방식 모두 동일한
  `CICDChatAgent → Capability → submit_analysis` 경로를 사용한다.

### Phase 7: ChatGPT 로그인 real-model smoke

- **Objective**: 저장된 ChatGPT 로그인을 이용해 실제 모델이 Capability를 반복 호출하고
  유효한 `AnalysisProposal`을 제출하도록 한다.
- **Validation**: fixture 저장소에서 `list → get facts → submit_analysis` trace와
  `proposal_validated` COMPLETE 결과를 확인한다.
- **Exit criteria**: provider/model/token/round/termination과 결과 artifact가 저장되고,
  Codex runtime의 inherited plugin·MCP server가 비활성화된다.

### Phase 8: 구조화 가설과 작업 메모

- **Objective**: Agent가 대화 기록과 별개로 현재 가설, 근거, 반증, 부족한 Evidence와 다음
  확인 대상을 `AnalysisState`에 보존한다.
- **Validation**: 메모 patch가 기존 Capability와 같은 라운드에서 실행되고, unknown ID,
  FALSE support, stale revision, semantic no-op과 반복 우회를 거부하며 두 provider 입력에 같은
  상태가 투영되는지 검사한다.
- **Exit criteria**: 추가 모델 호출·Agent 라운드 없이 최대 8개의 bounded hypothesis가
  checkpoint와 PARTIAL handoff에 보존되고 기존 Finding/AttackPath 결과가 변하지 않는다.

### Phase 9: 안전한 checkpoint 재개

- **Objective**: PARTIAL reasoning 실행을 마지막으로 확정된 라운드 경계에서 동일 run
  디렉터리로 재개하고, 실행 이력과 누적 예산을 보존한다.
- **Validation**: atomic checkpoint 복원, append-only event journal, 불완전 라운드 rollback,
  attempt 증가, 동시 실행 잠금, 저장소 commit·dirty·prompt·limit 검사를 확인한다.
- **Exit criteria**: `cicd-agent resume <run-dir>`가 provider와 무관한 `AnalysisState`를 원본으로
  사용하며, 모델 대화나 raw chain-of-thought 없이 중단 지점 이후 분석을 계속한다.

### Phase 10: GitHub Actions semantic IR

- **Objective**: YAML 문법 객체와 event-scoped 실행 의미론을 분리하고, permission/trust를
  deterministic semantic core에서 한 번만 계산한다.
- **Tasks**: `TriggerExecutionContext`, `JobExecutionContext`, permission lattice를 도입하고
  기존 `Workflow/Job/Step`와 Evidence는 그대로 유지한다. Context는 필요할 때만 materialize해
  syntax node 복제를 피한다.
- **Validation**: 다중 trigger workflow가 event별 context를 만들고 기존 parser 결과가 변하지
  않는다. Objective/Agent/Validator 계약은 영향받지 않는다.
- **Exit criteria**: `AnalysisState`가 syntax graph와 execution semantic state를 동시에 보존한다.
- **Dependencies**: 기존 parser/Evidence.

### Phase 11: Triggerability와 cross-workflow trust boundary

- **Objective**: `pull_request`, `pull_request_target`, `push`, `workflow_run`, `workflow_call`,
  `workflow_dispatch`, `issue_comment`의 triggerability와 trust provenance를 명시한다.
- **Tasks**: attacker→event→workflow→job reachability relation을 만들고 local reusable workflow와
  `workflow_run` 경계를 조기에 모델링한다. Nested reusable workflow는 현재 GitHub 제한인 최대
  10 level 안에서 cycle-safe하게 처리한다.
- **Validation**: `pull_request_target` trigger 가능성과 attacker code execution을 분리하고,
  local `workflow_call`은 caller context를 보존한다.
- **Exit criteria**: cross-workflow 경계가 privilege 계산보다 먼저 semantic graph에 존재한다.
- **Dependencies**: Phase 10.

### Phase 12: Effective permission / credential semantics

- **Objective**: `GITHUB_TOKEN` 권한을 Job의 평면 속성이 아니라 execution context의 권한으로
  계산한다.
- **Tasks**: repository default UNKNOWN → workflow declaration → job declaration → fork PR adjustment
  → reusable caller ceiling 순으로 permission lattice를 적용한다. Explicit mapping에서 누락 scope는
  `NONE`, 선언 자체가 없고 repository default를 모르면 `UNKNOWN`으로 구분한다.
- **Validation**: push/write, fork PR/write-setting UNKNOWN, explicit read, reusable caller ceiling을
  각각 검증한다.
- **Exit criteria**: credential relation은 effective permission state에서만 생성된다.
- **Dependencies**: Phase 10-11.

### Phase 13: Reachability / provenance / semantic relation migration

- **Objective**: condition의 안전한 deterministic subset과 code provenance를 결합해 실제 실행
  가능 context만 credential sink와 연결한다.
- **Tasks**: event-name/literal/status-function 중심 condition evaluator, step reachability,
  attacker-controlled checkout/provenance를 semantic relation으로 발행한다. 기존 flat permission
  edge와 `attack_graph.py`의 중복 permission 계산을 단계적으로 제거한다.
- **Validation**: SUPPORTED/UNKNOWN/BLOCKED fixture와 기존 reasoning matrix 회귀를 함께 돌린다.
- **Exit criteria**: attack graph는 GitHub semantics를 재계산하지 않고 semantic Relations만 조합한다.
- **Dependencies**: Phase 10-12.

### Phase 14: Goal-aware graph frontier와 guard 우선 판정

- **Objective**: 전체 Relation 목록을 LLM이 직접 조립하기 전에 deterministic 계층이
  source→sink 연속 경로 후보, 동일 실행 context의 guard/contradiction, missing evidence와 다음
  확장 지점을 bounded Frontier로 제공한다.
- **Tasks**: Relation semantic registry, trigger/job-context를 포함한 typed traversal state,
  fail-closed relation allowlist, conservative dominance pruning, diversity-preserving top-K,
  `ListAttackFrontiers` pagination/cache를 구현한다. Objective는 순위와 목표 sink까지의 탐색에만
  사용하고 Fact/verdict/Evidence/Validator를 변경하지 않는다.
- **Validation**: 모든 반환 `relation_ref`가 실제 그래프에서 연속이고, trigger-only authority는
  proposal-ready가 아니며, FALSE 또는 같은 scope의 hard guard는 `BLOCKED/REJECTED` 상한이 된다.
  UNKNOWN은 local/external/unmodeled로 구분되고 탐색·응답 truncation이 명시된다.
- **Exit criteria**: `COMPLETE + proposal_ready`만 compact proposal 후보가 되고 `PARTIAL`은
  진단 전용이다. 동일 state/Goal에서 결과가 안정적이며 기존 scenario rules와 Validator 결과는
  변하지 않는다.
- **Dependencies**: Phase 10-13.

### Phase 15: 동적 Observation과 재계획 runtime

- **Objective**: 정적 분석의 외부 `UNKNOWN`을 typed read-only Observation으로 해소하고
  `Observe → semantic recompute → Frontier replan`을 중단·재개 가능한 canonical loop로 만든다.
- **Tasks**: `RedTeamSession`, Knowledge/Task/Observation ledger, one active Task lease, deterministic
  planner와 Perceptor를 도입한다. repository 기본 workflow permission과 Frontier job의 concrete
  Environment protection을 GitHub read-only observer로 연결한다. Frontier readiness/impact/
  grounding/information-gain/Objective/cost score와 bounded diverse portfolio를 만들고 canonical
  working memory를 재조정한 뒤 runtime budget/progress 신호까지 포함해 `OBSERVE`, `INVESTIGATE`,
  `SUBMIT`, `STOP_PARTIAL` 전환을 결정한다.
- **Validation**: 동일 Observation은 중복 반영되지 않고 semantic revision과 Frontier가 실제로
  변경되어야 한다. checkpoint는 Knowledge revision, Task attempts, Observation을 복원하고 session
  state 불일치, 잘못된 binding, active lease를 fail-closed 처리해야 한다. stale, contradicted,
  blocked, duplicate-grounded hypothesis는 자동 폐기되고 조기/portfolio 밖 제출은 거부되어야 한다.
- **Exit criteria**: resume 후에도 완료 Task를 재실행하지 않고 같은 canonical state에서 동일한
  다음 조사 결정과 portfolio를 내린다. 관찰 실패는 `UNKNOWN`을 보존한 채 reasoning을 계속하고,
  충분히 grounded된 proposal-ready portfolio 또는 완결된 empty 결과에서만 제출로 전환한다.
  configured limit, token reserve 부족, 반복 호출·진행 정체는 `STOP_PARTIAL`로 정규화하고 현재
  canonical partial state를 보존한다.
- **Dependencies**: Phase 9, 12-14.

### Phase 16: Generic Evidence A-G freeze

- **Objective**: Frontier missing evidence를 provider-neutral typed requirement로 바꾸고 GitHub
  resolver/transport/runtime evidence의 완료 계약을 한 회귀 경계로 고정한다.
- **Tasks**: discriminated Evidence payload, `ResolverRegistry`, repository/fork/Actions/branch/
  Environment/pending-deployment/workflow-run-jobs resolver, semantic recompute, serial request,
  cache/conditional revalidation, request budget/rate limit metric을 통합한다.
- **Validation**: resolver 하나당 `Relation → Requirement → Resolver → Observation → KnowledgeState
  → semantic recompute`가 닫혀야 한다. runner job은 unique exact identity만 연결하고 다른
  `runner_id`는 해당 persistence path를 `FALSE`, 같은 ID는 cleanup uncertainty를 보존한다.
- **Exit criteria**: 전체 회귀와 Generic Evidence acceptance suite가 통과하고, 최대 3
  Observations × resolver당 2 GET 상한, failure-preserves-UNKNOWN, checkpoint/resume conditional
  revalidation 계약이 문서와 코드에서 일치한다.
- **Dependencies**: Phase 15.

### Phase 17 / P2: GitHub Actions status·dependency semantics freeze

- **Objective**: `if:` 문자열 truth와 실제 scheduler reachability를 분리하고, `needs` DAG,
  implicit `success()` status check, `success()/failure()/cancelled()/always()`, explicit
  `needs.<job>.result`, step `outcome/conclusion`, runtime job/step 결과를 하나의 deterministic
  execution-status semantic core로 연결한다.
- **P2-A — Status contract/parser**: `SUCCESS/FAILURE/CANCELLED/SKIPPED/UNKNOWN` result model,
  dependency/status context, step/job `continue-on-error`와 matrix/fail-fast 존재 여부를 bounded
  syntax fact로 보존한다. Dynamic `continue-on-error`와 matrix aggregate는 추측하지 않는다.
- **P2-B — Scheduler/dependency semantics**: workflow 내부 `needs` DAG를 job key로 resolve하고,
  explicit status-check 함수가 없는 `if:`에는 implicit `success()` gate를 별도 계산한다.
  `ConditionEvaluation`의 raw expression truth와 status-gate/effective truth, 최종
  `JobExecutionContext.reachable`을 분리한다. failed/skipped dependency propagation과 ancestor
  failure summary를 모델링하되 unknown/cycle/unresolved dependency는 fail-closed `UNKNOWN`으로 둔다.
- **P2-C — Condition/status evaluator**: job-level `success/failure/cancelled/always`와 explicit
  `needs.<job>.result`를 우선 지원하고, step-level은 이전 step의 순차 state transition을 사용해
  status 함수와 `steps.<id>.outcome/conclusion`을 평가한다. `continue-on-error` 전 result는 outcome,
  적용 후 result는 conclusion으로 분리한다. `needs.*` wildcard는 hosted GitHub live probe 전까지
  `UNKNOWN`으로 유지한다.
- **P2-D — Runtime Evidence**: 기존 `GITHUB_WORKFLOW_RUN_JOBS`를 확장해 bounded runtime step
  status/conclusion을 보존하고, workflow-run status는 identity와 분리된 typed Evidence로 둔다.
  static↔runtime job/step binding은 unique exact identity/name만 허용한다. REST가 제공하지 않는
  step outcome은 conclusion과 static `continue-on-error`만으로 증명 가능한 경우에만 projection하고
  나머지는 `UNKNOWN`이다. 기존 GET/cache/checkpoint budget을 유지하고 LLM resolver selection은 0이다.
- **P2-E — Controlled live acceptance/freeze**: 별도 private fixture에서 implicit success,
  `always` override, ancestor `failure()`, explicit `needs.X.result`, step continue-on-error,
  cancellation, `needs.*` wildcard hard-negative를 실제 GitHub hosted scheduler로 관측한다. Fixture
  preparation의 dispatch/cancel mutation과 acceptance GET-only 관찰을 분리한다. 문서와 실제 hosted
  behavior가 충돌하면 관측 사실과 공식 계약을 분리하고 provider quirk는 별도 contract로 제한한다.
- **Validation**: P0/P1 전체 회귀가 유지되고, status function truth와 dependency eligibility,
  runtime observation, reachability가 각각 독립적으로 검증된다. 403/404/429/timeout/budget failure는
  affirmative status evidence를 만들지 않는다. matrix/reusable/dynamic/wildcard ambiguity는
  similarity나 transitive 추측 없이 `UNKNOWN`을 보존한다.
- **Exit criteria**: targeted offline suite, 전체 pytest/Ruff/lock/diff check, controlled P2 live
  acceptance가 통과하고 `docs/CONTRACTS.md`, `docs/TESTING.md`, `docs/IMPLEMENTATION_STATUS.md`,
  `TASK_STATE.md`에 frozen boundary와 남은 UNKNOWN을 기록한다.
- **Dependencies**: Phase 13, Phase 16, P0 actor/run freeze, P1 event-principal freeze.
- **Status**: **COMPLETED / live-validated / frozen (2026-08-29)**. Controlled fixture status run
  `33236505618`, cancellation run `33236538249`, GET-only live harness 2/2, targeted regression
  64/64, full pytest/Ruff/lock/diff gates passed. Hosted `needs.*` indirect-ancestor behavior was
  observed but remains an explicit provider quirk/`UNKNOWN` boundary rather than portable semantics.

### Phase 18 / P3: AWS GitHub OIDC trust-policy semantics

- **Objective**: `configure-aws-credentials`의 실제 인증 모드, GitHub `aud/sub` 후보와 AWS IAM
  role trust를 분리해 `oidc_may_assume_cloud_identity`를 `TRUE/FALSE/UNKNOWN`으로 재계산한다.
- **Scope**: direct official action, 기본 legacy/immutable subject, environment/PR/단일 push ref,
  IAM federated principal, `sts:AssumeRoleWithWebIdentity`, Allow/Deny, `StringEquals/StringLike`.
  custom subject template, reusable-workflow 전용 claim, 실제 STS 호출과 role permission은 제외한다.
- **Evidence**: GitHub repository numeric metadata/OIDC subject configuration 및 명시적
  `--observe-aws`의 `GetRole`/`GetOpenIDConnectProvider` read-only Observation만 사용한다.
- **Semantics**: 필요한 관찰이 모두 있고 모든 가능한 subject 형식이 허용되며 적용 가능한
  Deny가 없을 때만 `TRUE`; 증거가 완전하고 Allow가 없거나 Deny가 적용될 때만 `FALSE`;
  unsupported/dynamic/custom/누락 상태는 `UNKNOWN`이다.
- **Status**: **IMPLEMENTED / offline-validated (2026-08-29)**. 실제 AWS 계정·STS assumption은
  실행하지 않았으며 계속 `UNKNOWN`이다.

### Phase 19 / P4: GitHub Actions condition satisfier semantics

- **Objective**: `if:` expression truth/reachability와 공격자가 그 식을 만족시킬 수 있는지를
  독립된 축으로 보존하고, condition 기반 source/guard를 단일 canonical Relation 경로로 Frontier에
  전달한다.
- **Scope**: 기존 tokenizer/AST에 `contains`, `startsWith`, `endsWith`를 추가하고 event-aware
  `ref/ref_name/head_ref/base_ref`, repository identity, actor/triggering actor, PR repository,
  label/author association 및 P2 status context를 bounded 방식으로 판정한다.
- **Semantics**: 보안 효과는 `ALWAYS_TRUE`, `ATTACKER_SATISFIABLE`, `TRUST_REQUIRED`, `BLOCKS`,
  `UNKNOWN`이다. actor 문자열이나 `triggering_actor`는 권한 증명이 아니며, label/review 조건은
  신뢰 주체 개입과 exact revision binding을 분리한다. 지원 밖 함수·coercion·dynamic context는
  boolean short-circuit로 안전하게 결정되는 경우를 제외하고 `UNKNOWN`이다.
- **Frontier contract**: 공격자 만족 가능성이 입증된 경우에만
  `attacker_can_satisfy_condition`; 확정 차단 또는 신뢰 조건은 canonical
  `guard_blocks_untrusted_execution`을 사용한다. 레거시 generic condition relation은 생성하지 않는다.
- **Status**: **COMPLETED / offline-validated (2026-08-29)**. 전체 pytest 298 passed,
  1 skipped, 12 deselected; Ruff와 `git diff --check`가 통과했다.

## Done definition

- 계획의 비-LLM API 완료 조건을 자동 테스트가 검증한다.
- 모든 출력 Finding과 AttackPath는 존재하는 Evidence만 참조한다.
- 전역 경로는 attacker-controlled source에서 민감한 token/identity sink로 연결된다.
- 실패·예산 종료에도 deterministic Evidence와 결과가 보존된다.
- 문서, 명령, 공개 schema 및 실제 검증 결과가 일치한다.
