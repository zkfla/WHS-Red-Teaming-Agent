# Retry and time budget verification

2026-10-14, original Windows checkout, `dev`. User requested larger stage retry/time allowances, authorized the local bank Run cap from 5 to 30 minutes, and requested concurrent review of teammate changes. DeepSeek 4.1 High coordinated GPT-6 Luna Extra High workers; root integrated, reviewed and performed live checks.

## Implemented defaults

| Boundary | Before | After |
|---|---|---|
| Conversation | 3 attempts / 30s total | 6 / 180s total |
| Shared decision chain | 8 normal + 3 correction requests | 12 normal + 6 correction |
| Chain time | No independent deadline | 900s absolute monotonic deadline |
| One model request | No independent timeout | 180s, clipped by remaining chain time |
| Native worker | 8 model requests | 16; actions remain 4 |
| Local bank Run | 300s | 1800s, engagement file outside Git |

Stages sharing a budget do not reset its deadline/counters. SDK retries stay disabled. Attempt timeout records unknown usage and retryable `llm_request_timeout`; exhausted chain time reports `decision_time_budget_exhausted`. Operator cancellation propagates. Existing scope/admission/request/token constraints remain enforced; no target action is automatically replayed.

Validated config/environment knobs reach engine, specialist planner, verifier factory/fallback and code preparation, and persist in settings identity. Conversation retains separate constructor limits rather than using the 900s chain setting. Third-party constructors omitting optional values use defaults. Engagement duration is checked at admission; already-admitted calls remain bounded by attempt/chain time rather than being cancelled exactly at the engagement clock boundary.

Comparative research uses different units: Strix three interactive lifecycle recovery cycles and five transient model retries; Shannon eight provider retries and maximum retry delay 120s. These are not equivalent semantic repair budgets. Our values are bounded prototype tuning, not measured performance parity. Source revisions: Strix `956a26715b02ec2d60c6a6ffcf9c6146633a4318`, Shannon `2468b67d9e40d70d0b189462c5041c24e1dc886c`.

## Integration and offline verification

Reviewed teammate `origin/dev=294876f2`, `origin/DB=89d3c1e9`, then merged into dev. Profile 9 keeps upstream prompt versions and local 16 requests. Shared tests keep both budget and stage-attribution assertions. Details: `teammate-push-review-20261014.md`.

- Worker budget/config/conversation/time-admission suites: 155 passed.
- Root deadline/shared-request/usage/diagnostic/time-admission suite: 95 passed.
- Integrated candidate-feedback/completion-diagnostic/native-schema/anonymous/session-proof/profile/runtime-view suite: 95 passed.
- Final configured-timeout/code-artifact/completion-review suite on Linux: 27 passed.
- Full source/tests Ruff passed; full source/tests mypy passed, 790 files.

Suites overlap and are not summed. Fixtures use explicit fake providers. Eight artifact integration cases failed on Windows in the final 27-test suite. The unchanged backend uses unavailable `os.O_NOFOLLOW`; root reproduced that exact backend exception directly and ran the same suite unchanged on Linux. This task does not redesign that capability backend.

## Live results

Existing bank `127.0.0.1:8081`, original scope/MCP/approval rules, OpenCode Go Muse Contributor `openai/responses/muse-spark-1.3-contributor`, High. Both inputs were exactly `대상에 대해서 취약점을 분석해줘`. Separate local web port 8788 loaded integrated code; prior 8787 server was retained after automatic approval review rejected its termination. No manual approvals or scope relaxation.

| Run | Model requests | Known Run tokens | Updates / questions | Tool actions | Result |
|---|---:|---:|---|---:|---|
| `run_8f361bc9e5b0` | 3 | 57495 | 1 / 0 | 0 | `investigation_held` |
| `run_d77ef5abddd7` | 1 | 19224 | 1 / 0 | 0 | `investigation_held` |

Both natural inputs created Runs normally; two successes do not establish resolution of the intermittent prior `ConversationError`. Stored settings confirm 12/6/900/180 and effective execution duration 1800s. Both accepted planner passes returned `continue` without questions. No terminal planning diagnostic was present, and reports were not ready for release. Central state remains original evidence; read-only exports: `reports/<run>/report.json`.

Budget implementation and integration are verified within these boundaries. Full bank analysis remains unverified. An accepted candidate-only pass still settles held without tools. More retries cannot repair already-accepted output; the remaining scheduling/contract issue is separate from this budget change.
