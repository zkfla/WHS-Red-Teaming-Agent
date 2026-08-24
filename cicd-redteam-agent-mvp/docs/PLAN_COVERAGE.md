# Seven-day plan coverage

## Implemented and locally verifiable

| Plan area | Implementation evidence |
|---|---|
| Day 1 contracts and vertical slice | pinned dependency, public models, CLI, contract tests |
| Day 2 parser | YAML 1.2, locations, permissions, secret/OIDC and malformed input tests |
| Day 3 Agent loop | five Capabilities and non-empty fake LLM Candidate submission |
| Day 4 control and references | fork-pr/repo-writer, bounded local tracing, cycle and UNKNOWN tests |
| Day 5 identity and proposal | AWS/OIDC candidate, evidence-only atomic proposal validation |
| Day 6 robustness | COMPLETE/PARTIAL/FAILED, exit codes, limits, model error, no-progress, redaction |
| Day 7 local integration | deterministic CLI, full pytest, Ruff, README and schema `1.0` |
| Auth extension | trusted `.env`, explicit provider resolution, Codex LLM adapter in the existing Agent loop |

The deterministic extension also composes local scenario paths into bounded attacker-to-credential
paths. It does not add Graph DB, network access or execution capability.

## Intentionally unverified

- real LiteLLM provider initialization and API authentication
- real ChatGPT-authenticated hackingBuddyGPT Agent run through the Codex LLM adapter
- real-model `proposal_validated` COMPLETE run
- provider token/cost accounting and external rate-limit behavior

These require an explicitly selected provider, credentials and cost approval. Fake LLM and stubbed
failure tests verify the local Agent contracts without making an API call.
