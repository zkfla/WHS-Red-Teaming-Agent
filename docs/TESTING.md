# Test specification

> The domain-specific scenarios below are historical CI/CD donor tests, not
> the current Generic Core acceptance criteria. No live target, provider or
> external CI workflow is exercised by the current offline release procedure.

## Current Generic Core checks

From the `redteam_core` worktree:

```text
uv sync --dev
uv run python -m pytest
uv run ruff check .
uv run python -m compileall -q src
uv build
```

Use the [performance plan](GENERIC_CORE_PERFORMANCE_EVOLUTION_PLAN.md),
[G9 receipt](GENERIC_CORE_G9_RECEIPT.md) and
[acceptance audit](GENERIC_CORE_PERFORMANCE_ACCEPTANCE.md) for current regression,
independent oracle, fault, determinism and benchmark evidence. Performance runners
must run separately from correctness tests. TIMEOUT/RESOURCE_LIMIT and withheld
default-enable results are preserved; a fake loop is not live-provider validation.
The approved offline G9 source is `7293c3e3ca79ba3177f27ee5a52c48c41719e7573996f1325a522b54b9945ce5`;
see the acceptance audit for the separated full-suite and post-approval hash gates.

## Historical donor test specification

## Required deterministic coverage

- workflow-free repository and ordinary CI
- YAML 1.2 `on`, malformed YAML and job permission override
- fork PR and `pull_request_target` checkout provenance, including current floating-major checkout fork protection and explicit unsafe opt-out
- event-scoped execution contexts, job/step reachability and effective permission lattice
- local reusable-workflow caller permission ceiling and fresh `workflow_run` consumer context
- cache write policy: `pull_request` merge-ref scope remains read-write while low-trust default-branch triggers are read-only
- local composite action, reusable workflow, static script and reference cycle
- dynamic expression path preserved as UNKNOWN
- Secret/GITHUB_TOKEN reference and redaction
- AWS OIDC baseline UNKNOWN, matching Allow TRUE, nonmatching/explicit Deny FALSE, custom subject UNKNOWN
- read-only AWS resolver의 exact role/provider binding과 credential 비영속화
- prompt injection text treated as data
- path traversal, symlink escape, binary, oversized file, slice and search limits

## Scenario coverage

- PR expression shell injection
- PR artifact consumed and executed by privileged `workflow_run`
- mutable external action combined with privileged context
- broad `GITHUB_TOKEN` permissions resolved from execution-context authority rather than parser-level permission summaries
- secure counterparts must not produce their scenario Finding
- composed path must reach a credential or cloud identity only through validated edges
- inference-flow fixtures cover SUPPORTED, CANDIDATE/UNKNOWN and secure guard variants
- env and step-output value flow reaches a command interpreter and granular token permission
- a blind local-action fixture is represented through generic source, execution and credential facts
- proposed paths with unknown `relation_id`, mismatched relation shape, or unresolved UNKNOWN facts are rejected
- GHA-01 through GHA-10 reasoning labels each have supported/exposed, candidate/unknown and rejected/secure repositories
- every matrix relation carries existing Evidence and every UNKNOWN relation names missing Evidence
- every proposal-ready Frontier is source-to-sink continuous and uses only existing Relation refs
- trigger-only authority routes remain non-proposal-ready without attacker-controlled influence
- Frontier traversal keeps trigger/job contexts separate unless a registered bridge changes context
- applicable hard guards and FALSE flow Relations terminate the candidate as BLOCKED/REJECTED
- PARTIAL Frontier rows are diagnostic only; deterministic limits and result truncation are explicit
- focused OIDC/cloud goals reach the cloud identity sink without changing state Facts or verdicts
- candidate scoring is deterministic and decomposes into readiness, impact, grounding,
  information gain, Objective alignment and cost; portfolio selection is stable, unique and bounded
- observation readiness wins over model work when runtime can continue; incomplete search remains
  INVESTIGATE; sufficiently grounded proposal-ready paths or a proven empty result transition to
  SUBMIT; configured limits, insufficient token reserve, repeated calls and no-progress transition
  to STOP_PARTIAL before another model round
- matrix graph composition checks the expected SUPPORTED/CANDIDATE/REJECTED outcome without product-side GHA dispatch
- the unlabeled blind fixture joins fork PR -> local action -> artifact/cache -> privileged workflow -> GitHub Token/OIDC
- FALSE guard edges are accepted only in REJECTED paths; path-level Evidence must cover every edge
- canonical `SecurityCandidate` parity holds between deterministic materialization and reasoning
  initialization, including ID/kind/root/status ceiling/supporting Relation grounding
- mutable-action candidates are grounded in non-structural action Relations, retain existing
  Relation `EvidenceRequirementRef` entries, and separate root-cause family from action/ref
  instance identity
- candidate-backed ACCEPTED decisions materialize only the bounded claim contract, while forbidden
  secret-log overclaims are rejected
- fork PR revision plus repository execution without an explicit write/secret/OIDC relation cannot
  become a privileged `SUPPORTED` claim
- broad portfolios require terminal accounting for every selected Candidate/Frontier; focused
  candidate decisions may use the explicit one-item submission gate
- semantic recompute preserves terminal coverage, marks disappeared Candidates `BLOCKED_EXTERNAL`,
  and discards stale bound WorkingMemory

## Agent and failure coverage

- fake LLM performs list, inspect and a non-empty valid submit
- malformed proposal returns a structured error without mutation
- duplicate submit is rejected
- model/rate-limit exception produces `model_error`
- repeated calls and no-progress are normalized to `STOP_PARTIAL` with `no_progress`
- round/token/cost/duration limits and insufficient next-round token reserve are normalized to
  `STOP_PARTIAL` while preserving the existing external termination reason and PARTIAL report
- reasoning PARTIAL exits preserve fact/Evidence/identity state without prepopulating deterministic Findings or AttackPaths
- deterministic mode retains existing rule-based Finding/AttackPath regression behavior
- trusted `.env` loading and process-environment precedence
- multiple provider keys require an explicit choice and model/provider mismatch is rejected
- analyzed-repository `.env` cannot become an authentication source
- Codex SDK contract surface and shared hackingBuddyGPT adapter references are checked statically
- Codex runtime disables inherited plugins and configured MCP servers
- semantic graph identifiers survive redaction while assignment-style secret values do not
- working-memory patches update a grounded hypothesis in the same Capability round
- unknown Evidence/Relation IDs, FALSE supporting Relations and stale revisions are rejected
- semantic no-op memory patches do not increase revision or bypass repeated-action detection
- canonical reconciliation promotes grounded viable hypotheses and automatically discards stale,
  contradicted, hard-blocked and duplicate-grounded hypotheses without revision churn
- LiteLLM-style message input and Codex decision context receive the same current working memory
- reasoning Objective text/file/explicit-stdin inputs normalize to one canonical instruction; empty,
  oversized and analyzed-repository Objective files are rejected
- deterministic mode rejects Objective input; reasoning without an Objective preserves existing behavior
- the Objective is re-injected every model round outside compactable history, remains subordinate to
  system policy, and is distinct from untrusted state/tool data in both LiteLLM and Codex paths
- the deterministic Objective compiler extracts only bounded source/mechanism/sink investigation
  hints; goal-relevant Fact ranking never mutates Relation verdicts or validator semantics
- checkpoint/resume preserves the exact Objective and compiled Goal and rejects Objective/hash/Goal
  tampering
- report output records Objective/hash/compiled Goal for reproducibility while trace/event logging
  does not repeat the raw Objective
- PARTIAL output preserves active hypotheses without treating them as Findings
- short Relation references expand to exact immutable nodes, edges and Evidence
- unknown, duplicate and discontinuous compact path references are rejected before state mutation
- compact drafts still pass the ordinary AnalysisProposal Validator before atomic submission
- the submission gate rejects pre-SUBMIT calls and paths outside the selected portfolio while
  allowing an empty proposal only after a complete search proves no grounded path remains
- compact Fact output omits long grounding IDs unless compatibility mode is requested
- working-memory Relation refs restore exact Evidence and missing Evidence; unknown refs do not mutate state
- compact drafts derive CANDIDATE from UNKNOWN and REJECTED from FALSE without accepting a model status override
- P0 pinned GitHub Actions Goat benchmark verifies deterministic/reasoning Candidate parity,
  six-item diversified portfolio accounting, candidate-backed submission, and no silent drops
- checkpoint replacement is atomic and restores AnalysisState, working memory, limits and runtime metrics
- observation checkpoints round-trip KnowledgeState, Task attempts and normalized Observations; missing
  sessions, divergent canonical state, invalid Task/Observation bindings and active leases fail closed
- Frontier job의 concrete Environment는 중복 없이 Task로 선택되고, normalized protection policy가
  unknown guard를 reviewer/branch/custom-rule별 `MAY_BLOCK` guard로 재계산하며 reviewer identity는
  Observation에 보존하지 않는다
- event journal sequence is append-only; only a malformed final line is repairable
- failed rounds roll back state, submission state and partial Agent history before checkpointing
- resume reuses the same run directory and attempt lineage while preserving cumulative budgets
- completed runs, Objective hash or compiled-Goal mismatch, commit drift, dirty repositories,
  stale/corrupt journals and concurrent leases are rejected

## P0 pinned Goat benchmark

The repeatable offline harness is `tests/benchmarks/p0_goat_benchmark.py`. It accepts only a clean
checkout of `https://github.com/step-security/github-actions-goat.git` at commit
`4da23c8602b4589bacdf7de77240113e02cdb884`, runs no workflow or action, and writes a structured
`benchmark.json` with workflow/job/step, Evidence/Relation, Candidate, portfolio, disposition,
Finding/AttackPath and reasoning-smoke metrics.

```powershell
uv run python tests/benchmarks/p0_goat_benchmark.py `
  --goat-root output/benchmark-target/github-actions-goat `
  --output output/p0-goat-benchmark
```

The reasoning metric is explicitly a provider-free framework smoke: it starts from the same
Candidate projection, runs the real portfolio/coverage gate and candidate-backed `SubmitAnalysis`,
and records zero external LLM rounds, observations and tokens. It is not a live provider-accuracy
claim.

## Generic Evidence A-G acceptance coverage

- selected Frontier relation만 typed `EvidenceRequirement`로 compile되고 resolver 선택에 LLM을
  사용하지 않는다
- `ResolverRegistry`는 provider/kind 계약, 직렬 실행, resolver당 최대 2 GET과 model 전 최대
  3 Observation을 유지해 cycle당 최대 6 GitHub GET 상한을 보장한다
- successful GET dedupe, ETag/Last-Modified conditional request, `304` representation reuse와
  checkpoint/resume 재검증이 semantic Evidence와 operational transport cache를 섞지 않는다
- 403 permission/rate-limit, 404 ambiguity, 429, timeout, bounded retry 및 budget exhaustion이
  Evidence를 날조하지 않고 기존 `UNKNOWN`을 보존한다
- repository metadata/default permission, fork PR, Actions, effective branch rules, Environment
  policy/pattern, pending deployment와 workflow-run jobs payload가 discriminated schema로
  checkpoint round-trip된다
- workflow-run jobs는 static job key/concrete name과 runtime name의 unique exact match만 허용한다;
  matrix/reusable/dynamic/duplicate name과 불완전 100-job page는 fail-closed 처리한다
- 서로 다른 실행 완료 `runner_id`는 해당 cross-job runner-state 경로를 `FALSE`로 닫고, 같은
  `runner_id`는 identity missing만 제거한 채 cleanup/ephemerality/workspace cleanup을 `UNKNOWN`으로
  남긴다

## P0 live GitHub actor/run acceptance

`tests/live/test_github_actor_resolver_live.py`는 실제 GitHub REST API를 사용하는 명시적
read-only acceptance suite다. `live_github` marker는 기본 pytest 선택에서 제외되므로 일반
unit/integration/release 검증은 네트워크나 GitHub credential 유무에 영향을 받지 않는다.

필수 입력은 `CICD_LIVE_GITHUB_REPOSITORY=owner/repository`,
`CICD_LIVE_GITHUB_TOKEN`, `CICD_LIVE_GITHUB_RUN_ID`다. 기존 rerun을 검증하려면
`CICD_LIVE_GITHUB_RERUN_ID`, 실제 404 분류를 검증하려면 통제된 저장소에서 존재하지 않는
`CICD_LIVE_GITHUB_MISSING_RUN_ID`를 선택적으로 제공한다. 토큰은 fine-grained read-only로
제한하고 테스트 산출물·로그·repr에 기록하지 않는다.

```powershell
uv run pytest -q -m live_github tests/live/test_github_actor_resolver_live.py
```

P0 freeze gate는 다음을 모두 요구한다.

- 실제 workflow-run 응답의 `id`, `event`, `actor`, `run_attempt`가
  `WorkflowRunIdentityEvidence`와 일치한다.
- 현재 actor의 collaborator permission이 `RepositoryActorPermissionEvidence`로 정규화되고
  `admin/write -> TRUE`, `read/none -> FALSE`의 current authorization으로 recompute된다.
- 기존 rerun fixture가 있으면 `triggering_actor` 조건 자체의 truth와 privilege authority를
  분리해 `github.triggering_actor`만으로 authorization을 증명하지 않는다.
- live 404는 `AMBIGUOUS_NOT_FOUND`로 분류한다. synthetic runtime 회귀
  `test_ambiguous_404_is_not_committed_as_evidence`가 KnowledgeState 미커밋/UNKNOWN 보존을
  계속 증명해야 한다.
- 첫 live GET의 ETag 또는 Last-Modified가 있으면 checkpoint/resume 재조회가 conditional
  header를 사용한다. GitHub가 `304`를 반환하면 cached representation을 재사용하고,
  변경 없는 `200` fallback도 동일 typed Evidence를 유지해야 한다.
- synthetic 403/429/retry/budget 회귀가 모두 통과한다. live suite는 rate limit을 의도적으로
  소진하거나 mutation endpoint를 호출하지 않는다.
- live transport는 GET 경로만 사용하며 workflow rerun/dispatch, collaborator 변경,
  repository 설정 변경 등 POST/PATCH/PUT/DELETE 작업은 P0 acceptance 범위 밖이다.

2026-08-29 controlled fixture `aoistar-kr/cicd-redteam-p0-fixture`의 run `33216265987`을
`run_attempt=2`로 rerun한 뒤 repository-scoped fine-grained read-only PAT로 credential-backed
live suite를 다시 실행했고 4개 케이스가 모두 통과했다. 검증 범위는 normal
actor/current-permission semantic recompute, rerun `triggering_actor` 비권한주체 처리, live 404
ambiguity, checkpoint/resume revalidation이다. 같은 시점에 synthetic 403/429/retry/budget/404
commit-safety 회귀 6개도 다시 통과했다. 따라서 P0 actor/run/current-permission slice는
`live-validated/frozen`으로 본다.

현재 rerun은 동일 계정이 최초 실행과 rerun을 모두 수행해 `actor == triggering_actor`였다. 따라서
서로 다른 두 principal이 실제 GitHub 응답에서 갈리는 장면 자체는 아직 관측하지 않았다. 다만 P0
freeze는 `triggering_actor` field 수집과 이를 privilege authority로 승격하지 않는 의미론까지를
고정하며, distinct-principal live fixture는 후속 provenance 강화를 위한 선택적 검증으로 남긴다.

## P1 live GitHub event-principal acceptance

`tests/live/test_github_event_principal_live.py`는 P1 event linkage/principal resolver의 실제 GitHub
GET-only acceptance suite다. fixture 준비 단계에서는 통제된 private 저장소에 issue/comment/PR/label/
review/review-comment 이벤트를 만들지만, acceptance 실행 자체는 mutation endpoint를 호출하지 않는다.

필수 환경변수는 다음과 같다. 토큰 값은 로그·repr·테스트 산출물에 기록하지 않는다.

```text
CICD_P1_LIVE_GITHUB_REPOSITORY
CICD_P1_LIVE_GITHUB_TOKEN
CICD_P1_LIVE_GITHUB_ACTOR_LOGIN
CICD_P1_LIVE_GITHUB_ACTOR_ID
CICD_P1_LIVE_GITHUB_ISSUE_NUMBER
CICD_P1_LIVE_GITHUB_COMMENT_ID
CICD_P1_LIVE_GITHUB_ISSUE_COMMENT_RUN_ID
CICD_P1_LIVE_GITHUB_PULL_NUMBER
CICD_P1_LIVE_GITHUB_PULL_REQUEST_RUN_ID
CICD_P1_LIVE_GITHUB_LABEL_RUN_ID
CICD_P1_LIVE_GITHUB_REVIEW_ID
CICD_P1_LIVE_GITHUB_REVIEW_RUN_ID
CICD_P1_LIVE_GITHUB_REVIEW_COMMENT_ID
CICD_P1_LIVE_GITHUB_REVIEW_COMMENT_RUN_ID
```

```powershell
uv run python -m pytest -q -m live_github tests/live/test_github_event_principal_live.py
```

P1 freeze gate는 다음을 요구한다.

- workflow run과 event resource를 exact locator 없이 시간·actor 유사도로 추정하지 않는다.
- comment, PR, review, review-comment author와 event sender/label actor를 서로 다른 principal role로
  보존한다.
- `author_association`은 condition data일 뿐 repository permission으로 승격하지 않는다.
- exact resource GET이 반환한 principal만 `EventPrincipalEvidence`로 커밋한다.
- label action 관측은 해당 action 수행 사실만 증명하며 write permission을 증명하지 않는다.
- current repository permission은 P0 resolver를 재사용하고 historical event-time permission은 증거가
  없으면 `UNKNOWN`으로 유지한다.
- `pull_request_target`은 workflow-run event가 이를 증명할 때 webhook-compatible `pull_request` payload
  shape를 허용하며, 그 외 event-name mismatch는 fail-closed 한다.
- 401/403/404/429/timeout/budget 실패는 affirmative Evidence를 만들지 않는다.
- 자동 조사 경로는 linkage → event principal → current permission 순서로 최대 3 Observation 안에서
  semantic recompute까지 도달한다.

2026-08-29 private fixture `aoistar-kr/cicd-redteam-p1-fixture`에서 실제 이벤트를 생성해 다음 resource와
run을 고정했다: issue `#1`, comment `5458717964`, PR `#2`, review `5055762841`, review-comment
`3884626896`; issue-comment run `33219599472`, pull-request run `33219603169`, label run
`33219605429`, review run `33219607365`, review-comment run `33219609251`. live acceptance는 6/6
통과했고, targeted P1 offline vertical은 65/65 통과했다. 전체 프로젝트 회귀도 통과했으므로 P1
Event principal/current-authorization slice를 `live-validated/frozen`으로 본다. historical effective
permission reconstruction은 이 freeze의 범위가 아니다.

## P2 live GitHub status/dependency acceptance

`tests/live/test_github_status_semantics_live.py`는 P2 scheduler/status 의미론을 실제 GitHub.com
hosted execution과 GET-only evidence resolution으로 검증한다. fixture 준비 단계에서는 workflow를
명시적으로 dispatch하고 cancellation fixture 한 건을 취소하지만, acceptance harness 자체는 GET만
사용한다.

필수 환경변수는 다음과 같다.

```text
CICD_P2_LIVE_GITHUB_REPOSITORY
CICD_P2_LIVE_GITHUB_TOKEN
CICD_P2_LIVE_GITHUB_STATUS_RUN_ID
CICD_P2_LIVE_GITHUB_CANCEL_RUN_ID
```

```powershell
uv run python -m pytest -q -m live_github tests/live/test_github_status_semantics_live.py
```

P2 freeze gate는 다음을 요구한다.

- `if` raw expression truth, implicit status gate, dependency gate, runtime result와 job reachability를
  하나의 boolean으로 합치지 않는다.
- status function 없는 job condition에는 implicit `success()` semantics를 적용하고 failed/skipped
  dependency가 ordinary downstream job을 막는 것을 재현한다.
- `always()`는 implicit success gate를 제거하지만 unrelated runtime UNKNOWN을 TRUE로 만들지 않는다.
- explicit `needs.<job>.result`는 `success/failure/cancelled/skipped`만 typed result로 사용한다.
- `failure()`는 transitive failed ancestor를 반영할 수 있어야 한다.
- step `outcome`과 `conclusion`은 분리하고 `continue-on-error` 이후 REST conclusion을 raw outcome으로
  역추론하지 않는다.
- workflow-jobs runtime evidence는 workflow-run identity의 exact workflow path에 먼저 묶고, 그
  workflow 안에서 unique exact job/step name만 연결한다.
- final run `cancelled`를 모든 과거 `cancelled()` condition의 truth로 소급하지 않는다.
- `needs.*.result` wildcard는 live hosted behavior를 기록하되 portable core semantics로 일반화하지
  않고 `UNKNOWN`을 유지한다.
- 401/403/404/429/timeout/budget 실패와 ambiguous runtime matching은 affirmative Evidence를 만들지
  않는다.

2026-08-29 private fixture `aoistar-kr/cicd-redteam-p2-fixture`에서 status run `33236505618`과
cancellation run `33236538249`를 생성했다. status run은 `Skipped=skipped`, ordinary
`Implicit after skip=skipped`, `Always after skip=success`, `Failing=failure`, `Failure handler=success`,
`Implicit after failure=skipped`, `Middle=success`, `Ancestor failure handler=success`, 그리고 direct
dependency가 success인데 indirect ancestor만 failure인 `Wildcard indirect=success`를 실제 hosted
GitHub에서 관측했다. `Soft fail job`에서는 failed step의 `continue-on-error` 이후 job 및 REST step
conclusion이 success인 것도 확인했다. cancellation run은 upstream `Long running=cancelled` 뒤
`On cancel=success`, `Always after cancel=success`를 관측했다.

GET-only P2 live acceptance는 2/2 통과했고 targeted P2 status/evidence regression은 64/64 통과했다.
전체 기본 pytest도 failure 없이 통과했으며 Ruff, `uv lock --check`, `git diff --check`가 모두
exit 0이었다. 따라서 P2 status/dependency slice를 `live-validated/frozen`으로 본다.

## P4 condition satisfier offline gate

P4는 다음을 자동 검증한다.

- expression truth/status reachability와 `ConditionSecurityEffect`를 독립적으로 직렬화한다.
- `false && unsupported()`와 `true || unsupported()`는 UNKNOWN branch를 안전하게 short-circuit한다.
- comment/body와 `head_ref` 같은 공격자 입력, repository-owner trust, literal actor,
  `triggering_actor`, author association과 label/revision binding을 서로 다른 의미로 판정한다.
- PR merge ref의 symbolic prefix/suffix 및 exact push/base branch filter만 결정적으로 평가한다.
- attacker-controlled predicate의 모순 가능성은 같은 operand가 중복 제약되면 `UNKNOWN`으로 둔다.
- condition Fact는 `attacker_can_satisfy_condition` 또는 canonical
  `guard_blocks_untrusted_execution`만 생성하며 레거시 generic UNKNOWN 관계는 생성하지 않는다.
- GHA-10 supported fixture가 generic actor/comment UNKNOWN 대신 attacker-satisfiable path를 요구한다.

## Release checks

```powershell
uv sync
uv run python -m pytest -q
uv run ruff check .
uv run cicd-agent analyze tests/fixtures/oidc-risk --output <temporary-output>
uv run cicd-agent evaluate tests/fixtures/reasoning-matrix --output <temporary-output> `
  --provider codex-chatgpt --model gpt-5.6-luna --scenario gha-01
```

The ChatGPT-login smoke was executed against `tests/fixtures/global-chain` with `gpt-5.6-terra` and
completed through `proposal_validated`. A real LiteLLM provider call remains excluded until an
external provider credential and cost/usage ceiling are available.
All new Codex live tests use `gpt-5.6-luna`; the historical Terra result remains provenance rather
than the current test-model policy.

`matrix-evaluation/evaluation.json` also records observable Agent behavior per case and in aggregate:
rounds, reported tokens, Capability call/repeat counts, working-memory revisions, hypothesis counts
and final statuses, retries and context compactions. These fields support later A/B comparison but
do not claim causal token savings without matched runs.
