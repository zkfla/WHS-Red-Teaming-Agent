# Reasoning retry and time budgets

2026-10-14, IMPLEMENTING in original Windows checkout on dev. User requested more stagewise retry and time headroom, then authorized local bank Run duration 300→1800 seconds and review of teammate's latest push in parallel. Models: DeepSeek 4.1 High orchestrator, GPT-6 Luna Extra High workers.

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

Root configuration/env/conversation/request-ledger/profile checks: 54 PASS before final clock patch; full Ruff passed. Teammate origin/dev 294876f2 contains candidate feedback, native schema delivery, structured completion diagnostics and evidence/session improvements; read-only review is in progress. Local implementation is based on 6704bb0c until checkpoint and safe merge.
