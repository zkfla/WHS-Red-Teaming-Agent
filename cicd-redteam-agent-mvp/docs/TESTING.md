# Test specification

## Required deterministic coverage

- workflow-free repository and ordinary CI
- YAML 1.2 `on`, malformed YAML and job permission override
- fork PR and `pull_request_target` checkout provenance
- local composite action, reusable workflow, static script and reference cycle
- dynamic expression path preserved as UNKNOWN
- Secret/GITHUB_TOKEN reference and redaction
- OIDC plus AWS role with unknown IAM trust
- prompt injection text treated as data
- path traversal, symlink escape, binary, oversized file, slice and search limits

## Scenario coverage

- PR expression shell injection
- PR artifact consumed and executed by privileged `workflow_run`
- mutable external action combined with privileged context
- broad `GITHUB_TOKEN` permissions
- secure counterparts must not produce their scenario Finding
- composed path must reach a credential or cloud identity only through validated edges
- inference-flow fixtures cover SUPPORTED, CANDIDATE/UNKNOWN and secure guard variants
- env and step-output value flow reaches a command interpreter and granular token permission
- a blind local-action fixture is represented through generic source, execution and credential facts
- proposed paths with unknown `relation_id`, mismatched relation shape, or unresolved UNKNOWN facts are rejected
- GHA-01 through GHA-10 reasoning labels each have supported/exposed, candidate/unknown and rejected/secure repositories
- every matrix relation carries existing Evidence and every UNKNOWN relation names missing Evidence
- matrix graph composition checks the expected SUPPORTED/CANDIDATE/REJECTED outcome without product-side GHA dispatch
- the unlabeled blind fixture joins fork PR -> local action -> artifact/cache -> privileged workflow -> GitHub Token/OIDC
- FALSE guard edges are accepted only in REJECTED paths; path-level Evidence must cover every edge

## Agent and failure coverage

- fake LLM performs list, inspect and a non-empty valid submit
- malformed proposal returns a structured error without mutation
- duplicate submit is rejected
- model/rate-limit exception produces `model_error`
- repeated calls and no-progress terminate deterministically
- round/token/cost/duration limit reason produces PARTIAL
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
- compact Fact output omits long grounding IDs unless compatibility mode is requested
- working-memory Relation refs restore exact Evidence and missing Evidence; unknown refs do not mutate state
- compact drafts derive CANDIDATE from UNKNOWN and REJECTED from FALSE without accepting a model status override
- checkpoint replacement is atomic and restores AnalysisState, working memory, limits and runtime metrics
- event journal sequence is append-only; only a malformed final line is repairable
- failed rounds roll back state, submission state and partial Agent history before checkpointing
- resume reuses the same run directory and attempt lineage while preserving cumulative budgets
- completed runs, Objective hash or compiled-Goal mismatch, commit drift, dirty repositories,
  stale/corrupt journals and concurrent leases are rejected

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
