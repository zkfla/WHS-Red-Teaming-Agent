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
- GitHub, AWS 및 외부 action 네트워크에 접근하지 않는다.
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

## Done definition

- 계획의 비-LLM API 완료 조건을 자동 테스트가 검증한다.
- 모든 출력 Finding과 AttackPath는 존재하는 Evidence만 참조한다.
- 전역 경로는 attacker-controlled source에서 민감한 token/identity sink로 연결된다.
- 실패·예산 종료에도 deterministic Evidence와 결과가 보존된다.
- 문서, 명령, 공개 schema 및 실제 검증 결과가 일치한다.
