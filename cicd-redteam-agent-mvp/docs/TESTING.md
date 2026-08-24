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

## Agent and failure coverage

- fake LLM performs list, inspect and a non-empty valid submit
- malformed proposal returns a structured error without mutation
- duplicate submit is rejected
- model/rate-limit exception produces `model_error`
- repeated calls and no-progress terminate deterministically
- round/token/cost/duration limit reason produces PARTIAL
- deterministic Evidence, Finding and identity survive all PARTIAL exits
- trusted `.env` loading and process-environment precedence
- multiple provider keys require an explicit choice and model/provider mismatch is rejected
- analyzed-repository `.env` cannot become an authentication source
- Codex SDK contract surface and shared hackingBuddyGPT adapter references are checked statically

## Release checks

```powershell
uv sync
uv run python -m pytest -q
uv run ruff check .
uv run cicd-agent analyze tests/fixtures/oidc-risk --output <temporary-output>
```

Actual LiteLLM/Codex model execution is intentionally excluded until a provider, credentials or
ChatGPT login, and cost/usage ceiling are explicitly approved.
