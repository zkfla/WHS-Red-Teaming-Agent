# Source Extraction Map

Batch A source freeze: 2026-09-07.

## Locked donor commits

| Donor | Branch | Commit | Workspace policy |
|---|---|---|---|
| CI/CD | `scenario/cicd` | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | read-only donor |
| Financial | `financial` | `83885a478b9151ab4be652caff6ba564dce8ee6e` | read-only donor |
| Mobile | `mobile` | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | read-only donor |

Implementation must stop and refresh this map if any donor HEAD changes.

## Classification legend

- `DIRECT_ADAPT`: mechanism is already largely domain-neutral and may be adapted with bounded edits.
- `REWRITE_AS_CORE`: behavior is valuable but concrete code is domain-coupled; preserve behavior, not implementation shape.
- `DOMAIN_ONLY`: keep out of generic core.
- `DISCARD`: migration/compatibility debt or source-specific orchestration not carried forward.

## CI/CD donor

| Source | Symbol / behavior | Class | Generic target / rule |
|---|---|---|---|
| `domain/models.py` | `SchemaModel`, `TriState` | DIRECT_ADAPT | `redteam_core.models` strict base and 3-valued truth |
| `domain/models.py` | `Evidence`, relation stable identity | REWRITE_AS_CORE | generic provenance/evidence/relation contracts |
| `domain/models.py` | `SecurityCandidate`, claim ceiling | REWRITE_AS_CORE | generic candidate contract; remove GHA enums/IDs |
| `domain/models.py` | `CoverageLedgerEntry` | REWRITE_AS_CORE | `knowledge/state.py::CoverageLedgerEntry` + Phase 6 candidate state |
| `domain/models.py` | `Workflow`, `Job`, `Step`, token/permission/event models | DOMAIN_ONLY | never enter core |
| `domain/evidence_requirement.py` | stable requirement identity, freshness | REWRITE_AS_CORE | typed generic requirements in Phase 4 |
| `domain/evidence_requirement.py` | mapping missing strings to GitHub requirements | DISCARD | new semantics emit typed requirement refs directly |
| `domain/investigation.py` | `KnowledgeState`, observation ledger | REWRITE_AS_CORE | generic knowledge/investigation state |
| `domain/investigation.py` | giant GitHub/AWS payload union | DOMAIN_ONLY | payloads registered by bundle-owned typed schemas |
| `framework/evidence_resolver.py` | resolver descriptors, failure taxonomy | REWRITE_AS_CORE | 0..N resolver runtime + deterministic routing |
| `framework/evidence_resolver.py` | exactly-one resolver per kind | DISCARD | removed in generic runtime |
| `framework/perception.py` | duplicate-observation idempotency, revision on semantic change | REWRITE_AS_CORE | atomic knowledge commit |
| `domain/graph_frontier.py` | bounded traversal, completeness, dominance/diversity | REWRITE_AS_CORE | `graph/frontier.py`; job/provider/source-name special cases removed |
| `domain/relation_semantics.py` | traversal/source/mechanism/sink/context semantics | REWRITE_AS_CORE | frozen `graph/semantics.py::RelationSemantics` registered per relation type |
| `domain/security_candidate.py` | root-cause identity/coverage concepts | REWRITE_AS_CORE | `models/security.py::RootCauseRef` + `investigation/coverage.py` |
| `domain/investigation_policy.py` | root representatives, family diversity, bounded batches | REWRITE_AS_CORE | generic score/portfolio/multi-batch coverage; donor impact tables excluded |
| `domain/security_candidate.py` | Finding/AttackPath -> Candidate normalization | DISCARD | candidate-first architecture |
| `framework/working_memory.py` | revision CAS, grounded refs, reconciliation | REWRITE_AS_CORE | `models/reasoning.py` + `reasoning/working_memory.py` |
| `framework/context_window.py` | deterministic history compaction, no factual LLM summarizer | REWRITE_AS_CORE | `reasoning/context_projection.py` |
| `framework/runtime_support.py` checkpoint/resume/journal | durable state, atomic replace, tail repair, safe resume | REWRITE_AS_CORE | `runtime/checkpoint.py`, `runtime/journal.py` |
| `framework/runtime_support.py::RunLease` | one active writer per run | REWRITE_AS_CORE | OS-lock-backed `runtime/lease.py` |
| GitHub/AWS resolvers/parsers/semantics | provider/domain behavior | DOMAIN_ONLY | future domain bundles/adapters only |

## Financial donor

| Source | Symbol / behavior | Class | Generic target / rule |
|---|---|---|---|
| `models/finding.py` | trusted vs LLM-inferred expectation provenance | REWRITE_AS_CORE | provenance ceiling / `ExpectationRef` |
| `models/state.py` | observed snapshot and missing-aware delta discipline | REWRITE_AS_CORE | later experiment/state seam; no money/account types in core |
| `knowledge/baseline.py` | measured normal behavior before judgment | REWRITE_AS_CORE | later `BaselineProvider` seam |
| `oracles/base.py` | deterministic outcome incl. `UNKNOWN` | DIRECT_ADAPT conceptually | generic validation outcome |
| `oracles/registry.py` | all registered oracles run; candidate cannot narrow set | REWRITE_AS_CORE | Phase 9 validator applicability authority |
| `engagement/compiler.py` | deterministic effective policy + policy hash | REWRITE_AS_CORE | Phase 10 policy snapshot |
| `engagement/policy.py` | final I/O-boundary enforcement | REWRITE_AS_CORE | Phase 10 effect boundary |
| `engagement/cleanup.py` | created-resource/cleanup ledger | REWRITE_AS_CORE | Phase 10 cleanup ledger |
| `agent/budget.py`, `agent/termination.py` | stop authority > LLM | REWRITE_AS_CORE | `runtime/budget.py`, `runtime/termination.py` |
| `agent/react.py::_finalize` | all terminal paths share cleanup/final manifest path | REWRITE_AS_CORE | `runtime/finalizer.py` |
| finance-specific operations, money/account/card semantics | concrete domain meaning | DOMAIN_ONLY | finance bundle only |
| `agent/react.py` giant dispatch / `run_probe` switches | orchestration monolith | DISCARD | do not copy |

## Mobile donor

| Source | Symbol / behavior | Class | Generic target / rule |
|---|---|---|---|
| `tools/agent_tools.py::_safe_path` | root escape prevention | REWRITE_AS_CORE | `acquisition/boundary.py::ArtifactBoundary` |
| `tools/agent_tools.py::list_files/search_text/read_file` | bounded acquisition | REWRITE_AS_CORE | `acquisition/operations.py` + explicit `AcquisitionBudget` |
| `tools/security_selector.py` | cheap keyword candidate ranking | REWRITE_AS_CORE conceptually | `AnalysisHint` + `rank_hints`; advisory priority only |
| `agent/mobile_agent.py` | preprocess candidates are hints; artifact text is untrusted | REWRITE_AS_CORE invariant | `UNTRUSTED_ARTIFACT` result marker + context trust channels |
| `tools/dynamic_tool.py` | static suspicion -> runtime check pattern | REWRITE_AS_CORE conceptually | requirement/resolver/observation seam |
| Android component/APK/JADX/ADB/logcat specifics | concrete domain/tool behavior | DOMAIN_ONLY | mobile bundle/adapter only |
| Gemini-specific orchestration | provider-specific | DISCARD | generic core does not depend on it |

## Batch A direct implementation scope

Batch A implements only the neutral foundation required by Phases 0-4:

1. strict canonical models and provenance/evidence boundaries,
2. typed bundle/schema registries and semantic manifest freezing,
3. generic evidence requirements and deterministic 0..N resolver selection,
4. observation ledger, freshness, conflict preservation, atomic knowledge revision.

Frontier, candidate coverage, acquisition, working memory, validator execution and controlled experiments remain later phases even when their foundational data contracts exist now.

## Batch B implemented extraction scope

Batch B implements Phases 5-6 from the locked CI/CD donor while preserving the donor only as a
behavioral reference:

1. `RelationSemantics` is a frozen bundle registration; the Frontier engine never compares concrete
   donor relation names.
2. bounded traversal, guard handling, context bridging, dominance, diversity and completeness
   metadata are rewritten in `redteam_core.graph.frontier`.
3. current CI/CD `Finding/AttackPath -> Candidate` normalization is deliberately **not** migrated.
   `CandidateDeriver` emits `SecurityCandidate` directly from canonical state.
4. root-cause grouping uses qualified `RootCauseRef` identity; one representative is selected per
   root before family-diverse bounded portfolio filling.
5. `CoverageLedgerEntry` preserves terminal accounting across candidate disappearance and supports
   repeated batches until every current root cause is terminal.
6. CandidateDeriver implementation fingerprints are part of the frozen semantic manifest so a
   semantic derivation change cannot silently reuse an old compatible manifest.

## Batch C implemented extraction scope

Batch C implements Phases 7-8 from the locked Mobile and CI/CD donors while strengthening the
trust boundary:

1. Mobile's module-global analysis root is **not** copied. `ArtifactBoundary` is immutable instance
   state and every path is resolved beneath that root.
2. inventory/search/read use explicit scanned-file, byte, single-file, result and output limits.
   Oversized search files are reported as skipped rather than silently becoming negative evidence.
3. artifact text/snippets are typed as `UNTRUSTED_ARTIFACT`; `AnalysisHint` has advisory-only
   semantics and acquisition APIs cannot accept hints as hard-scope filters.
4. CI/CD WorkingMemory CAS/grounding/reconciliation is rewritten against generic Candidate/Relation
   state. Model patches cannot directly write Evidence/missing/requirement IDs; core derives them
   from canonical grounding.
5. WorkingMemory is capped at eight hypotheses and rejects stale/unknown/over-limit patches.
   reconciliation cannot mutate authoritative Knowledge/Relation/Candidate state.
6. context projection is deterministic and bounded, separates six trust channels, exposes only
   canonical structured metadata by default, and omits raw Observation payload JSON.
7. history compaction preserves recent protocol structure without an LLM factual summarizer;
   irreducible oversized history fails closed instead of passing oversized input through.

## Batch D implemented extraction scope

Batch D implements Phases 9-10 from the locked CI/CD and Financial donors without copying either
domain's concrete claim vocabulary or operation catalog:

1. CI/CD claim-ceiling/grounding authority and Financial run-all oracle semantics are rewritten as a
   generic manifest-owned `Validator` subsystem. Candidate/LLM cannot choose an allowlist.
2. all validator outcomes remain canonical; representative reporting cannot delete PASS/UNKNOWN or
   concurrent violations. Finding materialization is a downstream 0..N projection.
3. validator execution receives deep canonical snapshots and optional experiment results must already
   exist in the canonical execution ledger.
4. Financial deterministic engagement compilation becomes immutable `ExecutionPolicySnapshot` with
   approval provenance, effective hash, typed registered domain constraints and active-policy binding.
5. Financial final request-boundary enforcement becomes a generic `ExecutionPermit` effect boundary.
   scope/effect/capability/concurrency/budget/cleanup/constraints are rechecked immediately before the
   registered executor is invoked.
6. executor and policy-evaluator implementation/applicability registrations participate in the frozen
   semantic manifest so old authorization cannot silently cross changed execution semantics.
7. Financial candidate→experiment separation becomes semantic `ExperimentDescriptor` + operational
   `ExperimentAttempt`; STARTED ambiguity is explicitly `UNCERTAIN` and unsafe automatic replay is
   rejected.
8. Financial cleanup tracking is generalized to CreatedResource→CleanupRequirement→CleanupAttempt→
   CleanupOutcome; missing cleanup implementation is terminal `NOT_CONFIGURED`, not silent success.

## Batch E implemented extraction scope

Batch E implements Phase 11 from the locked CI/CD and Financial donors, plus generic durability
strengthening required by the current plan:

1. CI/CD checkpoint/journal behavior is rewritten as atomic canonical checkpoints plus a fsync'd,
   hash-chained append-only runtime journal. Only a torn final JSON line is repaired; earlier or
   semantic corruption fails closed.
2. CI/CD safe-resume identity is strengthened into `RuntimeCompositionFingerprint`, which binds the
   frozen semantic manifest together with resolver descriptors, routing policy and priority-provider
   identity. A changed runtime composition cannot silently resume old state.
3. CI/CD single-run ownership is rewritten as an OS advisory `RunLease`, avoiding guessed stale-PID
   deletion while still preventing two processes from writing one runtime concurrently.
4. Phase 10 experiment state is made crash durable: STARTED state is checkpointed before the durable
   effect-start marker and external call; uncertain post-start cuts recover to `UNCERTAIN` rather than
   being replayed or invented as FAILED.
5. Financial budget/termination authority becomes generic monotonic runtime usage + deterministic
   termination ordering where policy/budget outrank LLM finish.
6. Financial `_finalize()`/cleanup behavior becomes one generic finalizer used by normal, policy,
   budget, LLM-finish, handled-external-failure and internal-error terminal paths. Cleanup uncertainty,
   failure and missing implementation remain visible.
7. `DomainStateEnvelope` + manifest-owned `DomainStateCodecRegistration` persist heterogeneous domain
   state without a `Web|Mobile|Finance` union. Core validates bundle/codec/schema/hash binding and never
   interprets domain payload keys.
8. Observation/Knowledge and Candidate/Coverage before/after checkpoint cuts are regression-tested for
   idempotent authoritative replay. Runtime crash tests are repeated under multiple `PYTHONHASHSEED`
   values to guard canonical serialization determinism.

## Batch F implemented reference / conformance scope

Batch F completes Phases 12-14:

1. Financial expectation provenance is promoted from a model-only contract to typed `ExpectationState`
   ledger input consumed by validators. Validation snapshot identity includes expectation revision.
2. A designated EvidenceStoragePolicy boundary now gates raw artifact retention, export and LLM exposure;
   the raw backend itself remains adapter-owned.
3. `static_graph`, `stateful_experiment`, `artifact_runtime`, and `request_response` synthetic reference
   bundles exercise graph/path, pathless state/invariant, bounded static→runtime and request/authorization
   shapes without importing donor packages or concrete domain models into core.
4. `refintegration` proves cross-bundle composition through declared dependencies and canonical refs,
   without sibling private DomainState imports.
5. The final acceptance-frozen `redteam_core` source SHA is
   `53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c`; Batch-F reference tests assert
   this exact tree to catch reference-specific core edits.
6. Locked donor mandatory conformance was re-run at the commits above: CI/CD 94 PASS, Financial 87 PASS,
   Mobile `MOBILE_CHARACTERIZATION_PASS`; donor worktrees remained clean.

The extraction plan's Generic Core scope is complete through Phase 14. Concrete domain adapters remain
`DOMAIN_ONLY`/future bundle work and are not silently promoted into the core.
