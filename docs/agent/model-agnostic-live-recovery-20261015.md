# Model-independent recovery: evidence and ongoing verification

Checkout: original WHS-Red-Teaming-Agent Windows repository, `dev`. This record continues plan7; it does not claim complete bank analysis or guaranteed compliance by every model.

## Live evidence before the frozen repair

Provider `opencode-go`, model `openai/cat-jam-free`, effort `high`, existing local bank, temporary engagement copies. Four planning failures each used one normal request and six corrections, then stopped with `decision_repair_budget_exhausted` in `hypothesis_planning`; no provider 429 was observed. The dominant rejection was `continue_requires_proposed_or_existing_work`: continue without proposed or referenced executable work. Other recorded failures included invalid JSON, unregistered question subjects, and invalid completion conditions. These are distinct from transport failure and approval waiting.

| Run | Actual tool executions | Outcome |
|---|---:|---|
| `run_9b1ceb99411c` | 0 | Planning correction budget exhausted. |
| `run_8e91be72b471` | 0 | Planning correction budget exhausted. |
| `run_012299415a19` | 0 | Planning correction budget exhausted. |
| `run_3758c90fa6fe` | 3 | Two high-risk actions awaited approval. |
| `run_a50add666337` | 0 | Planning correction budget exhausted; source was changing, so this is not a frozen post-fix comparison. |

The user subsequently authorized resolving all approvals for the existing bank/Docker sandbox tests through the actual approval mechanism. Approvals `appr_8d291c64b59a` and `appr_30b8e2ac7a1a` were resolved against their original actions. Resuming `run_3758c90fa6fe` produced no new execution and ended `budget_exhausted`. Its original start preceded resume by about 70 minutes, exceeding the 1800-second Run duration. The duration diagnosis follows timestamps and `reasoning_time_failure`; the stored final reason does not preserve that finer cause. The coincidental twelve model requests do not demonstrate exhaustion of `--max-steps 12`, which is a separate orchestration-turn setting.

## Repair and review

Commit `7e36989d` connects split-planner semantic rejection to code-owned, rule-specific correction guidance already available to the single-stage path. Five rules now identify the contract field and correction, including empty held/exhausted judgments, missing executable work, blank judgment text, incompatible termination, and invalid completion criteria. Unknown rules retain the existing fallback. No work, reference, evidence, approval, or completion is invented. The correction and transport budgets remain separate and bounded.

Independent review caught truncation after adding structured rule/location prefixes. The continue guidance was shortened and a regression now verifies the complete rendered feedback, including the completion-gate qualification, fits the existing limit. A combined reason/resume rejection points to the judgment root rather than falsely identifying only the reason field.

Root verification: four new tests passed; 61 neighboring planner/atomization/diagnostic tests passed; scoped source mypy and Ruff passed. The scripted provider tests demonstrate a rejected plan receiving specific correction and a valid subsequent plan reaching atomization with three total requests. They are not live-model evidence. Initial criterion kinds are already declared by the planner's local schema hint; the shared storage contract remains permissive for historical values. The unknown-completion code also includes blank descriptions, so historical records do not establish that an unknown kind caused it.

Frozen post-fix live verification is pending: fresh Run, same model and bank, 30-minute duration, normal configured step allowance, automatic handling of each in-scope approval through the existing service.

## Retry/time comparison

Read-only source audit checked the exact private repository [aoistar-kr/ARTEX_ko, original_cn](https://github.com/aoistar-kr/ARTEX_ko/tree/original_cn), reported ref `d0033724a63aefbbcc1ed022b0faf4dc4a1fce51`. Earlier comparisons against mhtsec/ARTEX or artex-global were incorrectly mapped and are excluded. ARTEX uses norma `v0.4.3`; SDK defaults below belong to that dependency, while stream/task settings belong to ARTEX.

| System and unit | Retry/iteration defaults | Time defaults |
|---|---|---|
| ARTEX `original_cn` | SDK connection 3; empty response 2; same-provider stream 2; task model-error retry 2; empty-turn nudges 2; max turns 0 means unlimited. | SDK backoff starts 0.5s, capped 8s; stream capped 4s; task retry delay 3s. Worker DB fallback 600s; absent DB/config path 1200s. Request timeout not established by this audit. |
| [Strix](https://github.com/usestrix/strix/tree/ca20626f) | Model retries 5; interactive recovery 3; max turns 500; noninteractive recovery derives from max turns. | Request timeout 300s; exponential delay starts 2s and caps 90s. |
| [Shannon](https://github.com/KeygraphHQ/shannon/tree/2468b67d) | Provider retries 8; agent retries disabled at that layer; Temporal activity retries 50. | Provider retry delay cap 120s; Temporal initial interval 5min, cap 30min, activity timeout 2h, heartbeat 60min. |
| Current project | Shared decision chain: 12 normal, 6 corrections, separate transport retry allowance 5. Planner and atomizer share it. Native profile worker requests 16. | Decision chain 900s; request 180s clipped by chain remainder; local bank Run 1800s. |

ARTEX locations: `db/llmretry.go`, `server/llmretry.go`, `server/task_llm.go`, `server/engine.go`, `llmpool/health.go`, `llmpool/pool.go`, `db/config.go`, `server/server.go`; norma locations: `llm/provider.go`, `llm/retry.go`, `llm/openai.go`. Strix locations: `strix/config/models.py`, `strix/config/settings.py`, `strix/core/execution.py`. Shannon locations: `apps/worker/src/ai/pi/retry-settings.ts`, `apps/worker/src/temporal/workflows.ts`.

These values count different operations. Temporal activity retry is not semantic plan repair; multiplying all layers would misstate effective allowance. The audit does not justify a blanket increase. Current new planner/atomizer requests already share the configured decision/time budget. Profile `max_activations` and `max_strategy_revisions` have no production consumers beyond their definitions; changing them would not increase execution. `OrchestrationSettings.max_steps` defaults to 8 without config while configured production uses 50; production injection must be distinguished from direct-constructor defaults. Replan passes 3 and worker instances 2 are real limits, but no collected failure establishes either as the current bottleneck.
