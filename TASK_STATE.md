# Task State

## Snapshot

`cicd-redteam-agent-mvp`는 공통 Evidence/Fact/Relation 추출 뒤 `deterministic`과 `reasoning`
분석 모드로 분기한다. deterministic은 기존 scenario rule과 global composer로 빠르고 재현
가능한 결과를 만들고, reasoning은 deterministic Finding/AttackPath를 미리 생성하거나 Agent에
노출하지 않은 채 security fact graph를 LLM이 조합하고 Validator가 검증한다. trusted `.env`,
LiteLLM provider와 공식 Codex SDK의 저장된 ChatGPT 로그인을 지원하며 Codex는 별도 Agent가
아니라 같은 hackingBuddyGPT Single Agent의 LLM backend다.

## Goal

실제 workflow나 공격을 실행하지 않고 GitHub Actions의 Evidence 기반 Finding, 국소 및 전역
AttackPath, CloudIdentityCandidate를 재현 가능하게 생성한다.

## Completed

- 계획·아키텍처·공개 계약·테스트 명세를 MVP 내부 문서로 동기화했다.
- deterministic global path composer가 attacker source에서 write-capable GITHUB_TOKEN 또는
  Cloud Identity까지 Evidence-backed 경로를 생성한다.
- mutable external action은 민감 권한과 결합된 경우에만 CANDIDATE가 된다.
- Relation에 `(source_id, relation_type, target_id)` 기반 stable `relation_id`를 부여하고,
  Proposal validator가 relation ID, source/type/target, ordered edge, Evidence와 identity 참조를
  검사한다.
- deterministic/reasoning 모드를 분리했고 reasoning에서는 기존 Finding/AttackPath 생성 및
  `GetWorkflowFacts` 노출을 차단한다.
- `ListSecurityFacts`가 relation ID와 source/transform/execution/credential/guard/context Fact를
  Agent에 제공한다.
- COMPLETE/PARTIAL/FAILED, exit 0/2/3/4/5, model error, no-progress, 반복 호출과 limits 보존을
  구현했다.
- 과거 커밋에 남아 있던 추적 Python bytecode 18개를 작업 트리에서 추가 제거했다.
- 프로젝트 `.env` 자동 로딩, 프로세스 환경 우선순위, 다중 key의 명시적 provider 선택,
  provider/model 불일치 거부를 구현했다.
- `CodexChatGPTLLM`이 hackingBuddyGPT의 `LLM.get_response` 경계를 구현해 저장된 ChatGPT
  로그인을 공식 `openai-codex` SDK로 재사용한다.
- LiteLLM과 Codex 로그인 방식 모두 동일한 `CICDChatAgent`, 5개 Capability,
  `CICDUseCase`, `submit_analysis` validator를 사용한다.
- Codex runtime은 isolated temporary cwd, read-only sandbox, approval deny-all을 사용하며
  inherited plugin과 configured MCP server를 비활성화한다. 인증 토큰과 전체 model response를
  trace에 기록하지 않는다.
- 실제 `gpt-5.6-terra` 실행에서 제출 필드명을 `paths`로 명확히 하고, 서로 다른 유효한
  Capability 호출을 progress로 인정하며, credential graph ID가 redaction으로 손상되지 않게
  보완했다.
- 잘못 추가했던 별도 모델 결과 적용 경로는 제거했다.

## Validation or Review

- 원본 계획 대조: Single Agent, `CICDChatAgent`, 반복 Capability 호출 및
  `submit_analysis`가 계획된 구조이며 별도 모델 분석 단계는 계획에 없음을 확인했다.
- 정적 호출 경로 대조: LiteLLM/API key와 `CodexChatGPTLLM`이 모두
  `run_agent_analysis`에서 동일한 Agent/Capability/UseCase 경로로 합류한다.
- 설치된 hackingBuddyGPT 및 `openai-codex` 소스와 메서드·필드 계약을 대조했다.
- reviewer 관련 이름과 제거된 adapter 참조를 저장소 전체에서 검색해 잔여가 없음을 확인했다.
- ChatGPT 로그인 real-model smoke: `tests/fixtures/global-chain`, `gpt-5.6-terra`, 4 rounds,
  94,677 reported tokens, 1 Finding, 5 AttackPaths, 1 CloudIdentityCandidate,
  `COMPLETE / proposal_validated`.
- real-model trace의 도구 호출은 `ListWorkflows`, `GetWorkflowFacts` 2회,
  `SubmitAnalysis`뿐이며 API-key 형태 원문 검색 결과는 0건이다.
- 전체 pytest: 67 passed, 1 skipped. skip은 Windows symlink 권한 조건이다.
- `uv lock --check`, Ruff 및 `git diff --check`: 통과.

## Partial or Unverified

- **UNVERIFIED**: 외부 자격 증명이 필요한 실제 LiteLLM provider 인증과 API 호출
- **UNVERIFIED**: LiteLLM provider의 token/cost 집계 및 외부 rate-limit 동작
- Codex SDK는 실행별 통화 비용을 제공하지 않아 보고서의 cost `0.0`은 무료 사용 증거가 아니다.
- hackingBuddyGPT 내부에서 Pydantic v2 deprecated `dict()` 경고가 발생하지만 테스트는 통과한다.

## Key Decisions

- 공통 Fact/Relation/Guard/Evidence 추출까지는 두 모드가 공유한다. deterministic은 rule 기반
  결론 생성, reasoning은 fact graph 기반 LLM 경로 추론을 담당하며 서로의 Finding/AttackPath를
  입력으로 사용하지 않는다.
- 현재 `relation_id`의 semantic key는 기존 tuple과 동일하게 유지하고 provenance/scope가 실제로
  필요해질 때 키를 확장한다.
- 실제 외부 provider API와 비용이 필요한 검증은 자격 증명과 사용 한도가 있을 때 수행한다.
- 다중 provider key가 있으면 `auto`로 추측하지 않고 `--provider`를 요구한다.
- 분석 대상 저장소의 `.env`는 인증 구성으로 읽지 않는다.
- ChatGPT 구독은 API key로 변환하지 않고 Codex SDK의 저장 로그인 경로로만 사용한다.
- Codex는 별도 분석 Agent가 아니라 hackingBuddyGPT Agent의 LLM 호환 adapter다.
- 정적 경로는 CONFIRMED가 될 수 없고 cloud trust가 없으면 CANDIDATE/UNKNOWN을 유지한다.

## Recommended Next Actions

1. Guard가 실제 공격 경로를 차단하는 의미 모델을 구체화한다.
2. Secret flow와 Artifact/Cache provenance 모델을 구체화한다.
3. 구조가 안정되면 복합 blind fixture로 reasoning 경로 조합을 평가한다.

## Relevant Artifacts

- `cicd-redteam-agent-mvp/PLANS.md`: 구현 범위와 완료 조건
- `cicd-redteam-agent-mvp/docs/`: 아키텍처, 계약, 테스트 및 계획 커버리지
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/domain/attack_graph.py`: 전역 경로 조합
- `cicd-redteam-agent-mvp/output/real-model/20260824T160152.198638Z/`: real-model 결과

## Updated

- Date: 2026-08-26
- Workflow: vibe-code-workflow + task-state-sync
- Workspace: `C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent`
