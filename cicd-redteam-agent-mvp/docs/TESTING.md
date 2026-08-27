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

## Release checks

```powershell
uv sync
uv run python -m pytest -q
uv run ruff check .
uv run cicd-agent analyze tests/fixtures/oidc-risk --output <temporary-output>
uv run cicd-agent evaluate tests/fixtures/reasoning-matrix --output <temporary-output> `
  --provider codex-chatgpt --scenario gha-01
```

The ChatGPT-login smoke was executed against `tests/fixtures/global-chain` with `gpt-5.6-terra` and
completed through `proposal_validated`. A real LiteLLM provider call remains excluded until an
external provider credential and cost/usage ceiling are available.
