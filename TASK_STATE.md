# Task State

## Snapshot

`cicd-redteam-agent-mvp`는 첨부된 7일 계획의 비-실모델 범위를 구현한 상태다.
deterministic 분석은 개별 GitHub Actions 시나리오를 전역 attacker-to-credential 경로로
조합하고 COMPLETE/PARTIAL/FAILED 보고서를 저장한다. trusted `.env`와 명시적 LiteLLM
provider 선택을 지원하며, 같은 hackingBuddyGPT Single Agent가 공식 Codex SDK의 저장된
ChatGPT 로그인을 LLM backend로 사용할 수도 있다. Codex가 별도 분석 단계나 두 번째 Agent로
동작하지 않는다.

## Goal

실제 workflow나 공격을 실행하지 않고 GitHub Actions의 Evidence 기반 Finding, 국소 및 전역
AttackPath, CloudIdentityCandidate를 재현 가능하게 생성한다.

## Completed

- 계획·아키텍처·공개 계약·테스트 명세를 MVP 내부 문서로 동기화했다.
- deterministic global path composer가 attacker source에서 write-capable GITHUB_TOKEN 또는
  Cloud Identity까지 Evidence-backed 경로를 생성한다.
- mutable external action은 민감 권한과 결합된 경우에만 CANDIDATE가 된다.
- Proposal validator가 기존 ID 충돌, ordered edge, node, Evidence와 identity 참조를 검사한다.
- Capability가 boundary 오류를 구조화하고 workflow facts에 assessment, Finding, path,
  identity를 포함한다.
- COMPLETE/PARTIAL/FAILED, exit 0/2/3/4/5, model error, no-progress, 반복 호출과 limits 보존을
  구현했다.
- 과거 커밋에 포함된 추적 Python bytecode 30개를 작업 트리에서 제거했다.
- 프로젝트 `.env` 자동 로딩, 프로세스 환경 우선순위, 다중 key의 명시적 provider 선택,
  provider/model 불일치 거부를 구현했다.
- `CodexChatGPTLLM`이 hackingBuddyGPT의 `LLM.get_response` 경계를 구현해 저장된 ChatGPT
  로그인을 공식 `openai-codex` SDK로 재사용한다.
- LiteLLM과 Codex 로그인 방식 모두 동일한 `CICDChatAgent`, 5개 Capability,
  `CICDUseCase`, `submit_analysis` validator를 사용한다.
- Codex runtime은 isolated temporary cwd, read-only sandbox, approval deny-all을 사용하며
  인증 토큰과 전체 model response를 trace에 기록하지 않는다.
- 잘못 추가했던 별도 모델 결과 적용 경로는 제거했다.

## Validation or Review

- 원본 계획 대조: Single Agent, `CICDChatAgent`, 반복 Capability 호출 및
  `submit_analysis`가 계획된 구조이며 별도 모델 분석 단계는 계획에 없음을 확인했다.
- 정적 호출 경로 대조: LiteLLM/API key와 `CodexChatGPTLLM`이 모두
  `run_agent_analysis`에서 동일한 Agent/Capability/UseCase 경로로 합류한다.
- 설치된 hackingBuddyGPT 및 `openai-codex` 소스와 메서드·필드 계약을 대조했다.
- reviewer 관련 이름과 제거된 adapter 참조를 저장소 전체에서 검색해 잔여가 없음을 확인했다.
- `uv lock --check`, Ruff 및 `git diff --check`: 통과.
- 사용자 요청에 따라 이번 정정 후 pytest, CLI smoke, 실제 모델 호출은 실행하지 않았다.

## Partial or Unverified

- **UNVERIFIED**: 실제 LiteLLM provider 인증, API 호출, real-model proposal COMPLETE
- **UNVERIFIED**: 실제 ChatGPT 로그인 Codex 실행과 real-model `submit_analysis` COMPLETE
- **UNVERIFIED**: 실제 provider의 token/cost 집계 및 외부 rate-limit 동작
- hackingBuddyGPT 내부에서 Pydantic v2 deprecated `dict()` 경고가 발생하지만 테스트는 통과한다.

## Key Decisions

- 확실한 사실·edge·상태는 deterministic domain이 결정하고 LLM은 향후 추가 조사와 우선순위만 담당한다.
- 실제 LLM API와 비용이 필요한 검증은 사용자 승인 전 실행하지 않는다.
- 다중 provider key가 있으면 `auto`로 추측하지 않고 `--provider`를 요구한다.
- 분석 대상 저장소의 `.env`는 인증 구성으로 읽지 않는다.
- ChatGPT 구독은 API key로 변환하지 않고 Codex SDK의 저장 로그인 경로로만 사용한다.
- Codex는 별도 분석 Agent가 아니라 hackingBuddyGPT Agent의 LLM 호환 adapter다.
- 정적 경로는 CONFIRMED가 될 수 없고 cloud trust가 없으면 CANDIDATE/UNKNOWN을 유지한다.

## Recommended Next Actions

1. 현재 변경을 팀 리뷰 후 `scenario/cicd`에 커밋한다.
2. 실제 LiteLLM provider 또는 `codex-chatgpt` 모델·사용량 한도를 정한 뒤 real-model
   smoke를 별도 승인으로 수행한다.
3. Windows Developer Mode 또는 CI 환경에서 symlink escape 테스트를 실행한다.

## Relevant Artifacts

- `cicd-redteam-agent-mvp/PLANS.md`: 구현 범위와 완료 조건
- `cicd-redteam-agent-mvp/docs/`: 아키텍처, 계약, 테스트 및 계획 커버리지
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/domain/attack_graph.py`: 전역 경로 조합

## Updated

- Date: 2026-08-25
- Workflow: lean-vibe-workflow + task-state-sync
- Workspace: `C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent`
