# Batch C Implementation Receipt — Phase 7~8

Date: 2026-09-08

## Result

`COMPLETE` for Batch C as defined in `GENERIC_CORE_REFACTOR_PLAN.md`, subject to the final hash
fields recorded at the end of this receipt.

Batch C implements Phase 7 (bounded artifact acquisition) and Phase 8 (WorkingMemory + bounded LLM
context). It does **not** claim that Phase 9 validator/oracle execution, Phase 10 controlled
experiments/effect-boundary policy, or later durable runtime/checkpoint/reference-domain work is
implemented.

## Locked donor commits

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by Batch C.

## Phase 7 — Bounded acquisition

Primary files:

```text
src/redteam_core/acquisition/models.py
src/redteam_core/acquisition/boundary.py
src/redteam_core/acquisition/operations.py
src/redteam_core/acquisition/hints.py
src/redteam_core/acquisition/__init__.py
tests/unit/test_acquisition.py
```

Implemented:

- immutable per-instance `ArtifactBoundary`; no module-global analysis root,
- strict relative-path resolution beneath one configured root,
- parent/absolute path escape rejection,
- directory symlink/junction escape rejection,
- deterministic bounded artifact inventory,
- bounded text search and bounded text reads,
- explicit scanned-file and total-byte budgets,
- explicit max-single-file, result-count, output-character and snippet limits,
- stable-size read check to fail closed if an artifact changes during read,
- direct oversize read rejection,
- search-time oversize skip with explicit `skipped_oversize_files` accounting,
- explicit truncation metadata,
- artifact text/snippets marked `UNTRUSTED_ARTIFACT`,
- `AnalysisHint` is structurally advisory-only,
- deterministic hint ranking,
- acquisition operation APIs expose no hint-driven hard-scope parameter.

The Mobile donor's useful confinement/bounding behavior is preserved without copying Android, APK,
JADX, ADB, global mutable configuration, or provider-specific orchestration into core.

## Phase 8 — WorkingMemory / bounded context

Primary files:

```text
src/redteam_core/models/reasoning.py
src/redteam_core/reasoning/working_memory.py
src/redteam_core/reasoning/context_projection.py
src/redteam_core/reasoning/__init__.py
src/redteam_core/security/redaction.py
src/redteam_core/knowledge/state.py
tests/unit/test_working_memory.py
tests/unit/test_context_projection.py
```

Implemented:

- revisioned `AgentWorkingMemory`,
- maximum eight hypotheses,
- bounded relation/evidence/missing/requirement/grounding reference sets,
- 12,000-character serialized WorkingMemory ceiling on patch/reconcile paths,
- optimistic CAS via `WorkingMemoryPatch.expected_revision`,
- stale patch rejection,
- semantic no-op update with no revision increment,
- stable hypothesis identity from an immutable semantic key,
- canonical Candidate/Relation grounding validation,
- candidate-bound relation-scope validation,
- FALSE support rejection,
- model patch schema has no authoritative `evidence_ids`, missing-reason or requirement write field,
- evidence/missing/requirement refs are re-derived by core from canonical Candidate/Relation state,
- deterministic reconcile after canonical recompute,
- READY/BLOCKED/DISCARDED transitions from canonical state,
- disappeared/rejected/merged candidates and stale/contradicted relations are reconciled fail-closed,
- reconciliation-marker-only updates do not create spurious WorkingMemory revisions,
- WorkingMemory code cannot assign authoritative Knowledge/Relation/Candidate state,
- deterministic best-effort redaction for non-authoritative claim/summary/next-check text,
- bounded deterministic decision-context projection,
- six explicit context channels:
  `SYSTEM_POLICY`, `EXECUTION_POLICY`, `TRUSTED_OBJECTIVE`, `CANONICAL_STATE`,
  `UNTRUSTED_ARTIFACT`, `LLM_WORKING_TEXT`,
- trusted objective is marked focus-only and cannot override policy,
- active Candidate portfolio refs are revalidated against canonical Candidate state,
- bounded budget/capability/coverage information can be projected,
- recent Observation projection carries metadata/hash/refs but not raw payload JSON,
- raw artifact content is not injected into model context by default,
- history compaction is deterministic and uses no LLM factual summarizer,
- oversized tool output is replaced with bounded hash/guidance metadata,
- irreducible oversized history raises `ContextBudgetExceeded` instead of returning oversized input.

## Batch C integration proof

`tests/integration/test_batch_c_spine.py` exercises:

```text
bounded artifact acquisition
→ domain/frontend-owned canonical Relation
→ WorkingMemory patch grounded by Relation ID
→ evidence refs derived from canonical Relation
→ bounded trust-separated decision context
```

The test deliberately does not make raw artifact text authoritative. A domain frontend must first
produce canonical semantic state before WorkingMemory can use that grounding.

## Generic release gates

Commands:

```text
uv run ruff check src tests
uv run pytest -ra -o addopts=''
uv run python -m compileall -q src
uv build
```

Result before final receipt hashing:

```text
ruff:       PASS
pytest:     102 passed
compileall: PASS
build:      source distribution + wheel PASS
```

Batch C regression coverage includes:

- parent/absolute/junction artifact escape rejection,
- deterministic inventory and explicit result truncation,
- direct oversize read rejection and search oversize skip/report,
- scanned-file/byte budget exhaustion,
- explicit zero-limit rejection,
- untrusted-artifact typing,
- advisory-only hints and architecture no-hard-filter gate,
- stale WorkingMemory CAS rejection,
- unknown/unrelated grounding rejection,
- direct authoritative evidence-ID injection rejection,
- WorkingMemory authoritative-state immutability,
- eight-hypothesis accumulation bound,
- semantic no-op/reconcile-marker revision stability,
- canonical READY/BLOCKED/DISCARDED reconciliation,
- free-text redaction,
- deterministic character-bounded context projection,
- explicit six-channel trust separation,
- Observation raw-payload exclusion,
- portfolio ref validation,
- tool-protocol-preserving history compaction,
- irreducible history fail-closed behavior.

## Donor conformance regression

### CI/CD

The locked CI/CD donor's relevant WorkingMemory/context suites were re-run:

```text
tests/unit/test_working_memory.py
tests/unit/test_context_window.py

13 passed
```

### Mobile

The locked Mobile donor currently has no `tests/` directory. The donor-read-only characterization
used for the Phase 0 baseline was re-run against the current locked HEAD and returned:

```text
MOBILE_CHARACTERIZATION_PASS
```

It rechecked root escape rejection, list/search result clamps, >2 MB search-file skipping and read
truncation behavior.

## Architecture / trust gates

Current architecture tests additionally verify:

- donor packages are not imported by `redteam_core`,
- donor-specific domain vocabulary remains outside core/application source,
- semantic code has no hidden wall-clock call outside the explicit clock adapter,
- project dependencies remain domain-neutral,
- acquisition operations cannot consume AnalysisHint as a hard scope,
- WorkingMemory cannot assign `session.knowledge`, `session.semantic` or `session.candidate_state`,
- Candidate derivation remains Finding-independent,
- generic Frontier remains relation-vocabulary independent.

## Deterministic receipts

Filled after final document synchronization and final release gates:

```text
src+tests tree SHA256: 900cc5c3987bd39719ca0a57c14d493db6cacf4431f182b8384c73cf983aa8af
GENERIC_CORE_REFACTOR_PLAN.md SHA256: 55ed5e48cfc0c9a5b39294585382ae30bd98bb40784e0fa228bbef66fd71ea2d
```

## Known intentionally-unimplemented work

Batch C does not implement:

- Phase 9 validator/oracle applicability/execution authority,
- Phase 10 controlled experiment, execution policy/effect boundary and cleanup ledger,
- Phase 11 durable checkpoint/resume runtime,
- final heterogeneous reference-domain and composite proofs,
- a concrete Mobile ADB/runtime Observation adapter.

The generic runtime Observation seam already exists from Batch A, but a domain-specific Mobile
runtime provider remains domain/adaptor work and is not falsely counted as Batch C completion.
