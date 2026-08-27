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
only for reasoning analysis. `--objective-file -` explicitly consumes stdin; stdin is never
auto-detected. Objective files inside the analyzed repository are rejected so repository data cannot
be promoted into trusted operator instructions. A deterministic compiler derives bounded
source/mechanism/sink Goal hints from the normalized Objective. These hints affect investigation
ordering only; they cannot create Evidence, Relations, verdicts, guards or final findings.

## Output

Each run writes `<output>/<run-id>/analysis.json`, `summary.md` and `trace.jsonl`. Reasoning runs
also write atomic `checkpoint.json` schema 2.0 and append-only `events.jsonl` schema 1.0. All public models
carry `schema_version="1.0"`. Run metadata records the repository commit, attacker profile, resolved
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
runtime metrics, working memory and the immutable user Objective. Optional limits are new cumulative
maxima and must exceed the already consumed value. Repository commit drift, dirty Git state, an
Objective hash or compiled-Goal mismatch, an active run lease, an older or corrupt event journal,
and completed runs are input errors. Changing the Objective requires a new analysis run. A failed/incomplete round never becomes
the resumed state; the last committed boundary is retried.

`auto` accepts exactly one configured provider key. More than one key requires explicit
`--provider`; provider/model prefix conflicts are configuration errors. Process variables take
precedence over `.env`. Automatic `.env` discovery never loads a file inside the analyzed
repository. `codex-chatgpt` requires saved ChatGPT authentication and does not consume an API key.
It supplies the LLM decisions inside the same hackingBuddyGPT round loop. Observed token usage is
registered with `Limits` after each turn; monetary `max-cost` is not enforceable for this provider
because the SDK does not report per-run currency cost.

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

The Agent can inspect the reusable security fact graph through `ListSecurityFacts`. Fact categories
are `source`, `transform`, `execution`, `credential`, `guard` and `context`; optional filters cannot
change graph state. In reasoning mode `GetWorkflowFacts` intentionally omits deterministic
`findings` and `attack_paths` so those conclusions cannot leak into Agent reasoning.
The default Fact response is compact: short Relation reference, endpoints, type, category, verdict,
Evidence count and missing Evidence. Exact Relation/Evidence IDs require
`include_grounding=true`, which exists only for legacy full-proposal compatibility.

## Agent working memory

All six reasoning Capabilities accept an optional `working_memory_patch_json` string in addition to
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
