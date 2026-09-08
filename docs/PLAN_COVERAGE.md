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
| Real-model smoke | ChatGPT-authenticated Codex adapter, four Capability rounds, valid submit and COMPLETE |

The deterministic extension also composes local scenario paths into bounded attacker-to-credential
paths. It does not add Graph DB, network access or execution capability.

## Real-model verification

The ChatGPT-authenticated smoke against `tests/fixtures/global-chain` completed with
`proposal_validated`. It used `gpt-5.6-terra`, four rounds and 94,677 reported tokens, and wrote
`analysis.json`, `summary.md` and `trace.jsonl`. The trace contains only the registered
hackingBuddyGPT Capabilities. Reported monetary cost is `0.0` because the Codex SDK does not expose
per-run currency cost; it must not be interpreted as proof that usage is free.

## Intentionally unverified

- real LiteLLM provider initialization and API authentication
- LiteLLM provider token/cost accounting and external rate-limit behavior
- currency-denominated Codex usage cost

These require an external provider credential or billing data that was not available. Fake LLM and
stubbed failure tests continue to verify the local Agent contracts without network usage.
