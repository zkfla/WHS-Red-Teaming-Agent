# Task State

## Snapshot

`cicd-redteam-agent-mvp`는 공통 Evidence/Fact/Relation 추출 뒤 `deterministic`과 `reasoning`
분석 모드로 분기한다. deterministic은 기존 scenario rule과 global composer로 빠르고 재현
가능한 결과를 만들고, reasoning은 deterministic Finding/AttackPath를 미리 생성하거나 Agent에
노출하지 않은 채 security fact graph를 LLM이 조합하고 Validator가 검증한다. trusted `.env`,
LiteLLM provider와 공식 Codex SDK의 저장된 ChatGPT 로그인을 지원하며 Codex는 별도 Agent가
아니라 같은 hackingBuddyGPT Single Agent의 LLM backend다. Codex는 분석 중 별도 대화를
소유하지 않고, 매 판단마다 구조화된 현재 상태와 최근 Agent 이벤트만 받는다. reasoning
Agent는 최대 8개의 근거 기반 가설, 부족한 정보와 다음 확인 대상을 bounded working memory로
유지하며 선택한 Capability 호출과 같은 라운드에서 이를 갱신한다. reasoning 실행은 선택적으로
trusted 자연어 Objective를 받아 조사 목표를 좁힐 수 있으며, Objective는 system policy 아래의
immutable run instruction으로 매 라운드 재투영되고 repository/tool 데이터와 분리된다. bounded
deterministic Goal compiler가 source/mechanism/sink 힌트를 추출해 관련 Fact를 우선 정렬하지만
Evidence/Relation verdict/Validator 결과를 만들거나 변경하지 않는다. GitHub Actions domain에는
별도 event-scoped execution semantic layer가 추가되어 trigger/job context, reachability,
`GITHUB_TOKEN` permission lattice와 cross-workflow trust boundary를 Fact 생성 전에 계산한다.

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
- Security Fact extractor와 hidden-oracle matrix가 guard, Secret flow, artifact/cache provenance,
  mutable dependency, runner/runtime 및 AWS/GCP/Azure identity 시나리오까지 확장됐다.
- reasoning Agent가 transient 모델 오류만 한 turn 안에서 최대 두 번 재시도하고, 전체 duration
  제한과 bounded exponential full jitter를 공유한다. LiteLLM 내부 retry는 꺼서 중첩을 막는다.
- canonical history/state는 보존하면서 모델 입력에서 오래된 limit와 context 초과 round만
  축약한다. `ListSecurityFacts`는 100개 단위 pagination과 `next_offset`을 제공한다.
- reasoning 실행은 매 라운드와 종료 상태에서 `checkpoint.json`을 원자 교체하며, retry,
  context compaction, checkpoint 및 마지막 Capability 통계를 보고서 metadata와 summary에 남긴다.
- COMPLETE/PARTIAL/FAILED, exit 0/2/3/4/5, model error, no-progress, 반복 호출과 limits 보존을
  구현했다.
- 과거 커밋에 남아 있던 추적 Python bytecode 18개를 작업 트리에서 추가 제거했다.
- 프로젝트 `.env` 자동 로딩, 프로세스 환경 우선순위, 다중 key의 명시적 provider 선택,
  provider/model 불일치 거부를 구현했다.
- `CodexChatGPTLLM`이 hackingBuddyGPT의 `LLM.get_response` 경계를 구현해 저장된 ChatGPT
  로그인을 공식 `openai-codex` SDK로 재사용한다.
- LiteLLM과 Codex 로그인 방식 모두 동일한 `CICDChatAgent`, 6개 Capability,
  `CICDUseCase`, `submit_analysis` validator를 사용한다.
- Codex runtime은 isolated temporary cwd, read-only sandbox, approval deny-all을 사용하며
  inherited plugin과 configured MCP server를 비활성화한다. 인증 토큰과 전체 model response를
  trace에 기록하지 않는다.
- Codex SDK runtime은 분석 실행 동안 재사용하되 판단마다 ephemeral thread를 사용한다. 전체
  hackingBuddyGPT 대화 대신 trusted Agent policy, `AnalysisState` 진행 요약, 현재 결과 ID,
  progress counters와 최근 일곱 Agent 이벤트를 bounded decision context로 투영한다.
- 구조화 작업 메모를 `AnalysisState`에 추가했다. 가설은 기존 Evidence/Relation ID만 참조하고,
  `FALSE` Relation을 지지 근거로 쓰거나 `UNKNOWN` Relation의 missing evidence를 누락하면
  갱신을 거부한다.
- 작업 메모 patch는 별도 Tool이 아니라 기존 6개 Capability 호출에 선택적으로 동봉된다.
  따라서 추가 모델 호출·라운드를 소비하지 않으며, patch 내용은 반복 호출 판정 signature에서
  제외된다. revision 충돌, 중복 가설, 전체 8개/12,000자 제한도 적용한다.
- LiteLLM과 Codex 모두 매 판단에 동일한 최신 working-memory projection을 받으며 PARTIAL
  보고서는 미완료 가설과 다음 확인 대상을 보존한다. COMPLETE 결과의 Finding/AttackPath
  확정 권한은 기존 `SubmitAnalysis` validator에만 남는다.
- reasoning matrix 평가가 라운드·토큰·Capability 호출/반복·working-memory revision과 최종
  가설 상태를 실행별·집계별로 기록한다. private chain-of-thought는 수집하지 않는다.
- Security Fact를 본 뒤 추가 workflow/file/search 조사를 선택하면 첫 후속 호출에 ACTIVE 또는
  BLOCKED 가설을 함께 기록하도록 런타임이 강제한다. 바로 제출 가능한 단순 경로에는
  불필요한 메모를 강제하지 않는다.
- 직전 모델 라운드보다 남은 토큰이 적으면 새 라운드를 시작하지 않으며, Codex가 malformed
  structured Tool JSON을 반환하면 원문을 노출하지 않고 bounded retry 대상으로 처리한다.
- `ListSecurityFacts`가 실행 내에서만 유효한 짧은 Relation 참조(`R0001` 형식)를 제공하고,
  compact proposal expander가 선택된 참조를 정확한 node, edge, Evidence, missing evidence 및
  허용 상태로 복원한다. 알 수 없거나 중복되거나 불연속인 참조는 상태 변경 전에 거부하며,
  복원된 결과는 기존 `AnalysisProposal` validator를 그대로 통과해야 한다.
- compact Fact 응답은 기본적으로 긴 Relation/Evidence ID를 생략하고 source/target/type/verdict,
  Evidence 개수와 missing evidence만 반환한다. legacy 전체 proposal 호환 시에만 grounding ID를
  명시적으로 요청할 수 있으며, unfiltered 전체 응답은 공격 경로 순서에 맞춰 정렬된다.
- working-memory patch도 짧은 supporting/contradicting Relation 참조를 받을 수 있다. 런타임이
  정확한 Relation ID, Evidence와 missing evidence를 복원한 뒤 기존 메모 validator를 적용한다.
- 실제 `gpt-5.6-terra` 실행에서 제출 필드명을 `paths`로 명확히 하고, 서로 다른 유효한
  Capability 호출을 progress로 인정하며, credential graph ID가 redaction으로 손상되지 않게
  보완했다.
- 잘못 추가했던 별도 모델 결과 적용 경로는 제거했다.
- reasoning PARTIAL 실행을 마지막 atomic checkpoint에서 같은 run 디렉터리로 재개하는
  `cicd-agent resume` 경로를 구현했다. append-only `events.jsonl`, 누적 limits/runtime 복원,
  attempt 증가, 실행 lease, prompt·repository·journal 검증과 불완전 라운드 상태/대화 rollback을
  포함하며 Codex나 LiteLLM 대화를 상태 원본으로 만들지 않는다.
- Windows run lease의 프로세스 생존 확인은 신호를 보내는 `os.kill(pid, 0)` 대신 Win32
  process handle과 exit code 조회를 사용한다. pytest와 Codex에 `CTRL+C`가 전파되던 문제를
  제거했다.
- reasoning 분석에 `--objective`와 `--objective-file`을 추가했다. 파일 입력은 분석 저장소
  밖의 trusted operator source만 허용하고 stdin은 `--objective-file -`에서만 명시적으로
  소비한다. Objective는 16,000자 한도와 정규화를 적용하며 deterministic 모드에서는 거부한다.
- Objective는 system prompt에 concat하지 않고 매 모델 라운드 별도 user-level instruction으로
  투영한다. LiteLLM 경로에서는 compactable history 밖에서 재주입하고 Codex decision context는
  `trusted_agent_instructions`, `user_objective`, untrusted `analysis_state/events`를 구분한다.
- checkpoint에 exact Objective와 SHA-256 hash를 저장하고 resume에서 복원·검증한다. Objective
  변경이나 hash tampering은 거부하며 report는 재현성을 위해 Objective/hash를 기록하되
  event/fallback trace metadata와 model trace에는 원문 Objective를 반복 저장하지 않는다.
- Objective Phase 10으로 deterministic compiled Goal을 추가했다. `fork_pr`, artifact/cache,
  local/reusable action, secret/OIDC/runner/dependency/permission 및 credential sink 같은 제한된
  source/mechanism/sink 힌트를 추출하고 LiteLLM/Codex decision context에 동일하게 투영한다.
  `ListSecurityFacts`는 이 힌트와 일치하는 Relation을 stable relevance ordering으로 앞세우지만
  Relation 내용/verdict/Evidence와 Validator semantics는 그대로 유지한다.
- checkpoint와 `analysis.json`에 compiled Goal을 기록하고 resume에서 Objective로 다시 컴파일한
  결과와 일치하는지 검증한다. compiled Goal tampering도 거부하며 `summary.md`에는 raw Objective
  전체를 복제하지 않고 bounded Objective preview/hash와 compact focus만 표시한다.
- GitHub Actions semantic IR을 추가했다. `TriggerExecutionContext`, `JobExecutionContext`와
  `NONE/READ/WRITE/UNKNOWN` permission lattice가 parser syntax와 분리되어 event별 실행 가능성,
  effective `GITHUB_TOKEN` 권한 및 local reusable-workflow caller ceiling을 deterministic하게
  계산한다. `workflow_run` consumer는 producer 권한을 복사하지 않고 새 context에서 권한을
  재계산한다.
- 보안 판정 계층에서 parser의 legacy `Job.permissions_effective` 직접 사용을 제거했다. 이 필드는
  parser/model 호환용으로만 남고 Security Facts, identity, scenario rules 및 global attack graph는
  execution semantic state/Relations를 authoritative source로 사용한다.
- 현재 GitHub.com의 2026 Actions hardening을 semantic layer에 반영했다. supported floating-major
  `actions/checkout`은 `pull_request_target`/fork-derived `workflow_run`의 흔한 fork-head checkout을
  기본 차단하고 명시적 unsafe opt-out/manual fetch는 별도 처리한다. Cache는 `pull_request`의
  merge-ref scope는 read-write로 유지하되 `pull_request_target`, `issue_comment`, `workflow_run`
  default-branch context는 read-only로 모델링한다.
- semantic-state resume 비호환을 막기 위해 prompt/runtime contract를 1.3으로 올렸다.

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
- 상태 투영 표적·계약·통합 검사 9개가 통과했고, 오래된 대화와 로컬 repository root가 Codex
  입력에서 제외되며 최신 Tool 결과와 구조화 상태가 유지됨을 확인했다.
- `gpt-5.6-luna` GHA-01 supported 실모델 재검증: 4 rounds, 90,555 reported tokens,
  4 Capability calls, 반복 0회, `COMPLETE / proposal_validated`, hidden oracle 1/1 통과.
- 최초 GHA-01 실행에서는 working-memory revision 1이 생성됐고 Validator가 잘못된 proposal을
  차단했다. 제출 계약 보강 전 실행이므로 최종 결과는 PARTIAL이었다.
- GHA-06 supported에서 후속 조사 중 메모 생략과 `ListWorkflows` 반복을 관측해 후속 조사
  메모를 강제했고 malformed Tool JSON을 bounded retry 대상으로 보완했다.
- compact proposal 적용 후 `gpt-5.6-luna` GHA-06 supported 재검증: 3 rounds, 68,180
  reported tokens, Capability 3회, 반복 0회, `COMPLETE / proposal_validated`, hidden oracle 1/1
  통과. Luna는 짧은 Relation 참조로 Azure/GCP OIDC 경로 2개를 제출했고 deterministic expander와
  기존 Validator가 정확한 Relation/Evidence 및 CANDIDATE 상태를 확정했다.
- `gpt-5.6-luna` GHA-01 candidate 검증: 3 rounds, 67,367 reported tokens, Capability 3회,
  반복 0회, working-memory revision 1, `COMPLETE / proposal_validated`, 상태와 missing evidence
  검증 1/1 통과.
- `gpt-5.6-luna` GHA-01 rejected 검증: 3 rounds, 65,285 reported tokens, Capability 3회,
  반복 0회, `COMPLETE / proposal_validated`, FALSE 관계가 REJECTED로 확정되고 1/1 통과.
- Execution semantics, current checkout/cache policy 회귀까지 포함한 전체 pytest: 167 passed,
  1 skipped(168 collected). skip은 Windows symlink 권한 조건이다.
- `uv lock --check`, Ruff 및 `git diff --check`: 통과.
- checkpoint/event journal 및 resume CLI 독립 smoke, 변경 파일 compileall·Ruff,
  `git diff --check`: 통과.

## Partial or Unverified

- **UNVERIFIED**: 외부 자격 증명이 필요한 실제 LiteLLM provider 인증과 API 호출
- **UNVERIFIED**: LiteLLM provider의 token/cost 집계 및 외부 rate-limit 동작
- **UNVERIFIED**: 새 retry, pagination, context compaction 경로를 포함한 실제 provider
  reasoning matrix 실행
- **PARTIAL**: 구조화 working memory 생성과 Validator 결합은 Luna에서 관측했지만 복잡한
  추론의 완료율·토큰 절감 효과는 matched matrix 실행 전까지 입증되지 않았다.
- **PARTIAL**: working memory의 짧은 참조와 자동 grounding은 Luna Candidate 실행에서 확인했지만,
  여러 차례의 장기 후속 조사가 필요한 대형 저장소에서의 효과는 더 검증해야 한다.
- **PARTIAL**: execution semantics는 현재 GitHub.com 정책을 기준으로 한다. 분석 대상이 구형 GHES인
  경우 checkout/cache hardening 적용 여부는 서버 버전에 따라 달라질 수 있으나 현재 CLI에는
  platform/version 입력이 없으므로 해당 차이를 자동 해석하지 않는다.
- Codex SDK는 실행별 통화 비용을 제공하지 않아 보고서의 cost `0.0`은 무료 사용 증거가 아니다.
- hackingBuddyGPT 내부에서 Pydantic v2 deprecated `dict()` 경고가 발생하지만 테스트는 통과한다.

## Key Decisions

- 공통 Fact/Relation/Guard/Evidence 추출까지는 두 모드가 공유한다. deterministic은 rule 기반
  결론 생성, reasoning은 fact graph 기반 LLM 경로 추론을 담당하며 서로의 Finding/AttackPath를
  입력으로 사용하지 않는다.
- 현재 `relation_id`의 semantic key는 기존 tuple과 동일하게 유지하고 provenance/scope가 실제로
  필요해질 때 키를 확장한다.
- compiled Objective Goal은 탐색 우선순위 힌트일 뿐 보안 사실이 아니다. 사용자 Objective가
  취약함을 가정해도 해당 힌트는 Fact/verdict/status를 생성할 수 없고 최종 판단은 기존
  Relation/Evidence와 deterministic Validator만 수행한다.
- 실제 외부 provider API와 비용이 필요한 검증은 자격 증명과 사용 한도가 있을 때 수행한다.
- 다중 provider key가 있으면 `auto`로 추측하지 않고 `--provider`를 요구한다.
- 분석 대상 저장소의 `.env`는 인증 구성으로 읽지 않는다.
- ChatGPT 구독은 API key로 변환하지 않고 Codex SDK의 저장 로그인 경로로만 사용한다.
- Codex는 별도 분석 Agent가 아니라 hackingBuddyGPT Agent의 LLM 호환 adapter다.
- 정적 경로는 CONFIRMED가 될 수 없고 cloud trust가 없으면 CANDIDATE/UNKNOWN을 유지한다.
- runtime 개선은 외부 패키지를 추가하거나 공개 코드를 복사하지 않고 표준 라이브러리로
  재구현한다. 참고한 공개 설계와 프로젝트는 `docs/AGENT_RUNTIME.md`에만 기록한다.
- checkpoint는 구조화 상태, working memory, 누적 예산과 runtime 통계를 복원하는 safe-round
  resume 원본이다. 모델 대화 전체를 복원하지 않으며 불완전 라운드는 버리고 다시 수행한다.
- `AnalysisState`와 canonical hackingBuddyGPT history만 상태 원본으로 둔다. Codex persistent
  thread를 추가하지 않으며, 누락된 과거 Fact는 bounded Capability로 다시 조회한다.
- 자연어 Objective는 `AnalysisState`의 Fact가 아니라 immutable run configuration이다. system
  policy/Validator보다 낮고 repository/tool 데이터보다 높은 trust level을 가지며 Fact extraction
  자체를 바꾸지 않는다. resume 중 Objective 변경 대신 새 analysis run을 시작한다.
- working memory는 추론 원문이나 최종 Finding 저장소가 아니다. 사용자에게 설명 가능한 짧은
  가설·근거·미확인 정보·다음 확인 작업만 보존하며 최종 결과 validator를 우회하지 않는다.
- 새 ChatGPT-login Codex 실모델 검증이 필요할 때는 `gpt-5.6-luna`를 명시한다. 기본 검증은
  모델 사용량이 없는 unit/integration/fake-LLM 테스트로 수행한다.

## Recommended Next Actions

1. 새 execution semantic graph를 이용하는 goal-aware graph frontier Capability를 구현한다.
2. Guard/contradiction 탐색과 working-memory hypothesis scoring/pruning/stop policy를 결합한다.
3. Validator 부분 repair와 대형 실제 저장소/real-model A/B benchmark로 정확도·토큰 효과를 측정한다.

## Relevant Artifacts

- `cicd-redteam-agent-mvp/PLANS.md`: 구현 범위와 완료 조건
- `cicd-redteam-agent-mvp/docs/`: 아키텍처, 계약, 테스트 및 계획 커버리지
- `cicd-redteam-agent-mvp/docs/AGENT_RUNTIME.md`: runtime 정책과 참고한 공개 구현
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/framework/working_memory.py`: bounded 가설
  patch 검증과 Capability 결합
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/framework/runtime_support.py`: atomic checkpoint,
  append-only event journal, cumulative limit 복원과 run lease
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/domain/proposal_draft.py`: 짧은 Relation 참조를
  기존 validator용 완전한 proposal로 확장
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/domain/execution_semantics.py`: event-scoped 실행,
  permission lattice, reusable/workflow_run 및 current checkout/cache semantics
- `cicd-redteam-agent-mvp/src/cicd_redteam_agent/domain/attack_graph.py`: 전역 경로 조합
- `cicd-redteam-agent-mvp/output/real-model/20260824T160152.198638Z/`: real-model 결과
- `cicd-redteam-agent-mvp/output/working-memory-luna-retry/matrix-evaluation/evaluation.json`:
  Luna GHA-01 성공 지표
- `cicd-redteam-agent-mvp/output/working-memory-luna-complex-final/matrix-evaluation/evaluation.json`:
  compact proposal 적용 전 Luna GHA-06 실패 지표
- `cicd-redteam-agent-mvp/output/compact-proposal-luna-gha06/matrix-evaluation/evaluation.json`:
  compact proposal 적용 후 Luna GHA-06 성공 지표
- `cicd-redteam-agent-mvp/output/compact-facts-luna-statuses/matrix-evaluation/evaluation.json`:
  compact Fact/working-memory 적용 후 Luna GHA-01 Candidate 성공 지표
- `cicd-redteam-agent-mvp/output/compact-facts-luna-rejected/matrix-evaluation/evaluation.json`:
  compact Fact 적용 후 Luna GHA-01 Rejected 성공 지표

## Updated

- Date: 2026-08-28
- Workflow: vibe-code-workflow + task-state-sync
- Workspace: `C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent`
