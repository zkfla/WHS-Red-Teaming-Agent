# Planning error: live reproduction and diagnosis

2026-10-14. Original `WHS-Red-Teaming-Agent` checkout, `dev`. Diagnostic fidelity implementation and live reproduction requested by the user. These are real provider calls against the user's configured local synthetic bank; offline fixture checks are listed separately below.

## Reproduction conditions

Fresh browser session for each attempt; identical input: `대상에 대해서 취약점을 분석해줘`. Existing engagement `engagements/engagement.yaml`, allowed host `127.0.0.1:8081`, existing MCP deployment, `native-probes` profile. Provider `opencode-go`, model `openai/responses/muse-spark-1.3-contributor`, reasoning effort `high`, original request limit 1500 and concurrency 3. No scope/approval relaxation, provider substitution, extra-high effort or manual approval clicks. The bank container remained healthy.

The web server was restarted to load the changes. Environment values took precedence over saved browser model choices on startup, so the original provider/model/effort were reapplied through the normal settings API before sending each request. The stored key was checked for presence only and never displayed. This precedence observation is distinct from the planning failure.

## Actual results

| Attempt | Run | Known run tokens | Tool actions | Terminal result | Observed rejection rules |
|---|---|---:|---:|---|---|
| 1 | `run_63c7cf164d21` | 74601 | 0 | `planning_error` | Empty judgment missing termination/resume conditions; then `ValidationError`, `value_error` at `$.updates[0]` |
| 2 | `run_6c48379ca3c2` | 72330 | 0 | `planning_error` | Four `ValidationError` rejections, `value_error` at `$.updates[0]` |
| 3 | `run_2ec679b01cea` | 73117 | 0 | `planning_error` | `candidate_update_requires_v2` at `$.updates[0]`/`$.updates[1]`; empty judgment missing termination/resume conditions |

Each run exhausted the normal first proposal plus three repairs in `hypothesis_planning`, before atomization and target tools. Total recorded run tokens: 220048. These token counters do not assert billing cost or include every separate conversational request. The overall 1500-request cap was not consumed. No 429, extra-high rejection or deployment absolute-path failure appeared in these three run diagnostics.

For attempt 3 only, the two existing `HypothesisUpdate._version` rejection branches received static `PydanticCustomError` rule IDs, without changing their conditions or permitting invalid updates. This distinguished the exact failing rule while excluding model inputs, exception message bodies and private reasoning from stored diagnostics.

## Confirmed cause and limits

`src/redteam/core/contracts/graph.py` defines `Hypothesis.contract_version` as `Literal[1, 2]` with default `1` for historical compatibility. `HypothesisUpdate.hypothesis` uses that same type, but its after-validator requires version `2`. Consequently the proposal schema permits/defaults version `1`, while the actual update validator rejects it. Attempt 3 confirms this branch (`candidate_update_requires_v2`), not the separate independent-review-receipt rejection branch. A missing version and an explicitly supplied version 1 both produce the same parsed v1 value; raw proposals were deliberately not retained, so those two forms cannot be distinguished retrospectively.

The other observed error is `empty_judgment_needs_termination_and_resume_conditions`: a proposal with no updates/questions must explain held/exhausted termination and resumable conditions. This is a separate semantic rejection, not a provider or bank connection failure.

The diagnostic fixes now preserve public field locations, rule IDs and exception type chains in repair feedback and durable reports. They do **not** make the bank analysis succeed: all three attempts still stopped before target execution. The remaining functional correction is to align the planner's update proposal schema with its existing v2-only contract, while keeping historical standalone v1 hypotheses readable. This was identified by the additionally requested live diagnosis; it is not claimed as implemented by the diagnostic-fidelity patch. Repair budgets and validation requirements were not loosened.

## Evidence and verification

Original terminal reports are `data/reports/<run>.json`. Read-only central report generation produced `reports/<run>/report.md` for all three runs. For attempts 1 and 3, `reports/<run>/report.json` was also generated to compare the durable diagnostic with the original. The comparisons preserve the diagnostic and display phase `failed`. The old run's recovered report remains `reports/run_d3ccaec68131/report-diagnostic-recovered.md`.

Offline checks: 113 worker planning/atomization/diagnostics tests, 10 independently repeated new planner/central tests, 32 Linux planner/v3/central regression tests, 8 candidate-rule/new planner tests; full Ruff passes and full source/tests mypy passes 777 files. Earlier neighboring Linux completion/recovery and report/resume/approval/CLI runs passed 35 and 41 respectively. These groups overlap and are not summed into a fictitious unique-test total. A Windows neighboring test's 2-second startup timeout occurred before the changed settlement path and passed on Linux without assertion relaxation.

Review: DeepSeek independently reviewed the worker changes, durable snapshot guards and coded candidate rules. A reproduced stop-after-planning-error regression in the initial storage patch was corrected and covered by a test. User changes and runtime engagement files remain outside the implementation commits.
