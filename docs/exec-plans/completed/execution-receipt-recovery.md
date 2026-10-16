# Execution receipt recovery and bounded feedback

Status: COMPLETE for scoped implementation; compatibility limit recorded below

## Goal and constraints

Implement the audit's reproducible recovery and feedback defects on isolated branch
`codex/execution-receipt-recovery`, based on `844dec4f`. The original checkout and live
fixtures must remain untouched. No target, model, approval, or live Docker calls.
No integration or push. Offline fixture tests are not live validation.

## Facts and unknowns

Gateway cancellation loses compare's local first result; Kali removes protected raw
spools after projection. Unknown executions occupy global capacity. Uncertain results
must never become successful observations. Arbitrary HTTP GET does not establish no
target resource effects. Repair previews truncate and omit durable cause fields.
Provider process/route termination must be proved separately from response outcome.

## Task DAG and ownership

- T1 Kali safe receipt persistence/recovery: Luna worker, capability and assembly.
- T2 verified termination capacity: root, shared contracts/journal/central state; depends T1.
- T3 provider uncertainty: Luna worker, network/MCP adapter; parallel.
- T4 recording feedback and reviewed identity: Luna worker, graph recording; parallel.
- T5 bounded repair diagnostics: Luna worker, cognition/request ledger; parallel.
- T6 unchanged inventory progress: Luna investigation/implementation, existing progress;
  no name-specific central exceptions.
- T7 independent review, repair and relevant regression suites after implementation.

## Strategy and acceptance

Reuse evidence store, execution recovery port, resource fences and request ledger.
No second scheduler or evidence source. Preserve safe ordinal originals before loss,
never replay an admitted ordinal. Verified termination can return capacity while the
unknown result, resource hold and goal completion block remain. Default historical
records remain conservative. Capture fixed diagnostic code/location/rules without prose.

## Validation

Isolated local virtual environment created with current pyproject dependencies.
Test cancellation/restart/identity tamper, capacity=1, same resource denial, old records,
target-free independent work, provider transport uncertainty, graph input feedback,
receipt revision and repair classification idempotence. Run relevant Ruff and typing.

## Progress / decisions / review

- Managed worktree created and independent environment installed (Python 3.10.4).
- Resource effects for arbitrary-path HTTP remain unknown unless provider can prove them.
- Termination proof is separate from resolution: no automatic UNKNOWN-to-failed conversion.
- Kali ordinal admission and safe receipt records survive cancellation. Raw streams are
  removed after projection; retained protected supervisor records establish termination,
  never a reconstructed successful ToolResult. Missing/corrupt/duplicate markers fail closed.
- A verified termination receipt releases only global execution capacity. UNKNOWN,
  resource fences, replay rejection and goal completion holds remain. Historical records
  without recovery references remain conservative. Reconciliation is idempotent after reopen.
- Recovery queries use the preserved original invocation deadline, not the shorter health
  check deadline. An uncertain original triggers one bounded reconciliation attempt.
- Explicit MCP result metadata carries provider uncertainty; output diagnostic DATA is not
  interpreted as authority. Independent review found this distinction missing initially;
  the correction and its negative test resolved that finding.
- Graph recording returns schema-depth feedback and the exact reviewed candidate revision
  and purpose hash. Native tests assert submission/result identity and receipt-bound Finding
  promotion only after confirmation, replacing stale pre-v2 expectations.
- Planner repair diagnostics persist bounded fixed codes, locations and rules with request
  links. No submitted prose or unknown field values are recorded in these diagnostics.
- Exact repeated same-Task originals report made_progress=false; changed inputs/results
  remain eligible. Cross-Task inventory reuse is planner guidance, not a deterministic gate.
  Planner request contract version is 7; existing fingerprint validation remains in force.
- Independent fixture review rejected a blanket capacity-blocked planning guard: after the
  UNKNOWN execution, new results, graph changes and task events continued. The planner
  cursor was 434 while a relevant result update reached 474. No unchanged-input bug was
  established; withholding planning solely for no new tool start would skip valid review.
- Independent reviews found no remaining confirmed P1/P2 in scoped Kali recovery or the
  corrected MCP metadata path. Additional negative recovery tests cover ownership failure,
  malformed/duplicate markers, live/failed process scans and ambiguous append acknowledgement.

## Validation results and limits

- Final combined affected offline suite on Python 3.14.6: 236 passed in 124.16s.
- Combined affected offline suite on Python 3.10.4: 235 passed, 1 failed (236 collected).
- The failure is unchanged test_client_source_requires_complete_http_framing[short-chunked]:
  expected partial body abc, received empty. Base 844dec4f and current _CLIENT_SOURCE are
  identical; both reproduced empty body 5/5 under this worktree's Python 3.10.4.
  Its http.client._read_chunked discards the nested IncompleteRead partial bytes. Parent
  confirmed the original environment is Python 3.14.6. Its independent run against the
  isolated source passed all 46 Kali/termination/reconciliation tests. The 3.10.4
  compatibility failure remains recorded; the test was not weakened and the client
  source was not changed. The user explicitly authorized using that 3.14.6 interpreter;
  PYTHONPATH was set to this worktree/src and the imported module path was verified.
- Additional worker checks: shell recovery/evidence 50 passed; bootstrap wiring 3 passed.
- Ruff across the CI source/test/example/server paths passed. git diff --check passed.
- Native Windows mypy reports existing POSIX-only symbols unavailable on Windows.
  After fixing new type annotation/narrowing issues, CI-targeted
  `mypy --platform linux src/redteam tests` passed all 919 source files.
- Tests use fixtures/fakes explicitly. No live target, model, approval or Docker calls.
- No automatic recovery of historical missing references, successful authentication,
  complete HTTP outcome, or live Finding is claimed. No hard cross-Task deduplication or
  blanket planning suppression was added. Original checkout and fixtures were not changed.

## Completion criteria

Each independent change has regression evidence, a review disposition, an atomic commit,
and explicit remaining limits. Source-only success never claims authenticated live success.

## Combined regression selection

Run from this isolated checkout with LITELLM_LOCAL_MODEL_COST_MAP=True. For the
explicitly requested Python 3.14.6 run, PYTHONPATH points to this checkout's src;
the imported kali_http module path was printed and verified before pytest.

```text
tests/unit/test_execution_termination.py
tests/unit/test_termination_reconciliation.py
tests/unit/test_execution_recovery_gateway.py
tests/unit/test_central_state.py
tests/integration/test_central_recovery.py
tests/integration/test_central_resume_independent_work.py
tests/unit/test_finance_network.py
tests/unit/test_mcp_adapter.py
tests/unit/test_mcp_session.py
tests/unit/test_web_http_request_metadata.py
tests/unit/test_kali_http.py
tests/unit/test_orchestration_diagnostics.py
tests/unit/test_shell_evidence_004a.py
tests/unit/test_planner_request_contract.py
tests/unit/test_web_graph.py
tests/unit/test_native_tool_progress.py
tests/unit/test_planner_repair_diagnostic_persistence.py
tests/integration/test_native_tool_progress.py
tests/integration/test_probe_graph_feedback_native.py
tests/integration/test_probe_graph_record_feedback.py
```
