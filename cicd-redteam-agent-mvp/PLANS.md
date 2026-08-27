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

## Done definition

- 계획의 비-LLM API 완료 조건을 자동 테스트가 검증한다.
- 모든 출력 Finding과 AttackPath는 존재하는 Evidence만 참조한다.
- 전역 경로는 attacker-controlled source에서 민감한 token/identity sink로 연결된다.
- 실패·예산 종료에도 deterministic Evidence와 결과가 보존된다.
- 문서, 명령, 공개 schema 및 실제 검증 결과가 일치한다.
