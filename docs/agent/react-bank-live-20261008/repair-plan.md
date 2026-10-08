# Bank live repair — 2026-10-08

## Required outcome

Run the real model against the existing local synthetic bank through registered recon and Web tools. Preserve originals, accept evidence-supported model graph recording, and settle the task and mission as achieved or canonically unresolved. Verify the stored graph, settlement receipts, and JSON/Markdown report. Tool invocation or a stalled run alone does not satisfy this outcome.

## Confirmed failure

`run_c662c7c43968` completed five remote calls. The comparison original `ev_db58f3ee285a` retained two actual session receipts and their account principals. Its execution representative was the session alias `alice`. Three accepted observation reviews named `test-alice` and `test-bob`, but the completion consumer rejected the representative alias before checking the actual account bindings. Its later coverage calculation also started from that single representative. No model `graph.record` or `goal.settle` call occurred, and the task remained running without a canonical unresolved record.

## Changes and ownership

1. DeepSeek 4.1 Flash Max: repair completion subject coverage using preserved per-response actual-use receipts and existing same-run/target account grounding. Preserve the execution representative. Reuse account-proof rules across affected consumers; do not add tool-name branches, new state, or new model validation duties.
2. Muse 1.3 Extra High: repair the data-only live harness. Keep the original objective; distinguish accepted graph writes from completion reviews, and successful task/goal settlement from attempted calls. Accept the canonical unresolved branch using its actual receipts and stored states, while rejecting a stalled run without settlement.
3. Opus 4.8 High: independent plan and code review. agy Gemini 3.8 Flash High: supplementary review. Root resolves feedback and coordinates ownership. Muse handles further refactoring only when justified by concrete duplication or responsibility problems.

## Plan-review decisions

Two independent Opus reviews approve the direction subject to covering both the early scope check and the later verified-principal coverage. Adopt both requirements. Actual-use proof, anonymous restrictions, origin/run linkage, and rejection of invented accounts must remain intact. An empty coverage list on an unknown review receipt is not itself a defect; retain each consumer's meaning instead of forcing a claim into every receipt.

The harness review identifies four required repairs: canonical unresolved acceptance, actual graph-record results, successful task and goal results, and sufficient event/receipt identifiers. Adopt these data-only changes. A new product requirement to call a specific tool in a fixed order is out of scope.

Gemini 3.8 Flash High supplementary review confirms the two coverage sites and the need for actual-use grounding. Adopt those points already in scope. Its proposed single-session filter failure is not demonstrated by this run: the preserved arguments contain the two comparison session fields and no single `session_ref`. Reject it as an additional mandatory repair without a reproducer; retain consistency when reusing the existing collector.

## Verification and boundaries

Review implementation before execution checks. Use focused regressions for both real subjects, aliases backed by actual receipts, invented accounts, missing/conflicting receipts, anonymous results, and other-run evidence; preserve affected legacy behavior. Run repository-required checks appropriate to the final delta. Then delegate the bounded real-model bank run and wait for its report without intervening in its execution.

Preserve the existing bank container, user files, other contributors' changes, and `.env`. Do not store credentials, full model responses, or private reasoning. Do not claim a finding from status/digest equality alone. Do not commit or push this repair without an applicable user request.

## Status

Principal repair and harness repair implemented and reviewed. The principal code review found an unconnected required-account argument and an inconsistent alias fallback for two requested sessions; both were repaired and the reviewer confirmed closure. Implementer reports 140 focused and 25 integration checks passed. Windows-wide checks have platform-related failures; Linux verification remains pending.

Post-repair live runs `run_e8dee40bbe53`, `run_4caacd460318`, and `run_bdfa6402d204` performed real recon/Web investigation and recorded provisional hypotheses/branches, but none called task or goal settlement. All ended `stalled_no_graph_progress`. These runs do not prove canonical completion or unresolved settlement. Stop unchanged live retries and investigate the progress/termination contract before another execution. DeepSeek Max and an independent Opus High advisor are tracing this new failure; no further product edit is approved by this record yet.

Linux verification of the principal repair is complete: Python 3.10.22 and 3.14.8 each ran 3033 passing tests with 51 existing skips, Ruff and mypy passed, and the CI-equivalent container checks exited zero. The manifest matches the current executable source; only this subsequently updated document differs. Evidence is under `data/react-bank-live-20261008/linux-verification/`.

The next repair plan is under independent Opus and supplementary Gemini review. The current-turn progress timing defect is supported by the run reconstruction. Whether grounded provisional hypotheses should contribute a text-independent progress key needs review against the existing repetition protections. Do not raise limits, require a fixed tool sequence, or claim canonical unresolved from a stalled report.

Review decision for the second repair: implement fresh canonical-change checking at the stall boundary while keeping observe-side counting, hard limits, and stop priorities. Reuse the existing public progress owner; do not import a private runner helper into graph nodes. Opus approves this direction conditionally. Hold the broader open-hypothesis fingerprint change: output-digest keys avoid invocation/text churn but can still vary through source subset recombination. Conflicting reviewer reconstructions of the last turn must be checked against stored timestamps before claiming that the timing repair covers every live failure. Canonical settlement remains unproven until a subsequent real run succeeds.

Fresh-change repair passed independent code review and 152 focused checks. Timestamp rechecking corrects the earlier assertion that all three runs ended with posture: only `run_bdfa6402d204` did; the other two ended with local recording activity.

One subsequent frozen-source live run, `run_3609b5af4824`, made nine remote calls and ten model requests. Two model graph-record attempts returned only `bad recording input`; neither created a graph record, and no settlement was attempted. This is not completion. The next bounded repair investigates and reuses existing safe validation diagnostics so the model receives the known field/rule needed to correct its input without preserving raw inputs or changing validation requirements. The exact invalid inputs are not retained, so their cause must not be invented from the generic error.

Safe schema-derived recording diagnostics were implemented and passed 152 focused checks. Opus review was unavailable after repeated provider 429; independent agy Gemini 3.8 Flash High reviewed the final diagnostic delta and found no essential issue. In frozen-source live run `run_272e975773c4`, a recording failure returned the actionable rule/location `$.facts[2].raw_path[2] / string_type` without values. An earlier recording call partially stored one hypothesis and two branches while refusing nine facts for `raw path missing`. No task or goal settlement occurred. The next investigation compares the published raw-path contract with the actual bounded original-result delivery. Exact rejected arguments are unavailable; preserve that uncertainty. Partial graph writes must be distinguished from whole-call `ok` in final audit.

Raw-path plan decision: clarify the recording-owned public description and support equivalent envelope-prefixed/output-relative paths in both fact consumers. Try the existing relative address first; only a missing address may retry once without a leading `output`. A found value mismatch must remain a mismatch. Accept strict string/integer path segments without boolean/float coercion, matching actual JSON address semantics. Retain origin, target, value equality and partial acceptance. Do not add reverse numeric-dictionary-key guessing or infer the exact prior invalid input. Gemini's plan conditions on deterministic fallback and strict types are adopted; its asserted literal cause is not supported by retained inputs. Implementation is delegated to DeepSeek Max.

The data-only harness now counts all graph-record calls and checks persisted partial items independently of whole-call success. Offline audit of the older report keeps canonical settlement false; the original report is preserved.

The raw-path repair passed 123 focused checks and independent Gemini code review. In the next single frozen-source live run, `run_e5c20fbdaf8b`, the model performed eight remote recon/Web calls, partially recorded seven facts, four open hypotheses and three branches, and successfully called task and goal settlement. Two named unresolved records match the terminal projection; the task is blocked with no pending/ready/running task. This is canonical unresolved, not a verified finding or mission achievement. Independent requirement-by-requirement audit accepted the chain and its independent body-evidence limitation. Path-final Linux checks passed on both required Python versions (3049 passed / 51 existing skips, Ruff/mypy exit zero). See `final-verification.md` for the model-context visibility limitation and exact evidence boundaries.
