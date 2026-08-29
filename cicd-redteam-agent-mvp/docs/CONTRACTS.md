# Public contracts

## CLI

```text
cicd-agent analyze <repo-path> --output <dir>
  [--model <litellm-model>]
  [--provider auto|openai|anthropic|openrouter|gemini|litellm|codex-chatgpt]
  [--mode deterministic|reasoning]
  [--env-file <trusted-env-path>]
  [--attacker-profile fork-pr|repo-writer]
  [--objective <trusted-text> | --objective-file <trusted-path|->]
  [--no-mode-prompt]
  [--no-objective-prompt]
  [--observe-github]
  [--observe-aws] [--aws-profile <name>]
  [--github-run-id <positive-integer>]
  [--max-rounds N] [--max-tokens N] [--max-cost N] [--max-duration N]

cicd-agent evaluate <matrix-path> --output <dir>
  [--model <litellm-model>] [--provider ...]
  [--scenario gha-01] [--variant supported|candidate|rejected]
  [--max-rounds N] [--max-tokens N] [--max-cost N] [--max-duration N]

cicd-agent resume <run-dir>
  [--env-file <trusted-env-path>]
  [--max-rounds N] [--max-tokens N] [--max-cost N] [--max-duration N]
```

`evaluate` always uses reasoning mode. It analyzes repositories before loading each adjacent hidden
oracle, writes ordinary per-run reports, and emits `matrix-evaluation/evaluation.json`. Fixture GHA
labels and expected states are never passed to the Agent.

Exit codes are `0` COMPLETE, `2` input/configuration error, `3` framework/model initialization
error, `4` PARTIAL and `5` internal validation failure. Natural-language Objective input is accepted
only for reasoning analysis. In an interactive terminal, omitting mode, model and provider prompts
for basic attack-path analysis or recommended AI-assisted deep analysis. The operator must select
1 or 2, and interactive reasoning selects the saved Codex ChatGPT login.
Explicit mode/provider/model options and non-interactive runs retain their existing behavior, and
`--no-mode-prompt` preserves the non-interactive default in a terminal. When a reasoning analysis
has no explicit Objective and stdin is an
interactive terminal, the CLI prompts once for an optional trusted Objective; an empty response
selects full-scope analysis. Non-interactive stdin is never auto-consumed. `--no-objective-prompt`
disables the terminal prompt, while `--objective-file -` explicitly consumes stdin. Objective files
inside the analyzed repository are rejected so repository data cannot
be promoted into trusted operator instructions. A deterministic compiler derives bounded
source/mechanism/sink Goal hints from the normalized Objective. These hints affect investigation
ordering only; they cannot create Evidence, Relations, verdicts, guards or final findings.
`--observe-github` is an explicit reasoning-mode opt-in for deterministic read-only GitHub.com
repository-policy observation. Without the flag, analysis preserves the existing local/static network
boundary. The observer reads `GH_TOKEN` or `GITHUB_TOKEN` only from trusted operator configuration,
automatically accepts only a `github.com` origin, and sends the credential only to `api.github.com`.
Observation failure preserves the corresponding external state as `UNKNOWN` and does not prevent the
model round from continuing.
`--observe-aws`는 `--observe-github`와 함께 쓰는 별도 opt-in이며 AWS SDK credential chain 또는
선택한 `--aws-profile`로 IAM `GetRole`과 `GetOpenIDConnectProvider`만 호출한다. credential이나
token은 Observation에 저장하지 않는다. 액션 인증 모드, GitHub subject 형식, role/provider
evidence 중 하나라도 불완전하거나 지원하지 않는 IAM 연산자가 결과를 바꿀 수 있으면 trust는
`UNKNOWN`이다. 정적 trust `TRUE`는 실제 STS 성공이나 role permission을 의미하지 않는다.
The frozen Generic Evidence A-G contract compiles only requirements needed by the selected Frontier
portfolio, resolves them through a deterministic `ResolverRegistry`, commits a discriminated typed
Observation, and rebuilds semantic state before the next model round. The GitHub resolver pack covers
repository metadata/default workflow permission, fork PR policy, Actions policy, effective branch
rules, Environment policy/branch patterns, pending deployments and workflow-run jobs. Resolver choice
never consumes an LLM call. At most three Observations run before a model round and each resolution
allows at most two GitHub requests, so the structural ceiling is six GitHub requests per model cycle.

Bounded GitHub `if` semantics use a typed tokenizer/AST result shared by job and step execution.
Supported expressions preserve GitHub's case-insensitive string equality; unsupported syntax, loose
coercion and unavailable runtime status remain `UNKNOWN`. Workflow-run actor identity and current
repository permission are separate Evidence types. `github.triggering_actor` is never treated as the
workflow privilege authority, current permission is not retroactive proof of run-time permission, and
Evidence from a different run event cannot satisfy a condition context.

P4 preserves expression truth/reachability separately from condition satisfier security. The latter is
one of `ALWAYS_TRUE`, `ATTACKER_SATISFIABLE`, `TRUST_REQUIRED`, `BLOCKS`, or `UNKNOWN`, with independent
attacker-satisfiable, trusted-principal and exact-revision fields. `contains`, `startsWith`, and
`endsWith` are supported for bounded string/list contexts; event-aware ref values use exact trigger
filters or conservative symbolic patterns. A label or review predicate may require a trusted mutation
without proving that approval is bound to the exact evaluated revision. A literal actor comparison,
`author_association`, and `github.triggering_actor` never become repository authorization by implication.
Only proven attacker satisfiability emits `attacker_can_satisfy_condition`; all condition blocking uses
the canonical `guard_blocks_untrusted_execution` relation. Unsupported dynamic functions remain
`UNKNOWN`, although boolean short-circuit may still prove `false && unknown` false or `true || unknown`
true.

`--github-run-id` is valid only with `--observe-github` and is preserved by checkpoint/resume. Pending
deployment evidence can prove that the current run is blocked on an Environment, but an empty pending
list does not prove approval. Workflow-run job evidence matches a parsed job only when one concrete
static job key/name and one runtime job name are an exact unique pair. A concrete different
`runner_id` rejects that run's cross-job runner-state path; the same `runner_id` resolves identity only
and leaves cleanup, ephemerality and workspace sanitation `UNKNOWN`. Matrix/reusable/dynamic or
ambiguous names, unassigned jobs and observation failures remain `UNKNOWN`.

Environment Observations persist normalized policy counts and booleans, never reviewer identities.
Raw HTTP representations are operational cache state, separate from semantic Evidence. ETag or
Last-Modified validators and runtime metrics survive checkpoint/resume; restored semantic external
Evidence is fail-closed until conditional revalidation succeeds. A configured reviewer, branch policy
or custom deployment rule is a `MAY_BLOCK` guard until execution-specific evidence says otherwise.

## Output

Each run writes `<output>/<run-id>/analysis.json`, `summary.md` and `trace.jsonl`. Reasoning runs
also write atomic `checkpoint.json` schema 2.0 and append-only `events.jsonl` schema 1.0. GitHub-observation
checkpoints contain a versioned investigation-session snapshot. All public models
carry `schema_version="1.0"`. `AnalysisState` includes parsed workflow syntax plus event-scoped
`trigger_contexts` and `job_execution_contexts`; effective `GITHUB_TOKEN` authority is carried by the
execution contexts and semantic Relations rather than inferred by consumers from the parser's legacy
permission summary. Run metadata records the repository commit, attacker profile, resolved
analysis mode, configured limits, framework commit and optional provider/model identifier. When a
reasoning Objective exists, `analysis.json` records its exact normalized text, SHA-256 hash and
compiled Objective Goal; `summary.md` shows a bounded preview, hash and compact focus summary. Event
journal entries and fallback trace metadata do not duplicate the raw Objective.

`summary.md` is a human-readable view of the same report. It includes an execution overview,
deterministic coverage counts, Finding details, global and local attack paths, cloud identity
handoffs, warnings and status interpretation. Evidence references include their repository-relative
source path and line range. Repository and model-derived text is Markdown-escaped before rendering.
`analysis.json` remains the canonical machine-readable contract.

Global paths are shown in deterministic review order: supported paths first, then fewer missing
Evidence items, fewer applicable guards and shorter paths. The Markdown report derives remediation
guidance from relation types without changing the version 1.0 JSON schema.

Matrix evaluation writes `matrix-evaluation/evaluation.json` and `evaluation.md`. The scorecard
contains Fact extraction completeness, status accuracy, missing-Evidence preservation, safe-variant
false-positive checks and separately measured deterministic/reasoning wall time.

`resume` accepts only a non-terminal reasoning checkpoint produced by the current prompt contract.
It continues in the same run directory and restores cumulative rounds, tokens, cost, duration,
runtime metrics, working memory, the immutable user Objective and the saved GitHub-observation opt-in.
Observation-enabled runs also restore `KnowledgeState`, the complete Task ledger and attempt counts,
normalized Observations, and the last safe lease-free investigation state. The session's live
`AnalysisState` must exactly match the top-level canonical state. A missing session, mismatched state,
unsupported investigation schema, invalid Task/Observation binding, or active Task lease fails closed.
Optional limits are new
cumulative maxima and must exceed the already consumed value. Repository commit drift, dirty Git state, an
Objective hash or compiled-Goal mismatch, an active run lease, an older or corrupt event journal,
and completed runs are input errors. Changing the Objective requires a new analysis run. A failed/incomplete round never becomes
the resumed state; the last committed boundary is retried.

`auto` accepts exactly one configured provider key. More than one key requires explicit
`--provider`; provider/model prefix conflicts are configuration errors. Process variables take
precedence over `.env`. Automatic `.env` discovery never loads a file inside the analyzed
repository. `codex-chatgpt` requires saved ChatGPT authentication and does not consume an API key.
It supplies the LLM decisions inside the same hackingBuddyGPT round loop. When `--observe-github` is
active, a deterministic investigation task runs before a model round, commits a normalized Observation,
recomputes semantic state and synchronizes the same live `AnalysisState` object used by Capabilities,
the Agent and checkpoint writer. Observed token usage is registered with `Limits` after each turn;
monetary `max-cost` is not enforceable for this provider because the SDK does not report per-run
currency cost.

### P0 live GitHub actor/run acceptance contract

The credential-backed P0 acceptance boundary is strictly read-only. It may resolve an existing
workflow run and the run actor's current repository permission through GitHub REST GET endpoints,
then feed those typed payloads through the existing condition evaluator. It does not create or rerun
a workflow, change repository/collaborator settings, or deliberately exhaust rate limits.

`WorkflowRunIdentityEvidence.actor_*` is the workflow privilege principal used by the current
authorization slice. `triggering_actor_*` is preserved as rerun provenance but is not independently
a privilege authority. `RepositoryActorPermissionEvidence.temporal_scope` remains `current`; a live
permission lookup must never be promoted to event-time or historical authorization evidence.

A 401/403/404/429/timeout/budget failure cannot synthesize affirmative Evidence. In particular,
404 remains ambiguous rather than proving `permission=none`. Operational cache validators may be
persisted across checkpoint/resume, but ETag/Last-Modified/304 state remains transport metadata and
must not become semantic Evidence. A resumed unchanged representation must produce the same typed
payload and semantic result as the pre-checkpoint observation.

The P0 slice is frozen as live-validated after the explicit `live_github` acceptance suite and the
offline synthetic failure/budget regressions pass under the intended credential boundary. On
2026-08-29 the controlled fixture `aoistar-kr/cicd-redteam-p0-fixture` passed the full four-case live
suite with a repository-scoped fine-grained read-only PAT: normal actor/current-permission semantics,
rerun `triggering_actor` handling with `run_attempt=2`, 404 ambiguity, and checkpoint/resume
revalidation. The corresponding synthetic 403/429/retry/budget/404 commit-safety regressions also
passed. P0 therefore freezes the actor/run/current-permission contract. A live rerun where
`actor != triggering_actor` remains an optional stronger provenance observation, not evidence that
current permission is historical/event-time authorization and not a blocker for this frozen slice.

### P1 event-principal provenance contract

P1 freezes event-level principal provenance separately from the P0 workflow-run principal. A workflow
run actor, event sender, comment author, pull-request author, review author, review-comment author and
label action actor are distinct identities unless exact Evidence proves that they are the same account.
`author_association` is event metadata that may be used when a workflow condition explicitly compares
it, but it is never converted into repository permission or historical authorization.

`--github-event-file` is trusted operator input and must resolve outside the analyzed repository. The
raw webhook payload is parsed into bounded locator/principal fields and is not persisted wholesale in
semantic Knowledge. Workflow-run REST metadata alone does not reconstruct the original webhook
payload. Therefore run-to-event binding is `partial` unless an exact event locator is available; time,
actor, title or other similarity heuristics must never be promoted to Evidence.

`WorkflowRunEventLinkageEvidence` binds a run to the exact event resource when sufficient identifiers
are known. `EventPrincipalEvidence` resolves only exact resource identifiers through GitHub GET
endpoints. `pull_request_target` accepts the webhook-compatible `pull_request` payload shape when the
workflow-run event proves the target event name; unrelated event-name mismatches fail closed.
Observed event action capability is distinct from repository authorization. For example, an exact
`labeled` event proves which principal performed that label action, but does not prove `write`
permission because GitHub roles such as `triage` may perform label operations.

Current repository authorization reuses the frozen P0 `RepositoryActorPermissionEvidence` contract.
Its `temporal_scope` remains `current`. P1 does not reconstruct historical effective repository
permission at event time; when such a conclusion is required, historical authorization remains
`UNKNOWN` instead of being inferred from current permission, `author_association`, or observed action.

On 2026-08-29 the controlled private fixture `aoistar-kr/cicd-redteam-p1-fixture` exercised actual
`issue_comment`, `pull_request`, `pull_request_target` label, `pull_request_review`, and
`pull_request_review_comment` events. `tests/live/test_github_event_principal_live.py` passed all six
live cases, including exact comment/PR/review/review-comment/label principal resolution, current
permission recomputation for the comment principal, and ambiguous 404 handling. The offline P1
vertical and full project regressions also passed. P1 therefore freezes the event-linkage/principal
and current-authorization contract described above; historical authorization remains an explicit
future evidence axis rather than part of this freeze.

### P2 status/dependency execution-semantics contract

P2 freezes GitHub Actions status/dependency semantics separately from expression truth. A job's raw
`if` expression, implicit status gate, dependency eligibility, observed runtime result and final
reachability are distinct values. `ConditionEvaluation.verdict` remains the compatibility projection
of the effective condition; it must not be treated as proof that the scheduler actually ran the job.

For jobs without an explicit status function, the engine models GitHub's implicit `success()` gate.
Direct `needs.<job>.result` values are normalized only to `success`, `failure`, `cancelled`, `skipped`
or `unknown`. `failure()` may reflect failed ancestors in the transitive dependency chain, while an
explicit `always()` removes the implicit success gate without converting unrelated UNKNOWN runtime
facts into TRUE. Cycles, unresolved dependency order, matrix aggregate results and dynamic
`continue-on-error` remain fail-closed.

Step status is sequential. `steps.<id>.outcome` and `steps.<id>.conclusion` are distinct because
`continue-on-error` may turn a failed outcome into a successful conclusion. GitHub's workflow-jobs REST
response exposes step conclusions but not raw outcomes, so a successful conclusion under
`continue-on-error: true` never proves that the outcome succeeded. Runtime step-to-static-step binding
requires a unique exact explicit step name; fuzzy matching is forbidden.

`WorkflowRunJobsEvidence` preserves bounded job and step status/conclusion data. Runtime job evidence
is first bound to the exact workflow path obtained from the workflow-run identity and is then matched
inside that workflow by a unique exact static/runtime job name. `WorkflowRunStatusEvidence` preserves
the final run status/conclusion as a separate observation. A final workflow conclusion of `cancelled`
is not retroactively used to claim that every earlier `cancelled()` condition evaluated TRUE; a
condition is closed only when evaluation-time dependency/runtime evidence supports it.

Wildcard `needs.*.result` is deliberately not generalized into the core deterministic contract. On
2026-08-29 the controlled GitHub.com fixture reproduced hosted-runner behavior where
`always() && contains(needs.*.result, 'failure')` ran even though the job's direct dependency had
succeeded and only an indirect ancestor had failed. Because this conflicts with the direct-needs
mental model and is provider behavior rather than a stable typed contract, the engine preserves such
wildcard expressions as `UNKNOWN` instead of encoding the observed quirk as portable semantics.

The controlled private fixture `aoistar-kr/cicd-redteam-p2-fixture` produced status run
`33236505618` and cancellation run `33236538249`. The status run observed implicit-success skip
propagation, `always()` override, explicit `needs.<job>.result`, ancestor `failure()`, step
`continue-on-error`, and the wildcard hard-negative. The cancellation run observed a cancelled
upstream job followed by successful `cancelled()` and `always()` handlers. The GET-only
`tests/live/test_github_status_semantics_live.py` suite passed 2/2, the targeted status/evidence
regression passed 64/64, and the full project pytest/Ruff/lock/diff gates passed. P2 therefore freezes
the status/dependency execution-semantics contract above.

## Proposal validation

- Findings, paths and identities may reference only Evidence already present in `AnalysisState`.
- Every AttackPath node must exist in the state graph.
- Edges must connect consecutive ordered nodes, reference an extracted `relation_id`, match that
  Relation's source/type/target and carry its Evidence.
- `SUPPORTED` paths cannot contain `UNKNOWN` or `FALSE` relations or non-empty missing evidence.
- Candidate paths must preserve the missing evidence declared by uncertain relations.
- A path containing a `FALSE` relation must be `REJECTED`.
- A path-level Evidence set must contain every Evidence ID carried by its edges.
- Static paths cannot be `CONFIRMED`.
- A `SUPPORTED` Finding requires an Evidence-linked `SUPPORTED` path in the same proposal.
- Proposed cloud identities must match an extracted repository/workflow/job/provider/mechanism
  candidate and preserve its Evidence.
- Duplicate IDs are rejected, including collisions with existing state.
- A rejected proposal does not mutate state.
- A successful `submit_analysis` may occur only once.

## Termination

- `deterministic_analysis_complete`: parser and deterministic domain completed without a model.
- `proposal_validated`: Agent submitted one valid proposal.
- `model_error`: a reasoning-mode model round failed after the fact graph was prepared.
- `no_progress`: four repetitions of the same call or four rounds without a new call signature or
  newly inspected resource.
- `insufficient_token_reserve`: the next round is not started when its predicted token need exceeds
  the remaining cumulative token budget; the run may be resumed with a larger maximum.
- framework limit reason or `limit_reached`: round, token, cost or duration budget ended.

These normal incomplete-run reasons are first normalized into the deterministic investigation
policy as `STOP_PARTIAL` with `stop_reason`; the external termination-reason strings remain unchanged
for report/checkpoint compatibility. Model/provider exceptions remain `model_error` rather than a
normal policy transition.

All non-successful Agent termination reasons produce `PARTIAL` and preserve the reasoning-mode
fact/Evidence/identity state without injecting deterministic Findings or AttackPaths.

## Safety

Repository paths must remain below the resolved input root. `.git`, parent and absolute paths,
symlink escape, binary input and files above 1 MiB are rejected. File slices are at most 200 lines;
literal search returns at most 50 redacted results and excludes dependency/build directories.
Codex receives no direct repository path or repository tool. In an isolated temporary directory
with read-only sandbox, approval denial, plugins disabled and configured MCP servers disabled, it
selects one registered Capability per round. Only hackingBuddyGPT executes the Capability, and
`submit_analysis` uses the existing deterministic proposal validator.

The Codex adapter is stateless between decisions. Its bounded input contains the trusted Agent
policy, a provider-neutral projection of the current `AnalysisState`, and only the seven most recent
non-system Agent events. `AnalysisState` and the canonical hackingBuddyGPT history remain the source
of truth; a Codex thread is never a checkpoint or a second conversation owner.

The Agent first inspects bounded source-to-sink candidates through `ListAttackFrontiers`. Each row
contains continuous run-local Relation refs, execution context, flow labels, permission/trust
provenance, applicable guards, missing-information classes, a status ceiling and `proposal_ready`.
`PARTIAL` rows are diagnostics and cannot be submitted. `BLOCKED` applies only to the represented
execution context. `search_complete`, `truncated`, depth/expansion metadata and pagination prevent a
limited result from being interpreted as proof that no path exists. Frontier engine and Relation
semantic-registry versions are returned for reproducibility.

A deterministic investigation-policy layer scores Frontier candidates from readiness, sensitive
sink impact, Evidence grounding, resolvable information gain, Objective alignment and bounded
investigation cost. It greedily selects at most six candidates while penalizing Relation, execution
context and source/mechanism/sink overlap. Runtime budget/progress signals are normalized alongside
the Frontier state, and the resulting transition is `OBSERVE`, `INVESTIGATE`, `SUBMIT`, or
`STOP_PARTIAL`. `STOP_PARTIAL` wins when another safe model round is unavailable because a configured
limit was reached, token reserve is insufficient, or repeated/no-progress thresholds were reached.
This ranking and transition logic never change a Relation verdict or proposal status.

`SubmitAnalysis` is policy-gated during Agent runs. It rejects submission before `SUBMIT`, rejects
non-empty paths outside the selected proposal-ready portfolio, and permits an empty proposal only
when bounded Frontier search is complete with no sufficiently grounded proposal-ready path. The
ordinary proposal expander and Validator still run after this gate and remain the final authority.

The Agent can inspect the reusable security fact graph through `ListSecurityFacts` as a fallback.
Fact categories
are `source`, `transform`, `execution`, `credential`, `guard` and `context`; optional filters cannot
change graph state. In reasoning mode `GetWorkflowFacts` intentionally omits deterministic
`findings` and `attack_paths` so those conclusions cannot leak into Agent reasoning.
The default Fact response is compact: short Relation reference, endpoints, type, category, verdict,
Evidence count and missing Evidence. Exact Relation/Evidence IDs require
`include_grounding=true`, which exists only for legacy full-proposal compatibility.

## Agent working memory

All seven reasoning Capabilities accept an optional `working_memory_patch_json` string in addition to
their existing arguments. The patch and selected action execute in the same Agent round. The patch
uses optimistic `expected_revision`, creates or updates at most three hypotheses, and may set one
focus hypothesis. New hypothesis IDs are generated by the runtime; updates must use the returned
ID.

Working memory contains at most eight hypotheses and 12,000 serialized characters. Status is one of
`ACTIVE`, `BLOCKED`, `READY_FOR_PROPOSAL`, or `DISCARDED`. Evidence and Relation IDs must already
exist. FALSE Relations cannot support a hypothesis; referenced Relation Evidence and UNKNOWN
missing-Evidence must be preserved. A memory patch cannot create Evidence, a Finding, an AttackPath
or a CloudIdentityCandidate, and `READY_FOR_PROPOSAL` has no effect on final status. Only
`SubmitAnalysis` plus the existing deterministic validator changes final conclusions.

Before every model round, the runtime reconciles working memory against the current canonical
Frontier graph. A hypothesis becomes `READY_FOR_PROPOSAL` only when a proposal-ready Frontier covers
its Relations and those Relations carry Evidence. Stale or FALSE support, a TRUE contradiction, a
hard-blocked execution path, a completed search with no viable covering Frontier, or duplicate
Relation grounding deterministically changes the hypothesis to `DISCARDED`. Reconciliation advances
working-memory revision once and is checkpointed; wording alone never bypasses duplicate detection.

The patch is excluded from repeated-action signatures. A semantic no-op does not increment memory
revision, while an accepted material update participates in progress detection. Free text is
redacted and bounded without altering grounding IDs. `checkpoint.json` and `analysis.json` persist
the memory. Only PARTIAL `summary.md` output shows active or blocked investigation state.
An upsert may use `supporting_relation_refs` and `contradicting_relation_refs`. The runtime resolves
them against the same run-local index and derives the exact Relation IDs, Evidence IDs and missing
Evidence before applying the unchanged working-memory validation rules.

## Compact analysis proposal

`ListSecurityFacts` assigns stable, run-local references such as `R0001` to the Relations exposed
to the Agent. `SubmitAnalysis` preferably accepts a compact proposal that names only the ordered
Relation references and user-facing Finding metadata:

```json
{
  "schema_version": "1.0",
  "path_drafts": [
    {
      "path_id": "path_example",
      "relation_refs": ["R0001", "R0004"]
    }
  ],
  "finding_drafts": [
    {
      "finding_id": "finding_example",
      "rule_id": "EXAMPLE",
      "title": "Example",
      "path_id": "path_example",
      "confidence": "MEDIUM",
      "summary": "Evidence-backed summary"
    }
  ]
}
```

The runtime rejects unknown, repeated, or discontinuous references before state mutation. It then
expands accepted references into exact Relation IDs, nodes, edges, Evidence IDs, missing Evidence,
and the maximum permitted status. The expanded ordinary `AnalysisProposal` still passes through
the existing deterministic validator. The legacy full proposal remains accepted for compatibility.
