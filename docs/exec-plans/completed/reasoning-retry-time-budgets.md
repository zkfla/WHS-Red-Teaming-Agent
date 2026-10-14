# Reasoning retry and time budgets

2026-10-14, COMPLETED in original Windows checkout on dev. User requested more stagewise retry and time headroom, then authorized local bank Run duration 300→1800 seconds and review of teammate's latest push in parallel. Models: DeepSeek 4.1 High orchestrator, GPT-6 Luna Extra High workers.

## Scope and ownership

Root owns core configuration/environment bindings, default native-probes model allowance, integration/remote merge, documents, commits/push and live verification. Workers own operative settings/conversation defaults and request-local DecisionBudget deadline/per-call timeout enforcement. Teammate-push reviewer is read-only. Preserve user .gitignore, untracked files and deployment/secret files; the local engagement duration change is explicitly authorized and stays out of Git.

## Accepted adjustment

- Conversation: 3 total attempts / 30s total → 6 / 180s total.
- Shared planner/atomizer/native reasoning chain: 8 normal / 3 correction → 12 normal / 6 correction. Failures of one stage do not create an independent retry pool. SDK retries remain disabled; provider/format/semantic retryable failures remain charged in the single existing correction pool.
- Native worker default: 8→16 total model requests; actions remain 4.
- One shared chain: 900s absolute monotonic deadline; one request including admission wait: 180s maximum, clipped by remaining chain time. Existing ROE/Run/request/token checks continue to apply and no tool call is auto-replayed.
- Expose normal/correction/time values through validated OrchestrationConfig, common environment input and runtime settings identity. Existing explicit overrides remain valid.

External numbers use different units: Strix has 3 interactive lifecycle recovery cycles and 5 transient model retries; Shannon has provider SDK retry maximum 8 with maximum retry delay 120s. They are comparative evidence, not identical contracts or measured optimal values. The chosen defaults are a prototype tuning decision with bounded accounting, not a claim of equivalent performance.

## Validation and completion

Verify configured values reach consumers and saved fingerprints; later correction succeeds; exhaustion remains bounded; chain deadline does not reset between stages; in-flight requests cancel on attempt/chain timeout without fabricated usage; operator cancellation propagates; no new SDK retries; explicit old settings still govern existing saved runs. Test new default and frozen historical profile boundaries. Review teammate diff before merge, preserve its new feedback/evidence contracts and re-run relevant checks after integration.

Live testing uses existing local bank/Muse High settings and user-authorized 30-minute Run cap. Missing questions with continue remain a separate workflow issue: more retry budget cannot repair an output that is accepted without retry. Report actual tool actions/termination, not model prose as success.

## Current evidence

Root configuration/env/conversation/request-ledger/profile checks: 54 PASS before final clock patch. Teammate origin/dev 294876f2 was reviewed and merged with source improvements preserved; profile is version 9 with 16 requests. Worker suites 155 PASS; root budget suite 95 PASS; integrated remote regression suite 95 PASS; final consumer/code-artifact suite Linux 27 PASS. Counts overlap. Full Ruff and full mypy (790 files) pass.

## Final Result and Requirement Status

Retry defaults, shared deadlines, attempt cancellation and configured-value propagation are implemented and checked. The local bank Run duration is 1800s. Two identical natural-language live requests created Runs, with saved settings 12/6/900/180. Both returned investigation_held with one update, zero questions and zero tool actions; bank analysis success is not claimed. More retry budget cannot repair accepted-but-inert planner output.

## Important Decisions and Independent Review

Root retained the single existing correction pool and disabled SDK retries; no target action replay, scope expansion or automatic approval. Separate Luna source review found no concrete remote blocker and identified profile/test integration requirements. Root review additionally found configured timeout values missing at verifier/code-preparation consumers; existing factories now pass those values. A float/None deadline annotation error found by full mypy was corrected.

## Validation and Remaining Risks

Detailed limits, tests, two live Runs and limitations are in `docs/agent/reasoning-budget-verification-20261014.md`; remote review is in `docs/agent/teammate-push-review-20261014.md`. Windows artifact integration failures were reproduced in the unchanged backend (os.O_NOFOLLOW absent); the same suite passes unchanged on Linux. The update-only/no-question issue and intermittent ConversationError remain unproven as resolved. Conversation uses its separate total timeout and constructor attempt count. Existing web8787 remains; integrated-code live verification used8788.

## Git Summary

Stage commits: ca7f774b defaults/configuration; 112794cd deadline enforcement/review; 3e100233 remote merge. Final timeout-consumer wiring and verification documents are committed separately. User .gitignore, engagement/config/secrets and unrelated files remain outside the staged task changes. Push uses normal dev history without force.
