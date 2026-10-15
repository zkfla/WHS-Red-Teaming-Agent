# Plan7 integrated implementation and verification

Checkout: original Windows `WHS-Red-Teaming-Agent`, branch `dev`. Implementation and verification are complete under the user's latest direct-provider decision. This report separates current implementation, scripted tests, physical execution and live-model results.

## Acceptance basis

The user selected the teammate's direct-program provider as the default and authorized adapting the plan. The original six source documents remain unchanged under `docs/plans/plan7-source-20261014/`. Packet gate, sidecar/NFQUEUE, byte/flow accounting and mandatory HTTP method/path enforcement are superseded for the direct default. Their absence is not reported as completed enforcement. The direct provider reports `network_enforcement=none`; operator scope/admission, approval, invocation identity, execution budgets, original evidence and truthful UNKNOWN remain required. Old gate code stays dormant in history/current modules as applicable.

The full teammate history through `38179561` was merged in `bcd08ed3`, including non-plan7 docs-site changes. Context-saving code was adopted, then one demonstrated missing-root defect was corrected in `c57da191`. No duplicate context implementation was introduced.

## Non-Kali requirements

| Requirement | Implementation and consuming boundary | Evidence |
|---|---|---|
| D01 ConversationError and input diagnostics | ConversationError retains DecisionDiagnostic; InputReceipt, CLI, WebFrontend/event/SSE and browser scalar projection preserve the safe effective cause. | Stage01 completed plan; current conversation/transport/completion-diagnostic run25 PASS; web projection included in30-test run. |
| D02 transport versus repair | Separate counters share the total request/deadline and metered ledger. Config/env/settings/consumer injection retain configured values. | `3f934309`, `c598f8f7`; current configuration/target/context run33 PASS. |
| D03 timeout/cancellation/privacy | Per-call budget survives outer timeout; cancellation propagates; raw provider messages/model text are excluded from safe diagnostics. | `a2bc7671`, `56e88126`; current conversation12 PASS after typed-fixture repair. |
| P01 executable continuation | Split, single-stage and legacy proposals require new work or a canonical viable existing-work witness; canonical commit repeats the check. | Fresh Sol merged-source audit CLEAR; progress/replan/atomic regression run included in30 PASS. |
| P02 finite repair/replan and termination | Durable bounded replan count, explicit held/exhausted/goal proposal and runtime settlement/report. Goal certification still uses complete_goal. | `da27d0d2`, `88701c9e`; reopened exhaustion/new wake/operator precedence regressions. |
| P03 no duplicate progress | Registration, necessity adjustments, judgment/cursor/progress/wake share a transaction; external drift rejects the batch. | `be9b95df`, actual legacy atomic regressions; intermediate follow-up targets2 PASS after valid held/resume fixtures. |
| K01 contradiction | Structured same-candidate confirmed/refuted AND conflict checked at producer and canonical registration; exact references survive feedback. | `47745022`, contradiction/admission tests and Sol source review. |
| K02 observation/review/verdict | Original criterion IDs restored; observations, independent review receipt, candidate revision and verdict remain separate. | `a87c2225`, observation-chain and binding/resume regressions; source audit CLEAR. |
| K03 reuse and final CAS | Original purpose/identity remain immutable; consuming adoption is independently reviewed and source/validity/graph proof is rechecked in final transaction. | `21bcf63d`; root merged reuse19 PASS and final source-reuse checks in30 PASS. |
| K04 actionable rejection | Typed owner/criterion/source/recovery information survives native feedback, storage, Planner/report and reopen. | `0b7de4e2`, `88701c9e`, completion-diagnostic delivery and source review. |

An additional concrete gap discovered during regression diagnosis was repaired in `ec3aaeb4`: legacy StrategyDraft rules were hidden behind generic invalid-proposal feedback. Six unchanged validator rules now retain static rule codes and exact field locations. Seven new tests exercise actual repair feedback and exhausted diagnostics without leaking model text; root7 PASS, worker14 PASS with neighboring rejection tests, scoped mypy and independent Sol review CLEAR.

## Original numbered requirements after the direct-default decision

| Source items | Current implementation/evidence | Boundary |
|---|---|---|
| 7a.1–2 | Existing web form/selection snapshot, request-local startup environment, closed/inherit/configured/direct preparation and CLI build wiring. Real startup drift and read-only recorded validation regressions8 PASS; Linux startup/preparation neighbors36 PASS. | No global environment/context mutation or second web executor. |
| 7a.3–5 | Direct config, selected Docker endpoint/bridge, pinned Kali image and matching preparation manifest. | Sidecar/NFQUEUE/nft quotas superseded; no packet enforcement claim. |
| 7a.6–7 | Structured preparation errors/readiness, immutable environment fingerprint and canonical component binding. | Readiness/registration does not certify an actual target call. |
| 7b.1–5 | Local shell remains network-none; direct target provider validates declared names/ports, approved target, explicit route and deny scope; preparation binds actual Docker endpoint/route identity. | No implicit loopback replacement, tunnel or target permission expansion. |
| 7b.6 | Shared invocation/time/output accounting remains; direct route attaches only for execution. | Packet/byte/flow window ledger is superseded for this default. |
| 7b.7 | Workroom capabilities unchanged; physical test asserts no NET_ADMIN/NET_RAW. | A successful program does not prove its stdout claims. |
| 7b.8 | Same-instance exclusion, timeout/cancellation cleanup, output preservation with uncertainty on failed detach, original execution identity. | Direct synchronous provider does not claim the abandoned sidecar/job window protocol. |
| 7c.1–4 | Existing role selector/catalog, direct readiness, manifest-backed program list/describe paging and bounded native input. | No fixed program allowlist, mandatory Kali action sequence or forced tool choice. |
| 7c.5–7 | Real proposal/Control/gateway/protected output; original invocation and evidence reach Task, next worker and Planner. | Scripted model in physical proof; output masking/artifact access retained. |
| 7c.8–10 | Existing shell accounting/graph contribution and independent observation/review/verdict; candidate/mission success remains separately gated. | Arbitrary stdout is not trusted HTTP/session evidence or a Finding. |
| 7d.1–9 | Covered by K01–K04 producer/canonical/CAS/review/criterion evidence above; historical Tasks are not rewritten and original condition-ID restoration is retained. | No purpose hash deletion or automatic receipt acceptance. |
| 7e proof | Real production assembly and Control, owned pinned-Kali/mock route, approval waiting, one original invocation, physical receipt, protected output, follow-up worker and Planner. | No live-model autonomous Kali choice or verified vulnerability is inferred. |
| 7e display/compatibility.1–4 | Existing readiness/report views, actual execution/provider metadata accounting, unknown usage, bound resume and changed configuration rejection. | Previous records are preserved; no parallel UI state database. |

## Physical evidence

`test_direct_control_chain_docker.py` passed on the original checkout with real Docker/Kali: zero mock accepts before approval; fixture operator resolves the actual Control approval; the same invocation succeeds once; one ACCEPT/12 bytes is observed; TOOL evidence and inconclusive Task result preserve the original; the next worker request and actual Planner payload contain its reference/status/stdout; the workroom has no attached networks after execution. Root final run:1 PASS in29.36seconds. Independent Sol review CLEAR.

`test_direct_target_route_docker.py` passed with actual allowed communication, gateway schema port refusal (`shell_invalid_arguments`), scope refusal (`direct_target_scope_denied`), internal program timeout output and detach, and cancellation after observing attachment followed by detach. Root final run:1 PASS in53.21seconds. Cancellation does not prove that the child had started before the cancellation point. Owned resource inventories were empty after both proofs. No historical unknown/quarantine record or user container was removed.

Failed detach is separately tested with the real manager's cache-loss path: `_Attached` retains the original room; detach failure preserves output/uncertainty and keeps the holder busy. Eight direct unit tests pass. No new recovery store or automatic retry was added.

## Actual Cat Jam High bank runs

Provider `opencode-go`, model `openai/cat-jam-free`, effort `high`, chat-completions protocol. Original objective: “대상에 대해서 취약점을 분석해줘”. Temporary engagement copies and the existing local bank were used; saved settings, original engagement and API secrets were preserved.

| Run | Model calls | Actual tools | Observed stop |
|---|---:|---:|---|
| run_9b1ceb99411c | 7 | 0 | planning_error: model failed to produce a valid progress/termination judgment within repair budget. |
| run_86804aeb64c6 | 10 | 2 | Two actual GET probes, two evidence records/graph mutations, then high-risk approval pending. |

These are real-model/real-bank observations. They prove that the old tools0 path is not inevitable and that approval is respected; they do not prove whole analysis completion or guarantee model contract compliance. No approval was fabricated to continue a live action.

## Review, checks and remaining gates

Primary independent review: GPT-6.1 Sol High, source-backed findings and scoped repairs. Auxiliary review: agy Gemini3.8FlashHigh, job `1792035226578649100-f6eb1b2d55f16c1ffc26b3d9`. Accepted cache-loss/context defects are repaired; invalid-state-only claims and the alleged closed-Run attachment bypass were rejected with canonical binding evidence.

Full CI-path Ruff and full Linux src/tests mypy pass after physical-test repairs. Both compose contracts pass. Specific regression closures include32 CLI/resume/collect/process tests,38 recovery/environment/preparation tests,16 historical migration tests,9 CLI/PTY tests, and2 process contention tests. Historical migration source identity reconstruction is restricted to its explicit legacy adapter; candidate policy and substantive drift still fail closed.

The earlier full run was4477 PASS/132 SKIP/25 FAIL and is not reported as green. The final full integrated run completed4510 PASS/133 SKIP/2 FAIL (JUnit `/tmp/whs-final-integrated-20261015.xml`). The two failures were investigated: a live owned workroom during the cap test legitimately made both processes refuse the global cap; a controlled owned-room reproduction recorded both exact shell_too_many_workrooms errors. The cap assertion is unchanged, diagnostic streams are now preserved, and root isolated verification passed. The stop fixture now supplies a valid held/resume judgment and separates its startup allowance from its unchanged2second stop deadline. Root reran both formerly failing cases together:2 PASS in14.91seconds; the corrected stop case also passed three precise repetitions. This targeted reconciliation is not represented as a newly green full-suite run. Newly changed legacy diagnostics and physical opt-in tests have separate targeted evidence. Final documents are complete. A fresh fetch confirmed no teammate commits beyond38179561, all already integrated, and the normal dev push38179561→2b786a79 succeeded. This closure-document update is published in the subsequent normal push. Unrelated user files and `.gitignore` remain outside commits.
