# Architecture

## Processing pipeline

```text
CLI
-> RepositoryBoundary
-> GitHub Actions parser
-> Evidence and local Relation extraction
-> attacker-control and identity assessment
-> reusable security fact graph
-> analysis mode split
   -> deterministic: scenario rules -> global AttackPath composition
   -> reasoning: hackingBuddyGPT Agent loop
      -> LiteLLM API-key adapter or Codex ChatGPT-login LLM adapter
      -> deterministic proposal validation
-> analysis.json, summary.md, trace.jsonl
```

The optional Agent never parses YAML or creates Evidence. Repository text and tool output are
untrusted data. Capabilities expose only bounded, redacted, read-only observations. Reasoning
analysis may also carry one immutable trusted operator Objective. The Objective steers what the
Agent investigates but cannot override the system policy, trust boundary, Evidence rules,
Capability limits or deterministic validator. A bounded deterministic compiler derives structured
source/mechanism/sink hints from the Objective. Those hints affect investigation priority only and
never create or alter Evidence, Relations, verdicts, guards or final statuses.

LiteLLM uses the explicitly resolved provider credential. `CodexChatGPTLLM` implements the same
hackingBuddyGPT `LLM.get_response` boundary using the official Python SDK and saved ChatGPT login.
It returns one capability decision per round; `CICDChatAgent` executes that capability, appends the
result to its existing history, and asks again until `submit_analysis` or a limit terminates the
same Agent loop.

Every reasoning Capability schema carries one optional `working_memory_patch_json` field. A
material hypothesis update is validated and applied before the selected Capability runs, then the
field is removed from the underlying action arguments. This preserves native Tool selection and
adds no model call or Agent round. The bounded memory stores at most eight concise, user-visible
hypotheses with Evidence/Relation grounding and a next check; it is not chain-of-thought, Evidence,
a Finding or an AttackPath. Both LiteLLM and Codex receive the same current-state projection. When an Objective exists, the
runtime re-injects it as a dedicated user-level instruction on every model round rather than storing
it in compactable history or concatenating it into the system prompt.

`ListSecurityFacts` assigns run-local short references such as `R0001`. The preferred compact
submission contains only ordered Relation references and user-facing Finding metadata. A
deterministic expander restores exact graph nodes, edges, Evidence, missing evidence and status
before the unchanged proposal Validator runs. The legacy full proposal remains accepted.
The same refs may ground working-memory hypotheses; their exact Relation/Evidence and missing
Evidence fields are restored before memory validation. Default Fact output omits those long IDs to
reduce repeated model input, while an explicit compatibility option can expose them. When a compiled
Objective Goal is focused, `ListSecurityFacts` stable-sorts matching Relations ahead of unrelated
ones while preserving every underlying Fact and Validator semantic unchanged.

The Agent runtime owns one bounded retry policy for both LLM backends; provider-internal retries are
disabled where configurable to prevent attempt multiplication. Canonical history and structured
state remain complete while a filtered model-input view removes superseded limits and old complete
rounds near the context ceiling. Each successful round atomically replaces `checkpoint.json`, while
bounded lifecycle metadata is appended to `events.jsonl`. A PARTIAL reasoning run can resume in the
same run directory from that committed boundary after repository/prompt/Objective/limit validation;
an incomplete round is rolled back and retried. The normalized Objective, its SHA-256 hash and the
compiled Goal are part of the safe-run configuration and cannot change during resume. Runtime counters and budgets remain cumulative across
attempts. See `docs/AGENT_RUNTIME.md`.

The Codex runtime starts in an empty temporary directory with read-only sandbox and denied
approvals. It disables inherited Codex plugins and every MCP server declared in the trusted user
configuration. Each model decision uses an ephemeral Codex thread. Instead of duplicating the full
hackingBuddyGPT conversation in Codex-owned memory, the adapter projects the trusted Agent policy,
optional user Objective, authoritative `AnalysisState`, current output identifiers, progress
counters and the seven most recent Agent events into a bounded decision context. The context marks
policy as controlling instructions, Objective as subordinate trusted operator intent, and state/tool
data as untrusted. Older facts must be fetched again through the
existing bounded Capabilities, which remain the only repository-access path.

## Global attack graph

Scenario rules produce local paths. The composer converts their supported facts into a partial
directed graph and joins only repository-backed edges. It does not enumerate hard-coded complete
attacks.

Sources are attacker profiles connected to repository-controlled execution or an untrusted GitHub
event. Sinks are write-capable `GITHUB_TOKEN` contexts and `CloudIdentityCandidate` objects. Exact
workflow, job, step, artifact, file and identity identifiers remain repository-specific.

An edge is `SUPPORTED` only when its Evidence exists locally. An uncertain control assessment or
external trust assumption keeps the composed path `CANDIDATE` and records `missing_evidence`.
`CONFIRMED` is forbidden because no workflow or exploit is executed.

## Security fact graph and LLM reasoning

The deterministic fact layer emits reusable `source`, `transform`, `execution`, `credential`,
`guard` and `context` relations. These relations describe observable repository facts; they do not
emit a vulnerability Finding. Examples include an untrusted event value flowing through an
environment variable or step output, repository code executing in a local action, and a step being
able to use a specific write-capable token permission.

Internally each relation retains a stable `relation_id`, Evidence, a tri-state verdict and any
missing evidence. `ListSecurityFacts` normally exposes a compact run-local ref plus the semantic
fields needed for reasoning. The Agent may compose only returned relations into a proposed path.
The runtime restores the stable IDs before the validator checks source/type/target and Evidence,
and rejects a `SUPPORTED` path containing an `UNKNOWN` or `FALSE` relation. GHA fixture IDs remain
evaluation labels rather than product rule identifiers.

The reusable extractor covers event inputs, job/step environments, step outputs, repository files,
local actions, reusable workflows, artifacts, caches, shell/import/network sinks, granular token
permissions, Secret flows, AWS/GCP/Azure identity access, mutable dependencies, runner persistence
and concrete guard application. A guard that locally negates a flow produces a `FALSE` execution
relation; uncertain external state remains `UNKNOWN` with an explicit missing-Evidence list.

The evaluation matrix lives only under `tests/fixtures/reasoning-matrix`. Its GHA labels and oracles
are loaded after analysis and are never visible to the parser, fact extractor, Agent capabilities or
prompt. A separate unlabeled blind fixture combines a fork PR, local action, artifact/cache,
privileged workflow and GitHub Token/OIDC sinks.

Deterministic and reasoning modes share parsing, Evidence and reusable fact extraction, then diverge
at conclusion generation. Deterministic mode keeps the legacy scenario rules and global composer for
fast/reproducible analysis. Reasoning mode does not prepopulate or expose deterministic Findings or
AttackPaths; the Agent derives them from the fact graph and the validator accepts or rejects the
proposal.

## Responsibility boundaries

- `application`: orchestration, termination, metadata and output assembly
- `domain`: pure models, parsing, relations, rules, graph composition and validation
- `framework`: provider auth, hackingBuddyGPT lifecycle, LiteLLM/Codex LLM adapters and Capability
  exposure
- `security`: repository boundary and redaction
- `tests`: contract, unit, integration, E2E and repository fixtures

The `domain` package must not import hackingBuddyGPT or the framework package.

## Scenario policy

Legacy deterministic mode retains its small regression rule set. New GHA expansion work must not add
scenario-ID dispatch or scenario-specific Finding rules: it adds only reusable facts, relations,
guards and Evidence. A hardening fact alone is not automatically an attack path. In particular, a
mutable external action ref requires a connected privileged context before it may become a
`CANDIDATE`; upstream compromise remains unverified.
