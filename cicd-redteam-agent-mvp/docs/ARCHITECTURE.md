# Architecture

## Processing pipeline

```text
CLI
-> RepositoryBoundary
-> GitHub Actions parser
-> Evidence and local Relation extraction
-> attacker-control and identity assessment
-> reusable security fact graph
-> deterministic scenario rules
-> global AttackPath composition
-> optional hackingBuddyGPT Agent loop
   -> LiteLLM API-key adapter or Codex ChatGPT-login LLM adapter
-> deterministic proposal validation
-> analysis.json, summary.md, trace.jsonl
```

The optional Agent never parses YAML or creates Evidence. Repository text and tool output are
untrusted data. Capabilities expose only bounded, redacted, read-only observations.

LiteLLM uses the explicitly resolved provider credential. `CodexChatGPTLLM` implements the same
hackingBuddyGPT `LLM.get_response` boundary using the official Python SDK and saved ChatGPT login.
It returns one capability decision per round; `CICDChatAgent` executes that capability, appends the
result to its existing history, and asks again until `submit_analysis` or a limit terminates the
same Agent loop.

The Codex runtime starts in an empty temporary directory with read-only sandbox and denied
approvals. It disables inherited Codex plugins and every MCP server declared in the trusted user
configuration. It receives capability schemas and the redacted hackingBuddyGPT conversation, while
repository access remains exclusively in the existing bounded Capabilities.

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

`ListSecurityFacts` exposes these relations with Evidence, a tri-state verdict and any missing
evidence. The Agent may compose only returned relations into a proposed path. The validator rejects
invented relations, omitted relation Evidence and a `SUPPORTED` path containing an `UNKNOWN` or
`FALSE` relation. GHA fixture IDs remain evaluation labels rather than product rule identifiers.

Legacy deterministic scenario rules remain available for regression compatibility while new
inference scenarios are implemented on the reusable fact graph.

## Responsibility boundaries

- `application`: orchestration, termination, metadata and output assembly
- `domain`: pure models, parsing, relations, rules, graph composition and validation
- `framework`: provider auth, hackingBuddyGPT lifecycle, LiteLLM/Codex LLM adapters and Capability
  exposure
- `security`: repository boundary and redaction
- `tests`: contract, unit, integration, E2E and repository fixtures

The `domain` package must not import hackingBuddyGPT or the framework package.

## Scenario policy

Individual scenarios may emit local Findings and paths when their complete local condition is met.
A hardening fact alone is not automatically an attack path. In particular, a mutable external
action ref requires a privileged job context before it may become a `CANDIDATE`; upstream compromise
remains unverified.
