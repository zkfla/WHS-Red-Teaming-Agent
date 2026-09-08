# G9 local release gate — preregistered execution record

2026-09-09 · **PASSED_OFFLINE**. This file preserves the preregistered G9 gate,
all adverse/inconclusive measurements and the final acceptance evidence. G9
does not claim live-provider, real-target or universal performance validation.

## Scope and comparison contract

- No domain integration, external provider, live target, automated CI service,
  commit or push. Preserve historical receipts and their exact source hashes.
- G1: compare full scan/indexed commit, freshness and payload separately.
- G2: compare sink memo off/on with the same legacy query budget. Work-budget
  v2 is a distinct bounded-search contract, not a speed optimization.
- G6: compare legacy/focused context, preserving the active candidate's required
  evidence roles and normative baseline. Synthetic UTF-8 token estimates are
  not provider tokenizer measurements or live LLM quality evidence.
- G8: compare baseline/utility with the common admission and durable decision
  binding contract. The final G8 quality receipt already withholds utility
  default enable because two fixed-budget fixtures regress. Preserve this result.
- Exercise every G1/G2/G6/G8 toggle combination in a composed offline scenario,
  with cold execution, warm operation-local cache reconstruction and checkpoint
  resume. Compare full canonical state for structural switches; compare priority
  choices only within the same versioned provider policy. Context representations
  differ intentionally, so compare required canonical references separately.
- Serial/concurrent execution must consume the same fixed response/clock tape.
  Compare complete canonical output and exact physical request accounting, not
  only aggregate counts. Resume must preserve durable history and must not replay
  dispatched work merely to reconstruct a cache or priority selection.

## Evidence and decision rules

1. G8 full semantic regression, source review, explicit approved hash update and
   subsequent source/reference checks precede G9 execution.
2. Functional matrix and fault tests are separate from timing measurements.
   Run timing in isolation, alternate paired order, retain raw samples and use
   the existing 30-sample/5-warmup release runner where applicable. Preserve
   timeout, memory-limit, failed and inconclusive cases rather than averaging
   them away. Record source/config/fixture/harness hashes and environment.
3. Keep the original plan thresholds: representative p50 improvement >=20%,
   p95 degradation <=10%, small-input and peak-memory regression limits.
   A combination correctness pass cannot substitute for an individual speed gate.
4. Source-changing default decisions require another semantic/source/build gate.
   If current-source evidence is insufficient, retain the existing default and
   document TESTED_OFFLINE / withheld instead of enabling speculatively.
5. Audit G5's conditional trigger against actual profiling evidence. A missing
   attribution of full-copy/recompute time is UNKNOWN, not evidence it is <20%.
   Do not introduce dependency-driven recomputation without its declared trigger.
6. Finish with the original semantic regression, four reference domains and
   composite, architecture, determinism, compatibility, Ruff, compileall and
   package build. Audit authoritative status documents for stale current claims.

## Required outputs

- Current-source combination/cold/warm/resume and serial/concurrent receipt.
- Current-source per-feature performance/default decisions with limitations.
- G5/C1-C4 disposition and compatibility/rollback instructions.
- Final regression/source/build results.
- `GENERIC_CORE_PERFORMANCE_ACCEPTANCE.md` and updated plan/status index.

All required outputs are closed in the final acceptance section below. This
heading and list preserve the gate that existed before measurement.

## Current-source G1 remeasurement

`GENERIC_CORE_G9_G1_RELEASE_AB.json`: source integrity PASS, core
`921c1003cc410e8a967a3d358977570ebf07f08705c6f70a12de98509e99a09e`,
5 warmups + 30 alternating pairs per completed case. Seven cases completed;
commit/freshness/payload at 10,000 Observations each hit the 60-second case
watchdog. They remain TIMEOUT, not successful measurements. The runner exited
1 because it preserves these incomplete scale cases.

| Stage / size / cycles | p50 off -> on ms | p50 ratio / 95% interval | p95 ratio | Python peak ratio |
|---|---|---|---|---|
| commit / 100 / 1 | 6.8800 -> 7.6105 | 1.1062 / .9903..1.1485 | 1.1087 | 1.0057 |
| commit / 1,000 / 1 | 83.7070 -> 73.9696 | .8837 / .8511...9568 | .8348 | 1.0008 |
| freshness / 100 / 1 | 3.7068 -> 4.1057 | 1.1076 / .9348..1.2074 | .9077 | .9998 |
| freshness / 1,000 / 1 | 44.1917 -> 38.3978 | .8689 / .7727..1.0069 | .9754 | 1.0009 |
| payload / 100 / 1 | .1097 -> .0236 | .2151 / .2099...2242 | .2091 | **1.2005** |
| payload / 1,000 / 1 | 6.9097 -> .1626 | .0235 / .0222...0250 | .0290 | 1.0011 |
| commit / 100 / 10 | 29.9645 -> 28.9064 | .9647 / .9225..1.0103 | .9622 | .9940 |

Commit/freshness do not meet the representative 20% speed threshold. Payload
lookup meets speed thresholds on the two completed sizes, but its size-100
separate fixture+operation+verification allocation sample was **359,660 ->
431,770 bytes (20.05% increase)**. Do not round this down to a memory PASS.
No separate justification has been approved; payload default enable is therefore
still withheld. This is a measured peak, not proof of a persistent cache leak.
The 10,000-size cases remain unproven. Existing defaults have not changed.

## Functional composition matrix

`tests/integration/test_g9_composition.py`: **18 PASS (249.16s)**. All sixteen
G1/G2/G6/G8 toggle combinations exercised serial and concurrent-2 execution,
cold/warm projection and a newly constructed runtime/registry after checkpoint
resume. Two additional tests compare all eight structural combinations within
each fixed priority policy. The total is 48 composed executions, not 48 distinct
pytest tests. Full canonical objects preserve IDs, timestamps and provenance;
structural toggles preserve policy-specific selection. Restoring and committing
the completed batch makes zero new resolver calls and retains priority history.

The fixed two-call response tape is independent of priority; this is structural
integration evidence, not a substitute for the G8 fixed-budget quality evaluation.
After adding explicit 22-Observation/4-consistent-slot/2-selected-candidate oracle
checks, the baseline serial/concurrent case passed again (**1 PASS, 10.80s**).
The expanded whole-composition runner has separate watchdog/error/canonical-
mismatch/receipt-no-overwrite canaries: **4 PASS (1.14s)**. Its actual 30-pair
measurement was still pending at this recorded stage. Ruff PASS. Core source was unchanged.

## Current-source G2 remeasurement

`GENERIC_CORE_G9_G2_RELEASE_AB.json`: source/harness integrity PASS on the same
approved G8 core. Thirty pairs plus five warmups; four cases completed and both
10,000-Relation cases hit the 60-second watchdog. TIMEOUT remains unproven.

| Fixture / Relations | p50 off -> on ms | ratio / 95% interval | p95 ratio | Python peak ratio |
|---|---|---|---|---|
| fanout / 100 | 5.3448 -> 5.3205 | .9955 / .8861..1.1029 | .7465 | 1.0015 |
| fanout / 1,000 | 62.5285 -> 63.6144 | 1.0174 / .9653..1.1201 | .9143 | 1.0207 |
| shared suffix / 100 | 4.4702 -> 3.3594 | .7515 / .7048...8071 | .9836 | .9983 |
| shared suffix / 1,000 | 54.5962 -> 42.7852 | .7837 / .7597...8237 | .7719 | 1.0000 |

The designated shared-1,000 workload meets the point-estimate p50 threshold
(21.63% improvement), p95 and memory limits. Its confidence interval shows
uncertainty in the exact benefit, not a universal >=20% guarantee. Fanout controls
do not establish a speedup; their small regression is below the declared limit.
At that intermediate checkpoint, memo default enable was still a G9 candidate awaiting composition/default-change
regression. No default has been changed yet; bounded work v2 is not conflated
with the legacy-budget memo timing comparison.

The isolated whole-composition baseline/structural/all arm benchmark was then
started with 30 planned pairs, five warmups, per-case/total watchdogs and separate
allocation samples. No correctness tests are run concurrently with its timing.

## Whole-composition result and final-audit source changes

`GENERIC_CORE_G9_COMPOSITION_RELEASE_AB.json`: **PASS**, 105 timing executions
(three arms, five warmups plus 30 samples each), three separate allocation runs,
source/harness integrity PASS, 748.05 seconds total. This receipt belongs to the
pre-cap G8 source `921c1003...a09e` and is not relabeled as a later source result.

| Arm | p50 / p95 ms | ratio to baseline / 95% interval | separate Python peak bytes |
|---|---|---|---|
| baseline | 5,100.01 / 5,713.11 | 1 | 5,658,808 |
| G1+G2+G6 structural | 5,050.997 / 5,466.87 | .99039 / .97474..1.00339 | 5,658,625 |
| G1+G2+G6+G8 all | 5,128.056 / 5,635.67 | 1.00550 / .98395..1.02482 | 5,709,020 |

No overall-workflow speed improvement was established. Full canonical outputs
were identical; ranking-policy outputs were repeat-stable within each policy.
The fixed response tape does not evaluate priority quality. The per-feature
graph speed evidence and whole-workflow timing are explicitly different claims.

The final requirement audit found the operation-local Observation index lacked
the common contract's explicit item/reference-byte caps. New contract tests
first failed at that missing interface (14 failures; not an OOM reproduction).
The index now admits at most 100,000 memberships and 8 MiB of UTF-8 membership
IDs. These bytes are not payload bytes or Python heap/RSS. Overflow discards
the private partial index and returns a full-scan fallback, never incomplete
canonical knowledge. Knowledge, focused context/retrieval and priority share
this behavior. Relevant differential tests: **56 PASS (1.26s)**.

G2's provisional default change enables memo only for omitted-policy **legacy**
searches. Explicit `memoize_sink=False` remains rollback; explicit v2 retains
its old work-counter behavior unless memo is requested. Invalid memo flags reject.
Default/overflow/frontier cases: **82 PASS (.78s)**. Extended cap/compatibility/
opaque-state/step integration: **35 PASS (27.42s)**; harness checks: **5 PASS (1.24s)**.

Runtime/package candidate is **0.4.1**, checkpoint schema remains **4.0** because
no durable field changed. Exact runtime-version binding rejects 0.4.0 resume;
no migration or history reset is introduced. Ruff, compileall and sdist/wheel PASS.
Candidate source is `e19036b661e15738b8a582acebda156d6ce1946ea515bb92179cba3bb318af10`.
The approved baseline remained G8 at this stage pending final source/default gates. Final-source
G2/G6 remeasurement and full semantic/reference audit are still required.

## Final remeasurement: default-enable veto

`GENERIC_CORE_G9_G2_FINAL_AB.json`, candidate source `e19036...af10`, source
integrity PASS: four completed cases, fanout-10,000 RESOURCE_LIMIT and shared-
10,000 TIMEOUT. Resource-constrained cases are not counted as successes.

| Fixture / Relations | p50 off -> on ms | p50 ratio | p95 ratio | Python peak ratio |
|---|---|---|---|---|
| fanout / 100 | 5.5818 -> 5.4272 | .9723 | .9084 | .9994 |
| fanout / 1,000 | 61.5364 -> 64.0883 | 1.0415 | **1.2440** | 1.0207 |
| shared suffix / 100 | 4.3882 -> 3.4642 | .7894 | .9081 | .9983 |
| shared suffix / 1,000 | 60.0402 -> 45.3083 | .7546 | .7056 | 1.0000 |

The designated shared suffix improved, but the latest fanout control has a
24.4% p95 regression. Conservatively veto the provisional blanket legacy memo
default rather than discard this control or reinterpret the tail gate after
seeing it. Keep memo opt-in. This is the plan's rollback/default-withholding
workflow, not a redesign of the objective. Current in-flight G8 comparison
finishes on its frozen candidate source before reverting the default and rerunning
final regression. No optimization threshold or score weight is changed.

`GENERIC_CORE_G9_G6_FINAL_AB.json`: source/harness integrity PASS on the same
candidate, 30 alternating pairs. Synthetic UTF-8-quad input tokens are
**65,614 -> 781 (98.81% reduction)**, complete bundle count 1, required omissions 0.
Separate fixture+projection+verification Python allocation peaks are
**2,364,618 -> 1,554,516 bytes**. Real-provider token/latency/quality remains NOT_RUN.
These are the recorded source's measurements, not a universal agent speed claim.

## Final default rollback and regression freeze

The candidate-source G8 comparison finished all **20 arms**, source/harness
integrity PASS, zero incorrect root decisions, the same two budget-3 coverage
regressions. Receipt: `GENERIC_CORE_G9_G8_FINAL_AB.json`. Utility remains opt-in.

After those source-frozen measurements finished, the provisional regression
process was stopped intentionally before completion so it would not be mislabeled
as testing a later rollback. It was neither a full PASS nor a semantic failure.
Only that task's verified pytest child process was stopped; unrelated processes
and user data were not touched.

The final candidate preserves memo-off defaults for both budget contracts.
Explicit true/false policies remain available. A read-only exact-byte reversal
of the two default-policy/docstring changes reproduced the measured `e19036...af10`
source hash. Thus G2's measured **explicit** off/on code paths, G6 and G8 are
unchanged by the rollback; the JSON files keep their original candidate hashes.
No claim of a new whole-workflow speed gain is made from these measurements.

Final candidate core:
`7293c3e3ca79ba3177f27ee5a52c48c41719e7573996f1325a522b54b9945ce5`.
Default/cap/compatibility checks: **30 PASS (1.68s)**. Ruff, compileall and 0.4.1
sdist/wheel PASS. The paragraphs through the initial full-suite result below
record the state before the final isolated composition run and hash approval.

Final-source determinism checks on priority, memo/work-budget and reference domains:
`PYTHONHASHSEED=1`: **67 PASS, 1 prior-release hash gate deselected (1.42s)**;
`7`: **67 PASS, 1 deselected (1.34s)**; `777`: **67 PASS, 1 deselected (1.40s)**.
These focused checks ran during the full semantic regression; their durations
are not performance samples. The final source hash approval test remained a
separate gate and is closed below.

### Final full semantic regression completed

Final core `7293c3e3...9945ce5`: **1,164 PASS, 2 explicit prior-release source-hash
checks deselected (816.92s)**. No semantic/architecture/reference/fault test failed.
The focused seed checks and package checks ran alongside portions of this suite;
its elapsed time is not a benchmark. The approved hash was not updated until the
remaining G9 gate completed below.

## Final isolated composition and release acceptance

`GENERIC_CORE_G9_COMPOSITION_FINAL_AB.json`: **PASS** on the exact final core
`7293c3e3...9945ce5`. The source and harness/lock fingerprints were identical
before and after (`216d6e05...5e92` for the harness). The run completed 105 timing
executions (five warmups and 30 retained samples for each of three arms) plus
three isolated allocation samples in 785.44 seconds. Every repeated output and
the full canonical state comparison passed; no timeout or resource-limit row was
discarded because none occurred in this final run.

| Arm | p50 / p95 ms | p50 ratio to baseline / 95% interval | Python peak bytes |
|---|---|---|---|
| baseline | 5,343.614 / 5,772.708 | 1 | 5,660,518 |
| G1+G2+G6 structural | 5,344.083 / 6,119.285 | 1.00009 / .97492..1.02052 | 5,658,260 |
| G1+G2+G6+G8 all | 5,375.993 / 5,854.211 | 1.00606 / .97167..1.02742 | 5,711,135 |

This final composition proves compatibility and bounded-resource behavior, not
an overall speedup: both p50 comparisons are statistically inconclusive and do
not meet the preregistered 20% improvement threshold. G1 commit/freshness and
payload defaults, G2 memo, G4 concurrency and G8 expected-utility ordering remain
off by default. The explicit alternatives and rollback paths remain tested.

The final core hash was then written to
`tests/baselines/generic_core_release.json` as
`g9-performance-evolution-tested-offline-defaults-withheld`. The subsequent
source/reference gate passed **10/10 (1.36s)**. Final Ruff, compileall,
sdist/wheel 0.4.1 and `git diff --check` also passed. Finally, the approved tree's
complete test collection (**1,166 tests**) ran to 100% with exit 0 and no failure,
skip or deselection markers. This supersedes the sequencing-only split of 1,164
PASS plus the two then-stale hash checks; the earlier 816.92-second run and 10-test
post-approval gate remain preserved as independent evidence. Current
architecture/contracts/testing documentation separates retained CI/CD donor
records from the Generic Core API and procedure.

G0/G0-C1/G1/G2/G3/G4/G6/G7/G8/G9 are **PASSED_OFFLINE**. G5 and C1-C4 remain
explicitly **DEFERRED** under their untriggered conditions. No CI/CD/domain
implementation, live LLM/provider, real target, commit or push was performed.
