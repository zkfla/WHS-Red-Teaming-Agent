# Plan7 integration overview

## Latest user decision — direct target execution (2026-10-15)

The user explicitly selected the teammate's direct-program provider as the preferred default and authorized adapting the plans and importing that implementation with concrete bug fixes. This decision supersedes earlier acceptance requirements that mandate a packet gate, sidecar/NFQUEUE, byte/flow network ledger, or deny target readiness solely because arbitrary programs cannot enforce HTTP method/path/header/session restrictions. Original source plan snapshots remain historical design data, not an override of this decision.

The integration target is origin/dev 38179561 direct_target / prepare_kali_network / production bootstrap, preserving verified planner, atomizer, structured diagnostic, original-evidence and completion fixes. Validate actual selected Docker endpoint/bridge identity, declared target/port and explicit scope/deny admission, ordinary human approval, execution request/time/output budgets, same-instance exclusion, cancellation/timeout and network detach, protected original results, truthful UNKNOWN, and configuration-bound resume. Target-ready means measured direct-route readiness and must retain truthful network_enforcement=none; it must not claim packet isolation or per-request HTTP enforcement. No model NET_ADMIN/NET_RAW or automatic scope expansion is introduced by this choice.

Physical acceptance now exercises the actual direct provider through the production assembly/common-worker path against an explicitly owned local fixture, including admission refusal, delivery, timeout/cancellation/detach and stored result/report/resume. Old gated-wire deny and verified gate-settlement tests are not prerequisites for the new default. Existing dormant gate code and unfinished gate-specific repair are preserved at a safe checkpoint; they are not claimed complete or silently activated. Do not add a new alternate-mode framework merely to retain the abandoned design.

Import is staged after current writers freeze, preserving unrelated local files and staged boundaries. Review teammate code for concrete errors rather than rejecting its chosen architecture. Reconcile overlapping planner/context/provenance changes and avoid duplicate descriptor registration. Re-run applicable checks after integration and retain the final review/repair/test/commit/push requirements. The earlier instruction to postpone remote inspection/integration is superseded to the extent required to bring in the implementation the user now selected.


## Status

IMPLEMENTING — 2026-10-14. User explicitly requested complete implementation of planner progress/recovery + plan7a–e + concrete ConversationError diagnostics, separate ExecPlans, stage checks/commits, final review→repair→tests. Original Windows checkout C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent, dev376b479d; preserve user changes. Current active goal is authoritative.

## Authority and Inputs

Root AGENTS.md → .agent/PLANS.md → core/capability/orchestration/control/app AGENTS.md. The source plans are copied unchanged under docs/plans/plan7-source-20261014/ for review and traceability; they are specifications, not autonomous permission. User current request authorizes all stages (overrides attached "do not start next stage" restriction). Commit at each checked stage; no automatic external account/service/security scope expansion. Prior authorization for local bank live testing exists, but actual engagement policy/ROE/approval must remain in force.

## Goal / Requirements

Implement all requirements in child plans; final completion includes real Kali target route proof, correct source/result feedback and independent candidate/Task boundary. Registration, prompts and fixture success alone do not prove provider execution or vulnerability discovery. User specifically adds executable-continue/no-progress, concrete bounded repair feedback, separate transport and output-correction counters, safe durable/input-visible ConversationError diagnosis.

## Stages / Task DAG

| Stage | ExecPlan | Requirement set |
|---|---|---|
| 01 | [대화 오류 진단 보존과 API/답변 수정 재시도 분리](../completed/plan7-01-conversation-recovery.md) | D01, D02, D03 |
| 02 | [이어갈 경로가 있는 continue와 무진행 처리](plan7-02-planner-progress.md) | P01, P02, P03 |
| 03 | [Kali 설정·준비·조립 연결](plan7-03-kali-preparation.md) | A01, A02, A03 |
| 04 | [Docker 대상 route와 네트워크 gate 정합성](plan7-04-kali-route.md) | B01, B02, B03, B04 |
| 05 | [공통 워커 Kali 선택·실행·원본 피드백](plan7-05-kali-worker-feedback.md) | C01, C02, C03, C04 |
| 06 | [모순 완료 조건과 정당한 원본 재사용](plan7-06-observation-completion.md) | K01, K02, K03, K04 |
| 07 | [실제 Kali 전체 실행 경로와 상태·재개 검증](plan7-07-kali-physical-proof.md) | E01, E02, E03, E04, E05 |
| 08 | [최종 독립 리뷰·수정·테스트](plan7-08-final-review.md) | F01, F02, F03 |

01→02 for recovery semantics; 03→04→05→06→07 retains plan7a–e implementation order. Read-only discovery and disjoint01/03 packages may run in parallel after plan review. 08 follows all implementation, then independent review→repair→test. Stages expose actual dependencies and file owners before dispatch. Scope shared-contract edits serially; fake model/real provider execution distinguish explicitly.

## Facts / Unknowns

Confirmed local HEAD376b479d profile9/atomizer2 differs from input plan claiming atomizer3. Source run_a3947f4a3758 and claimed prior condition-ID fix must be located/verified before relying on them. Previous locally observed runs update1/questions0/continue/held are evidence of incomplete progress flow, not evidence that all current/succeeding branches fail. Docker context currently desktop-linux, Linux kernel6.18.33.2-microsoft-standard-WSL2; actual quota/NFQUEUE/readiness remains unknown. Local Kali and sidecar images exist but image presence is not physical route proof. No assumption that a past Mac kernel blocker applies here.

## Model / Ownership Ledger

- Root owns whole task interpretation, plans, integration, validation evidence, stage commits and final judgment.
- Orchestrator: /root/plan7_orchestrator, opencode-go/deepseek-v4.1-flash max.
- Workers must use equal dispatch counts and comparable bounded workloads among gpt-6-luna xhigh, opencode-go/muse-spark-1.3-contributor xhigh, anthropic/claude-haiku-5-5 xhigh. No substitution authorized. General models_cache verifies Haiku xhigh despite initial abbreviated delegation list; orchestrator notified to use exact selector. Actual handles/results determine execution, not catalog alone.
- Primary reviewer: /root/plan7_review_sol, gpt-6.1-sol high, successfully spawned. Auxiliary reviewer: agy gemini-3.8-flash-high (effort high), list_models verified. Plan review before implementation; final review again after integration.
- Workers no further delegation, git operations or scope expansion; orchestrator routes packages and root integrates. Existing agents from prior goals are unrelated.

## Test Strategy / Completion Audit

Per-stage source-backed reproduce+regression tests, module responsibility review and scoped commit. Current CI uses Python3.10/3.14 on Linux; protocol verification also Windows. Full applicable Ruff/mypy and compose configuration checks at final stage; do not label pre-existing POSIX limitations as newly fixed. Physical disposable local Kali/mock evidence must include server receipt, protected execution, original ToolResult and gate ledger/teardown. Paid LLM or existing Run changes must stay user-approved and preserve exact original action identity.

Every child requirement needs source/test/physical artifact evidence matching its scope. Review/repair/test alone is not sufficient if physical requirements remain unknown. Goal stays active if incomplete. Final docs list implementation, fake tests, physical Docker/Kali, live model calls, findings and environment blockers separately.

## Progress / Decision / Review / Validation Log

2026-10-14: Read AGENTS and PLANS; six source documents copied; eight child plans created. Initial reviewer confirms candidate-only continue allowed, transport errors consume correctionpool, ConversationError loses structured cause. No implementation claimed at plan creation. Review and stage results pending.

### Independent plan review reconciliation

Sol High reviewed concrete plans: conditional approval, HIGH common ROE enforcement limitation was added to04 B04/05 C04/07 E05; MEDIUM ownership/interface packets were recorded for01 before dispatch. Later stages require similarly concrete packets before source mutation.

agy job1791969846332920300-3d99d9f953543cc29df1ca9c, Gemini3.8FlashHigh completed read-only plan review. Root accepted evidence-backed diagnostic/counter split, explicit no-progress owner, purpose-source/consumer validation and serial shared-interface freeze concerns. Transport retry must reuse the current request/repair prompt state, never append transport failure as if it were model-output correction feedback.

Root rejected unsupported review claims: web selection already passes through request-local environment_context (app/web/host.py76/100, app/web_policy.py344+); program.list/describe handlers and contributions exist (orchestration/tool_discovery.py694/772/1019+); run_a394 is absent here and the source document describes12 tool calls, not candidate-only tools0; current nft check passed, so no current quota-blocker conclusion follows from older Docker records. No duplicate discovery handlers/config runner are authorized by those claims. Review's suggestion to consider physical proof optional is not accepted: missing proof remains an incomplete goal item, with environment diagnosis where applicable.

Current stage01 is IMPLEMENTING; implementation packets are in its separate ExecPlan. Other stages remain PLANNING until source reconnaissance and dependency interfaces are verified.

### Initial actual worker ledger and coordination

Orchestrator reports stage01 Retry running at /root/plan7_orchestrator/recon7ab_luna (Luna xhigh), Configuration at /root/plan7_orchestrator/recon7cd_muse (Muse xhigh); root bounded Conversation worker /root/plan7_conversation_haiku (Haiku xhigh) owns all conversation/session/CLI catch files. A concurrently dispatched coordinator Haiku conversation worker was immediately interrupted with no source edits; it does not own files. Worker file mutation requires this ownership ledger, not mere model registration.

Research Muse worker improperly delegated three children before the no-further-delegation instruction was reinforced. Their read-only results were reconciled; dispatches are recorded, not omitted from accounting. Coordinator counts at this point: Luna2, Muse5, Haiku2 (one stood-down), plus root Haiku1. Balance later meaningful Luna/Haiku implementation packages against the extra Muse research calls; do not launch filler work solely to adjust counts. Distinguish attempted dispatch, completed work and model failure in final ledger.

Research confirmed web closed/configured/inherit propagation already exists; standalone CLI saved-selection hydration and new-policy preparation remain to inspect/implement. Existing program discovery/paging/describe and shell evidence windows are reused. Current managed Kali image a85c97d7... is Kali2026.3 and matching cached manifest has19 program records including curl/nmap/sqlmap; old repository fixture manifest targets a different d9d2... image and must not be mixed with it. Kernel readiness and42 existing operator-policy/CLI-preparation tests passed; actual target packets/common-worker proof remain pending.



Stage01 completed with scoped Sol reviews, one timeout repair/recheck and root tests/type/lint/JS gates. Stage02-A is implementing after reviewed concrete packets; remaining stages and final physical proof are open. Plan01 moved to completed; this is not whole-goal completion.

User steering (2026-10-14): if an actual Muse/DeepSeek429 occurs, replace coordinator with anthropic/claude-sonnet-5 Medium and use only Luna/Haiku workers at their existing XHigh effort. Before the trigger current assignments remain; after it the earlier three-model equal ratio is superseded. Exact Sonnet selector and Medium support are present in the general local model catalog. Record only safe status/model/stage evidence, stop repeated quota retries, and preserve scopes/ownership at handoff. No trigger has been confirmed at this checkpoint.

Integration checkpoint: 02-A accepted and committed0a3292d3 (fresh Sol, root Linux74/type/lint); 03-S accepted and committedc5258156 (fresh Sol, root Linux61/type, worker201/lint). Stage02-B authorized after A handoff. Stage03-P authorized after S freeze;03-U authorized only pure selection/form phase until P prepared-result freeze. Full02/03 traces remain open. Stage04/05 packets are in source-backed reconnaissance,06 preliminary independent gap review; no later-stage mutation approval yet.

Muse pure-U actual failure: invalid_request_error, model failed to generate response, no429 label and no source edits. Root does not infer quota exhaustion or trigger conditional model substitution from this code. One concise bounded retry authorized; repeated invalid-request failure leaves U pending while independent work proceeds, actual429 triggers the user-selected fallback. No secret request payload recorded.

ACTUAL429 trigger: predecessor /root/plan7_orchestrator (DeepSeek4.1FlashMax) failed after retry limit with429 Too Many Requests. User conditional fallback now active. Root started /root/plan7_sonnet_orchestrator using anthropic/claude-sonnet-5 Medium (actual invocation succeeded); interrupted running Muse pure-U retry, retains user/state changes, assigns future workers only Luna/Haiku XHigh. Existing B/P/06 workers notified of coordinator handoff; source ownership and review gates preserved. No more DeepSeek/Muse retries under this goal unless user directs otherwise.

User model override supersedes conditional fallback: Claude quota exhausted; all implementation workers now GPT6-Luna XHigh only, orchestration GPT6.1Sol Light interpreted supported low effort. Root started /root/plan7_sol_orchestrator using gpt-6.1-sol low successfully; stopped Sonnet coordinator and Haiku handles. B and06 Haiku were actual429errored, partial edits preserved and reassigned for Luna continuation; U had no source edits. Existing Luna04N retains source ownership. Root confirmed user follow-up Luna workers. Reviews remain separately requested SolHigh/agy unless changed. No further Claude/Muse/DeepSeek worker retries.

Continuation audit2026-10-15: previous goal turn classified PROGRESS (04Nsourcecommit4ddcece8 and B repair validation52Linux/type6), not mere wait. Actual current checkout dev4ddcece8 dirtyB/U/06 preserved. Coordinator was interrupted; U/06workerhandles terminal usage-limit errors, Bfrozen. Current account tool ordinaryUsageAllowedtrue primary1%/secondary28%; one bounded reactivation requested using latestmodelselection, no silent substitution or restarting a live handle. Full goal remains active; 04C/R,02C,03U,05,06,07physical and08review still open.

2026-10-15 Bacceptedcommitda27d0d2 afterhistoricalsettingsrepair andLinux52/type6/freshSol; C authorizedsameLuna toactualsettlement/report/resume/prompt afterBfreeze. Nacceptedcommit4ddcece8;04C/R stillinterfacegated. U/06handles reactivated once and confirmedactuallyrunning undercurrentordinaryusageavailability, partialchangespreserved. Fullgoalphysical/finalreview remains unproven.

Latest user model override2026-10-15: Claude quota returned; prospective implementation workers use GPT6-Luna XHigh and anthropic/claude-haiku-5-5 XHigh in equal meaningful dispatch/work proportions. Existing running Luna ownership continues unchanged; no restart solely to rebalance. Orchestrator remains gpt-6.1-sol low, primary reviewer SolHigh and auxiliary agy Gemini3.8FlashHigh unchanged. This supersedes the earlier Luna-only rule for new worker assignments; no DeepSeek/Muse/Sonnet coordinator resumption.

Subsequent user override supersedes the half/half rule: existing Luna workers finish only their currently assigned packets; do not assign new work after those packets complete. New worker packets use anthropic/claude-haiku-5-5 XHigh exclusively. If an actual Haiku429 occurs, switch worker assignments to GPT6-Luna XHigh, preserving partial work and concrete ownership handoffs; observation timeouts or unrelated failures do not constitute this trigger. Coordinator remains Sol6.1 low and review roles unchanged. This latest rule governs the queue below, including new follow-ups after a Luna packet freeze.

Latest effort correction: prospective Haiku worker assignments use High, not XHigh. Already assigned XHigh work retains its effort; root06D read-only packet handle plan7_06d_packet_haiku was dispatched XHigh before this correction and stays assigned. New Haiku-only allocation and actual429→Luna fallback remain as above. Orchestrator/review efforts unchanged.

User Haiku context constraint: complete each bounded packet within100K context where possible; give new workers only the required packet and source/interface references, not full conversation history. Near100K or once exceeded, freeze the old worker and transfer a compact checkpoint (exact owned paths, interface decisions, current changes, verification results, remaining actions) to a new HaikuHigh handle with explicit ownership handoff. Workers do not spawn successors themselves; root/coordinator does. Use actual context measurements when available; cumulative generated tokens are not context size and an unexposed measurement must not be claimed as enforced. Existing live workers are not duplicated while writing.

User final integration timing: inspect and incorporate teammate dev pushes only AFTER current implementation work is complete, immediately before final push, then verify affected behavior. Root had already fetched origin/dev96c183a2 and read commit names/diff statistics when the user corrected the timing; no semantic review, merge, branch switch or source integration occurred. Defer further remote inspection/reconciliation now and continue the local approved packets. At final pre-push fetch again for authoritative current state, preserve checked local commits/user changes without automatic stash/reset, review semantic/functional overlap, integrate and rerun affected tests before a normal push. Remote changes are not silently assumed compatible or already included.

### Atomic integration queue — latest user parallelization/atomization steering

Each row delivers one reviewable result. Existing live owners retain their files; splitting a queue is not permission to send two writers into the same file. Exact interfaces, paths and negative criteria remain in the child ExecPlans. Independent read-only packet/review work runs now; source mutation follows its interface review and predecessor freeze. Root alone integrates checked commits. New eligible worker assignments use Haiku XHigh under the latest override above; do not create filler agents or interrupt existing Luna work before its current packet completes.

| Atomic task | Single result / boundary | Dependency and verification | Current disposition |
|---|---|---|---|
| 02C-settlement | Durable latest-progress→actual settlement/report/resume | Existing02B public status; stop/pause/goal precedence and stale exhaustion tests | Current Luna owner, goal diagnostic handoff pending |
| 02C-specialist | Legacy planner actionable judgment plus attached repair diagnostic | Actual strategy/atomization consumer, bounded transport/repair, no pathless continue | Same file owner; trace-order regression repair pending |
| 03U-selection | Pure web form and request-local startup/resume selection | Selected/recorded policy hashes, closed/inherit, DNS/environment drift, actual CLI/web consumers | Source review/final freeze pending; root82 fixtures pass |
| 04C-abort | Cancel-safe original-window UNKNOWN settlement | Same grant, bounded shielding, cleanup/settlement failure and idempotent retry | Existing Capability Luna owner authorized |
| 04C-observation | Read-only prepared-endpoint/owned physical closure observation | Complete inventory, HOST drift, foreign/legacy labels, absent/neverattached | Same Capability owner; no setup/adoption |
| 04C-reconciler | Original outstanding-grant→per-window settlement/release facts | Frozen Control read port and closure predicate, no actor-ready authority | Serial after Capability interface freeze |
| 04C-host | Locked original-instance recovery hook before actor CAS | Frozen reconciler; U assembly handoff; success and UNKNOWN actor-transfer tests | RuntimeDeps/driver/assembly handoff pending |
| 04R-projection | Verified operator-origin applicability projection | Actual deployment/provider-owned source and existing identity seam; no name/args heuristic | Read-only packet recon active, plan review pending |
| 04R-admission | Applicable HTTP-origin raw-route refusal before grant | Frozen projection, all transport same origin, sibling port independent | Not yet source-authorized |
| 04R-resume | Existing durable component binding includes restriction digest | Frozen projection and app owner handoff; fake/manual applicability persistence and drift refusal | Not yet source-authorized |
| 05-chain | Scripted common-worker→Control/gateway→original→next worker/Planner proof | Frozen04 interface; paging/discovery/approval exact invocation retained | Read-only packet builder active |
| 06A-consumer | Shared contradiction guard reaches concrete repair feedback | Existing frozen4-source/3-test package plus02C wrapper review | Root65 fixtures/type4 pass; consumer recheck pending |
| 06B-restore | Criterion-ID selection→exact original condition restoration | A/probe and C prompt/profile freeze, unknown/duplicate/cross-question/coverage negatives | Pending predecessor acceptance |
| 06C-task-CAS | Typed final source-row refusal→native completion feedback | Existing original proof/Task CAS, immediate-commit mutation and rollback | Current Luna repair owner |
| 06C-goal-CAS | Same typed refusal→actual held/report/resume diagnostic | Frozen carrier, serial02C central_run owner | Handoff pending; no duplicate writer |
| 06D-chain | Actual Task observation/review→graph reviewer receipt→Planner judgment | Producer/reuse freeze; actual verifier route, no fabricated graph status | Independent read-only packet requested |
| 07-P | Real immutable Kali/mock provider allow/deny/window evidence | Reviewed physical packet and03/04 accepted, exact owned resource scope | Independent Sol plan review active, no execution yet |
| 07-W | Real provider through scripted common-worker consuming chain | P+05+06 accepted; approval once, originals and criterion restoration | Pending physical/consumer prerequisites |
| 07-I | Original identity/status/count/unknown-usage acceptance | P/W authoritative artifacts, existing projections/ledgers | Pending actual proof |
| 08-review | Independent Sol plus agy final requirement/source review | All source frozen; concrete findings then owner-scoped repair | Pending integration |
| 08-finish | Risk-based final checks, full requirement audit, commit/push | Accepted reviews/repairs plus actual physical proof; no partial PASS | Goal active, completion unproven |

### 2026-10-15 parallel implementation handoff

Root revalidated the original Windows dev checkout (HEAD fdeb24f8), preserving all dirty and unrelated untracked files. The prior Linux session 50563 completed 56 hypothesis-planner/probe/canonical-state tests successfully; this is fixture evidence, not physical target execution. The completed agy review corroborates two first-start U defects: generated MCP server selection not propagated into supplied AppConfig, and a stale pre-startup session baseline rejecting legitimate preparation persistence. The repair must preserve genuine concurrent operator changes; broad metadata exclusions are not accepted.

Authoritative worker inventory found the former U/C handles absent and previous 06C/05 Luna handles terminal with 429. New Haiku High assignments are disjoint: coordinator children u_finish_haiku (startup/web/CLI), c_finish_haiku (settlement/legacy planner/profile), reuse_finish_haiku (06C reuse and completion excluding central_run), gate_finish_haiku (04C recovery), feedback05_haiku (new common-worker chain test), restriction04r_haiku (read-only projection packet). Root children plan7_diag_actual_tests_haiku (new actual diagnostic finalize/rollback integration test), plan7_06b_binding_tests_haiku (new real fingerprint/resume regression), and plan7_07_physical_harness_haiku (new isolated physical provider harness). No duplicate source ownership or new Luna is authorized unless actual Haiku 429 occurs. Each worker checkpoints before 100K context. New harness work does not establish physical proof before execution and prerequisites. Root retains Git/docs, caller/profile integration after handoff, final reviews and acceptance. No remote integration until the requested final pre-push stage.

### Remote review exception and source-review checkpoint

The user explicitly requested a single-worker review of recent teammate dev changes now, overriding only the earlier timing of remote inspection. plan7_teammate_dev_audit_haiku fetched origin/dev for inspection; no merge/pull/rebase or checkout change is authorized by that review. Observed remote HEAD38179561, merge-base376b479d; major recent f994b561 adds Kali/direct-target routing and context projection with extensive overlap. Full source-to-plan mapping is pending. New large overlapping expansions are held while existing bounded fixes/tests finish; actual integration remains a later explicit pre-push step.

Fresh root Windows checks: 06B changed consumer/profile fixtures22 PASS, binding/resume5 PASS, diagnostic canonical integration3 PASS; 05 common-worker fixture chain7 PASS; C settlement/legacy fixtures36 PASS and scoped Ruff6. Independent Sol06B source review CLEAR. Independent C review accepted two legacy defects despite those tests: nonempty registration omits cursor/progress, and separately committed necessity adjustments invalidate their own original snapshot before judgment. These require atomic registration/adjustment/judgment repairs plus focused duplicate/replan/rollback regressions before C acceptance. Passing fixture counts do not close those defects or the physical proof.

### Integrated teammate baseline (2026-10-15)

Merged the whole origin/dev38179561 history, including the docs-site migration and non-plan7 commits, after preserving local feature checkpoints. The direct provider and Kali preparation are the active default. investigation_context.py and planner_context.py match origin/dev byte-for-byte (git diff exit0); no local reimplementation. Conflict resolution retains current rich contradiction/admission diagnostics, finite progress/replan controls, original-source final CAS and atomic legacy progress alongside the teammate context readers. Team default profile15/atomizer prompt4 are retained; the real fingerprint includes the merged prompt bytes.

Concrete seam fixes: remove obsolete gate-policy keyword arguments from direct shell preparation, compose selected MCP IDs into supplied config, preserve applied-startup drift baseline, and retain completed target output with the existing execution_uncertain marker if detach fails. Gate recovery WIP remains in pre-merge Git history and is not activated. Two tests for removed gate-only APIs were removed from the active suite because their implementation was superseded by the explicit direct-provider decision; production direct admission and uncertainty tests replace that acceptance scope, not a silent skip of an active requirement.

Root checks on the merged worktree: all src Ruff PASS, all4623 tests collect without errors, direct/preparation31 PASS, unchanged-original reuse/CAS19 PASS, context lookup/admission diagnostics23 PASS, Linux startup/CLI/direct-preparation36 PASS. Worker merged atomization fixtures59 PASS. Actual Docker direct provider/gateway fixture passes with server reception and post-execution detach, but production assembly/Control consuming proof remains underway. Final review, three legacy atomic regressions, direct startup drift regressions, paid Cat Jam live test and final pre-push fetch/review are still required; no full-goal completion or push claimed.

### Final integration repairs and independent review dispositions

Merge commit bcd08ed3 includes the complete teammate history. Commit47745022 preserves structured atomization conflict diagnostics and direct execution typing. Actual legacy atomic regressions now cover same-wake duplicate suppression, cancel/replan in one transaction, and operator drift rejecting the entire batch; root related tests14 PASS. Source mypy429 modules PASS and direct/adapter regressions88 PASS at that checkpoint.

Sol independently found the native context query captured no graph roots. Commit c57da191 derives the worker's own target/source/dependency roots inside the existing read transaction and shares the bounded ancestry walk with the reader. This is a minimal concrete correction to the teammate context-saving baseline, not a replacement implementation. Root real-store context tests22 PASS; no live model is used in those fixtures.

Auxiliary Gemini review job1792035226578649100-f6eb1b2d55f16c1ffc26b3d9 completed. Root and Sol dispositions: accept cache-loss false detach (failed workroom removal can drop the cache while leaving the physical container); repair using the original room identity in the existing attachment. Reject the proposed closed-Run resume bypass: canonical selection binding rejects changed network/shell configuration before actual attachment. Missing Task execution, missing generated deployment metadata, and unknown target-port keys were not demonstrated in valid producer/validator paths; no speculative fallback or duplicate guard is added.

Startup regressions demonstrate legitimate preparation writes are accepted and concurrent operator writes remain rejected. A separate concrete caller defect was reproduced in offline validate: recorded deployment environment was not composed before config validation. Its repair must reuse verified artifacts in memory, preserve masking and leave artifact bytes/mtimes unchanged.

Actual OpenCode Cat Jam Free High structured preflight succeeded through the existing chat-completions adapter in12.62seconds. This establishes the selected model/protocol/effort call only; full bank analysis and production assembly/Control physical proof remain running. Full-suite execution, final lint/types and final pre-push remote reconciliation remain open. No whole-goal completion or push is claimed.

### Live bank result and full-suite reconciliation

Cat Jam Free High actual bank runs used temporary engagement copies, the existing local bank and request-local model settings; original engagement/browser settings were not edited. Run `run_9b1ceb99411c` ended planning_error after7 model requests,0 tools: the final correction budget exhausted on an empty judgment; an earlier rejection correctly identified `continue_requires_proposed_or_existing_work`. Run `run_86804aeb64c6` used10 model requests, executed2 actual GET probes, stored2 evidence records and2 graph mutations, then stopped at the required high-risk approval. No approval was invented or silently granted. These runs prove real model-to-bank execution and truthful approval waiting, not complete analysis, Kali execution, or guaranteed model contract compliance. Sanitized source summary: `%TEMP%/catjam-live-summary.json`.

Root full Linux regression run completed:4477 passed,132 skipped,25 failures in1789.64seconds. It started before the context-root fix and included stale direct/gate fixtures; those failures require current-state reruns, not blanket dismissal. Concrete groups: missing engagement_path in direct internal resume callers; legacy planner fixtures lacking bound run_configuration; removed gated_shell/bootstrap parameters; migration binding mismatch; CLI preparation stale keyword assertions; process/PTY timeouts. Owners have disjoint repair packets. Separate partition harness mistakenly lacked a multiprocessing main guard; its BrokenProcessPool results are harness-invalid, not product proof. No production process guard is weakened to accommodate that harness.

CI lint passes across src/tests/examples and both MCP example servers. Production mypy429 modules passed; full src/tests typing exposed test-only fixture mismatches, now assigned with real typed dependencies and optional narrowing rather than ignores. Compose contracts both passed. Final full integration proof remains open.

### Non-Kali requirement audit after integration

Fresh independent Sol review traced D01–D03, P01–P03 and K01–K04 through the current merged producer, consumer and storage boundaries. It found no concrete missing implementation or merge regression in those bounded requirements. D01 includes ConversationError→InputReceipt→CLI/WebFrontend→browser scalar projection; D02 includes separate transport/repair counters under the same total/deadline and metered request ledger; D03 preserves cancellation and timeout cause accounting. P01 checks executable continuation at proposal and canonical commit; P02/P03 preserve durable bounded replans, state projection and atomic judgment/cursor/wake. K01–K04 retain structured contradiction checks, original criterion restoration, observation/review/verdict linkage, original-source reuse CAS and typed safe feedback.

Root independently reran the current merged non-Kali checks: transport repair/conversation/completion diagnostic delivery25 PASS; central planner progress/replan disposition/legacy atomic progress/source reuse acceptance/web diagnostic projection30 PASS. Separate transport configuration/target binding/context-root tests33 PASS. These are real state/transaction and scripted-provider tests; they are not full live-analysis completion. Review does not close the outstanding integration suite failures or physical default-provider chain. Commits59ade704 ande3b83fd7 close the internal resume argument seam and typed transport/startup fixtures respectively.

### Concrete regression closure checkpoint

Commits1b83c3d1/4675882b adapt the dormant gate-ledger fixture to explicit selection and bind production run metadata before parallel stop;38 affected recovery/environment/preparation tests pass. Commit80672feb repairs the named historical migration adapter after exact hash comparison proved schema2 versus schema4 was the sole mismatch. Sol independently accepted the explicit source-identity projection, candidate schema3 and substantive drift remain rejected, root16 migration tests pass. Commitf8503ff2 repairs only a stale PTY test stub missing RuntimeDeps.tool_catalog; CLI/PTY9 tests pass. Commit920759e3 supplies real bound metadata and explicit held+resume conditions to legacy intermediate-planner fixtures; both previously failing target tests pass independently. Source guards and stop/approval/budget assertions are preserved.

A full integrated regression rerun is now active (root session59768, JUnit `/tmp/whs-final-integrated-20261015.xml`). No result is claimed before completion. The initial physical control-chain harness failed before approval because it did not bind its Task to the owned mock target; the new bounded owner is correcting the harness and proving the actual assembly/Control path. An additional narrow diagnostic audit is checking whether legacy StrategyDraft Pydantic errors lose their concrete validation rule in repair feedback; root observed generic $.questions rejection during the fixture investigation. Any confirmed production correction will require a separate commit and targeted test rather than being hidden as a fixture change.
