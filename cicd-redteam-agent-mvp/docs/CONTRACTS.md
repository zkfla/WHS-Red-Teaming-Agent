# Public contracts

## CLI

```text
cicd-agent analyze <repo-path> --output <dir>
  [--model <litellm-model>]
  [--provider auto|openai|anthropic|openrouter|gemini|litellm|codex-chatgpt]
  [--env-file <trusted-env-path>]
  [--attacker-profile fork-pr|repo-writer]
  [--max-rounds N] [--max-tokens N] [--max-cost N] [--max-duration N]
```

Exit codes are `0` COMPLETE, `2` input/configuration error, `3` framework/model initialization
error, `4` PARTIAL and `5` internal validation failure.

## Output

Each run writes `<output>/<run-id>/analysis.json`, `summary.md` and `trace.jsonl`. All public models
carry `schema_version="1.0"`. Run metadata records the repository commit, attacker profile, configured
limits, framework commit and optional provider/model identifier.

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
- Edges must connect consecutive ordered nodes, match an extracted Relation and carry that
  Relation's Evidence.
- `SUPPORTED` paths cannot contain `UNKNOWN` or `FALSE` relations or non-empty missing evidence.
- Candidate paths must preserve the missing evidence declared by uncertain relations.
- Static paths cannot be `CONFIRMED`.
- Duplicate IDs are rejected, including collisions with existing state.
- A rejected proposal does not mutate state.
- A successful `submit_analysis` may occur only once.

## Termination

- `deterministic_analysis_complete`: parser and deterministic domain completed without a model.
- `proposal_validated`: Agent submitted one valid proposal.
- `model_error`: a model round failed after deterministic results existed.
- `no_progress`: four repetitions of the same call or four rounds without a new call signature or
  newly inspected resource.
- framework limit reason or `limit_reached`: round, token, cost or duration budget ended.

All non-successful Agent termination reasons produce `PARTIAL` and preserve deterministic state.

## Safety

Repository paths must remain below the resolved input root. `.git`, parent and absolute paths,
symlink escape, binary input and files above 1 MiB are rejected. File slices are at most 200 lines;
literal search returns at most 50 redacted results and excludes dependency/build directories.
Codex receives no direct repository path or repository tool. In an isolated temporary directory
with read-only sandbox, approval denial, plugins disabled and configured MCP servers disabled, it
selects one registered Capability per round. Only hackingBuddyGPT executes the Capability, and
`submit_analysis` uses the existing deterministic proposal validator.

The Agent can inspect the reusable security fact graph through `ListSecurityFacts`. Fact categories
are `source`, `transform`, `execution`, `credential`, `guard` and `context`; optional filters cannot
change graph state.
