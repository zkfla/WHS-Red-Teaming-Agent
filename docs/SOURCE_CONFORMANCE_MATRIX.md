# Source Conformance Matrix

This matrix is the behavioral migration contract. It does not require code identity with donors.

| ID | Donor behavior to preserve | Source evidence | Batch / target proof | Status |
|---|---|---|---|---|
| C-01 | strict canonical models reject unknown fields | CI/CD `SchemaModel(extra="forbid")` | Batch A model tests | **IMPLEMENTED** |
| C-02 | truth supports `TRUE/FALSE/UNKNOWN` | CI/CD `TriState` | Batch A model tests | **IMPLEMENTED** |
| C-03 | semantic IDs are deterministic from canonical identity | CI/CD stable relation/requirement IDs | Batch A stable-ID tests | **IMPLEMENTED** |
| C-04 | duplicate exact observation is idempotent | CI/CD `perception.apply_observation` | Batch A knowledge tests | **IMPLEMENTED** |
| C-05 | semantic revision changes only when semantic knowledge changes | CI/CD `KnowledgeState.revision` | Batch A knowledge tests | **IMPLEMENTED** |
| C-06 | resolver failure taxonomy does not become affirmative/negative security truth | CI/CD `EvidenceResolutionError` | Batch A resolver tests | **IMPLEMENTED** |
| C-07 | external evidence is typed and requirement-bound | CI/CD typed payload union + requirement IDs | Batch A registry/resolver tests | **IMPLEMENTED** |
| C-08 | multiple independent observations must not be silently last-write-wins | generic safety strengthening over CI/CD | Batch A conflict tests | **IMPLEMENTED** |
| C-09 | missing or stale evidence remains unresolved rather than fabricated | CI/CD UNKNOWN/freshness discipline | Batch A freshness/snapshot/expiry tests | **IMPLEMENTED** |
| C-10 | candidate static claim cannot exceed its evidence ceiling | CI/CD candidate/claim validators | Batch A foundational candidate tests | **IMPLEMENTED** |
| C-11 | Frontier traversal is bounded and reports complete/truncated/depth-limited states separately | CI/CD `graph_frontier.py` | `test_generic_frontier.py` bounded/depth tests | **IMPLEMENTED / BATCH B** |
| C-12 | traversal/source/mechanism/sink/guard/context meaning is data-driven rather than relation-name hardcoding | CI/CD `relation_semantics.py` + `graph_frontier.py` | registered frozen `RelationSemantics` + architecture literal gate | **IMPLEMENTED / BATCH B** |
| C-13 | hard guards block, uncertain guards cap certainty, and unrelated contexts do not cross without an explicit bridge | CI/CD Frontier guard/context handling | guard/context/bridge Frontier tests | **IMPLEMENTED / BATCH B** |
| C-14 | redundant paths are dominance-pruned while bounded results retain semantic diversity | CI/CD `_diverse_rank` + dominance ledger | explicit dominance + source-bucket diversity tests | **IMPLEMENTED / BATCH B** |
| C-15 | unregistered relation meaning is fail-closed and non-traversable | generic strengthening over donor string fallback | Frontier unregistered-relation test | **IMPLEMENTED / BATCH B** |
| C-16 | canonical Candidate exists before Finding/materialization | CI/CD migration debt in `security_candidate.py` | AST architecture gate + direct CandidateDeriver test | **IMPLEMENTED / BATCH B** |
| C-17 | root-cause identity, representative selection and family diversity prevent one family from crowding out others | CI/CD `investigation_policy.py` | qualified `RootCauseRef` + family-diversity portfolio test | **IMPLEMENTED / BATCH B** |
| C-18 | candidate sets larger than one portfolio rotate through later batches until all roots are terminal | latest CI/CD multi-batch coverage | 9 roots / limit 3 terminal-rotation test | **IMPLEMENTED / BATCH B** |
| C-19 | candidate disappearance after semantic recompute does not erase prior investigation accounting | CI/CD `reconcile_candidate_state` | inactive `BLOCKED_EXTERNAL` history test | **IMPLEMENTED / BATCH B** |
| C-20 | stale candidate/knowledge portfolios and unrecorded/duplicate batch commits fail closed | generic concurrency/durability strengthening | portfolio revision/batch-binding tests | **IMPLEMENTED / BATCH B** |
| C-21 | CandidateDeriver semantics participate in compatibility identity | semantic-manifest requirement from plan | static registration fingerprint + runtime exact-set tests | **IMPLEMENTED / BATCH B** |
| C-22 | WorkingMemory updates are optimistic-CAS and stale revisions fail closed | CI/CD `framework/working_memory.py` | stale-patch + semantic no-op tests | **IMPLEMENTED / BATCH C** |
| C-23 | WorkingMemory grounding is canonical and model text cannot inject authoritative evidence IDs | CI/CD grounded memory + generic trust strengthening | strict upsert schema + canonical relation/candidate derivation tests | **IMPLEMENTED / BATCH C** |
| C-24 | hypotheses reconcile deterministically after canonical recompute | CI/CD `reconcile_working_memory` | READY/BLOCKED/DISCARDED + disappeared-candidate tests | **IMPLEMENTED / BATCH C** |
| C-25 | LLM decision context is deterministic/bounded and history compaction uses no factual LLM summarizer | CI/CD `context_window.py` | character-bound projection + tool-hash compaction + irreducible fail-closed tests | **IMPLEMENTED / BATCH C** |
| C-26 | WorkingMemory cannot mutate authoritative Knowledge/Relation/Candidate state | generic authority strengthening | snapshot + source architecture gates | **IMPLEMENTED / BATCH C** |
| C-27 | model-visible context separates trust channels and free working text is redacted/bounded | plan §14 trust boundary | six-channel projection + redaction/raw-payload exclusion tests | **IMPLEMENTED / BATCH C** |
| C-28 | checkpoint replacement is atomic and corrupt prior state cannot be silently overwritten | CI/CD `CheckpointWriter` / `load_checkpoint` | atomic replace + tamper/write-rejection tests | **IMPLEMENTED / BATCH E** |
| C-29 | operational event history is append-only, ordered and corruption fails closed | CI/CD `EventJournal` | hash-chain, torn-tail repair and corruption tests | **IMPLEMENTED / BATCH E** |
| C-30 | crash after an external effect may have started never becomes invented terminal truth or automatic replay | CI/CD resume discipline + generic P10 attempt lifecycle | durable effect-start cut → `UNCERTAIN` recovery tests | **IMPLEMENTED / BATCH E** |
| C-31 | resume compatibility binds semantic implementation and runtime resolver-routing identity | CI/CD checkpoint compatibility strengthened by plan §16.2 | semantic/routing composition fingerprint mismatch tests | **IMPLEMENTED / BATCH E** |
| C-32 | one runtime directory has at most one process-level writer | CI/CD `RunLease` | OS advisory single-writer lease test | **IMPLEMENTED / BATCH E** |
| C-33 | heterogeneous domain state is checkpointed without core-owned domain unions or key interpretation | plan §16.1 `DomainStateEnvelope` / `DomainStateCodec` | manifest-bound opaque codec round-trip + architecture gate | **IMPLEMENTED / BATCH E** |
| F-01 | LLM-inferred expected state is not trusted judgment evidence | Financial `ExpectedDelta.is_trusted` | typed ExpectationState + Batch-F stateful validator: inferred-only baseline → UNKNOWN, authoritative baseline consumed separately | **IMPLEMENTED / BATCH F** |
| F-02 | missing/untrusted normative material produces UNKNOWN rather than invented judgment | Financial state/oracle tests | Batch-F `stateful_experiment`: no authoritative baseline → validator UNKNOWN | **IMPLEMENTED / BATCH F** |
| F-03 | all registered validators/oracles are system-owned, not candidate-selected | Financial `oracles/registry.py` | manifest/runtime exact-set + run-all validation tests | **IMPLEMENTED / BATCH D** |
| F-04 | execution policy is deterministic and hashed | Financial `engagement/compiler.py` | deterministic immutable snapshot/hash tests | **IMPLEMENTED / BATCH D** |
| F-05 | final side-effect boundary rechecks policy | Financial `engagement/policy.py` | active policy + permit + final scope/effect/capability/concurrency/budget tests | **IMPLEMENTED / BATCH D** |
| F-06 | cleanup failure/not-configured is explicit | Financial `engagement/cleanup.py` | cleanup requirement/attempt/outcome + NOT_CONFIGURED tests | **IMPLEMENTED / BATCH D** |
| F-07 | policy/budget termination authority outranks LLM finish and terminal reasons remain explicit | Financial `agent/termination.py`, `agent/budget.py` | deterministic termination precedence + monotonic usage tests | **IMPLEMENTED / BATCH E** |
| F-08 | normal/error/policy/budget/LLM terminal paths share finalization and cleanup uncertainty stays visible | Financial `agent/react.py::_finalize`, `engagement/cleanup.py` | all-terminal-kind finalizer + cleanup crash/UNCERTAIN tests | **IMPLEMENTED / BATCH E** |
| M-01 | analysis path cannot escape configured artifact root | Mobile `_safe_path` | `ArtifactBoundary` escape/junction tests | **IMPLEMENTED / BATCH C** |
| M-02 | file listing/search/read are bounded | Mobile `agent_tools.py` | acquisition limit/budget tests | **IMPLEMENTED / BATCH C** |
| M-03 | oversized search input files are skipped | Mobile `search_text` | explicit skipped-oversize result test | **IMPLEMENTED / BATCH C** |
| M-04 | preprocessing/keyword candidates are hints, not hard scope | Mobile system prompt + selector | advisory `AnalysisHint` + architecture no-filter gate | **IMPLEMENTED / BATCH C** |
| M-05 | artifact natural language is untrusted data | Mobile system prompt | typed untrusted result + six-channel context tests | **IMPLEMENTED / BATCH C** |
| M-06 | runtime verification is distinct from static suspicion | Mobile dynamic tools + prompt | Batch-F `artifact_runtime`: static UNKNOWN → fake runtime Observation → deterministic recompute; donor dynamic contract re-characterized | **IMPLEMENTED / BATCH F GENERIC CONFORMANCE** |
| C-34 | four heterogeneous reference domains must attach without reference-specific core edits | Phase 12 exit gate | frozen core-source SHA gate + `test_batch_f_reference_domains.py` | **IMPLEMENTED / BATCH F** |
| C-35 | composition/registration order cannot change semantic manifest or remaining bundle semantics | Phase 13 / canonical bundle ordering | normal/reverse compose + bundle-removal semantic-output test | **IMPLEMENTED / BATCH F** |
| C-36 | request/response error-like results must not become authorization-safe truth | future Web proof + generic failure taxonomy | synthetic not-found → `AMBIGUOUS_NOT_FOUND`, relation remains UNKNOWN | **IMPLEMENTED / BATCH F** |
| C-37 | raw evidence retention/export/LLM exposure must cross an explicit EvidenceStoragePolicy boundary | plan acceptance 61-63 | `authorize_raw_artifact_use` retain/export/LLM fail-closed tests | **IMPLEMENTED / BATCH F** |
| C-38 | expectation changes that affect validators must invalidate validation snapshot identity | Financial baseline semantics + deterministic validation | `expectation_revision` in ValidationRunRecord/run identity + stateful reference proof | **IMPLEMENTED / BATCH F** |

## Completed batch rows

Batch A completed `C-01..C-10`, the provenance part of `F-01`, `F-02` at generic knowledge level,
and the typed runtime-observation seam needed by `M-06`.

Batch B completed `C-11..C-21`. The locked CI/CD source regression for its Frontier,
investigation-policy and candidate suites was also re-run after implementation: **32 passed**.

Batch C completed `C-22..C-27` and `M-01..M-05`. The locked CI/CD WorkingMemory/context suites were
re-run: **13 passed**. The latest Mobile donor has no test suite, so the locked read-only
characterization was re-run and returned **MOBILE_CHARACTERIZATION_PASS**.

Batch D completed `F-03..F-06` and the generic Phase 9-10 acceptance behavior. The locked CI/CD
validator/candidate/proposal focused regression was re-run: **19 passed**. The locked Financial
oracle/execute/engagement-policy/final-boundary/cleanup focused regression was run in an ephemeral
dependency environment without modifying the donor: **68 passed**.

Batch E completed `C-28..C-33` and `F-07..F-08`, implementing the Phase 11 durable runtime contract.
The locked CI/CD checkpoint/journal/resume regression was re-run: **15 passed**. The locked Financial
oracle/execute/engagement-policy/final-boundary/termination regression was run with ephemeral
dependencies and returned **69 passed**. All three donor worktrees remained clean at their locked
commits after the checks.

Batch F completed `F-01`, `F-02`, `M-06` at the generic mechanism/conformance level and added
`C-34..C-38`. Four reference domains plus the composite proof passed without reference-specific core
changes after the final core freeze. Locked donor mandatory conformance was re-run: **CI/CD 94 passed**,
**Financial 87 passed**, and Mobile returned **MOBILE_CHARACTERIZATION_PASS**. All donor worktrees remained
clean at their locked commits.

The mandatory behavioral migration rows in this matrix are now **100% green for the Generic Core scope**.
Concrete GitHub/financial target/Mobile ADB/Web adapters remain domain-owned implementation work rather
than hidden prerequisites for this core conformance claim.
