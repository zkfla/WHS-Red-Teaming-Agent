# Generic Core Plan Index

The authoritative implementation plan is:

`docs/GENERIC_CORE_REFACTOR_PLAN.md`

Supporting migration contracts:

- `docs/SOURCE_EXTRACTION_MAP.md`
- `docs/SOURCE_CONFORMANCE_MATRIX.md`
- `docs/SOURCE_BASELINE_RECEIPT.md`
- `docs/BATCH_A_IMPLEMENTATION_RECEIPT.md`
- `docs/BATCH_B_IMPLEMENTATION_RECEIPT.md`
- `docs/BATCH_C_IMPLEMENTATION_RECEIPT.md`
- `docs/BATCH_D_IMPLEMENTATION_RECEIPT.md`
- `docs/BATCH_E_IMPLEMENTATION_RECEIPT.md`
- `docs/BATCH_F_IMPLEMENTATION_RECEIPT.md`

Do not reintroduce the previous CI/CD-specific plan into this workspace. Donor repositories remain
independent sources used for conformance, not packages imported by `redteam_core`.

## Follow-up: domain-neutral performance and investigation efficiency

The completed Phase 0-14 plan above remains the historical implementation baseline.
The active follow-up improvement plan is:

- [Generic Core performance evolution plan](docs/GENERIC_CORE_PERFORMANCE_EVOLUTION_PLAN.md)
- [Plan self-review and revision log](docs/GENERIC_CORE_PERFORMANCE_PLAN_REVIEW.md)
- [G0 baseline and progress receipt](docs/GENERIC_CORE_PERFORMANCE_BASELINE_RECEIPT.md)
- [G0-C1 correctness proposal](docs/GENERIC_CORE_G0_CORRECTNESS_PROPOSAL.md)
- [G6 focused decision context receipt](docs/GENERIC_CORE_G6_RECEIPT.md)

This follow-up excludes actual domain integration, including CI/CD. The user requested
implementation on 2026-09-08. G0/G1/G2 are PASSED_OFFLINE. The independently reproduced
Frontier correctness issue was fixed and verified separately. G1/G2 optimizations remain
default-off after final release evaluation. G3 durable operational budget is PASSED_OFFLINE
(963 full-suite tests, Ruff, compileall, sdist and wheel passed);
G4 is PASSED_OFFLINE with durable receipts, atomic replay commit, bounded batch,
single-flight and explicit safe fallback. Concurrency remains serial-by-default because the
paired 30-sample p50 improvement was 4.82%, below the predeclared 20% gate. G6 focused
candidate evidence bundles and G7 are PASSED_OFFLINE (see
`docs/GENERIC_CORE_G7_RECEIPT.md`: 1,077 semantic tests plus 10 source/reference checks).
G8 is PASSED_OFFLINE (`docs/GENERIC_CORE_G8_RECEIPT.md`: 1,118 semantic tests,
10 post-approval source/reference checks); utility default enable is withheld
because two fixed-budget quality fixtures regress. G9 is PASSED_OFFLINE
(`docs/GENERIC_CORE_G9_RECEIPT.md`: final core `7293c3e3...9945ce5`, all 1,166
collected tests passed on the approved tree), and G5 is
DEFERRED because its profiling trigger was not established. C1-C4 are likewise
DEFERRED under their declared conditions.
Use section 13 of the active plan and the latest G-stage receipts for current evidence.
The self-review score is not implementation acceptance.
No commit, push, or remote execution is included.
