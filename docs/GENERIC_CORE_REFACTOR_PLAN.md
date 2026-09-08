# Generic Red-Team Core 3-Source 재구성 계획

> 상태: **Batch A+B+C+D+E+F (Phase 0~14) 구현 완료 / 계획 범위 Generic Core COMPLETE**
>
> 기준 source snapshot
>
> - CI/CD `scenario/cicd`: `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d`
> - Financial `financial`: `83885a478b9151ab4be652caff6ba564dce8ee6e`
> - Mobile `mobile`: `f1ed305fb384de7e0043df8fc5e01f4935ab4689`
>
> Generic 작업 경로: `C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent-generic`

이 문서는 과거의 **“CI/CD Agent에서 generic core를 추출한 뒤 Financial/Mobile을 나중에 붙인다”**는 계획을 폐기한다.

새 목표는 다음과 같다.

> **기존 stale generic 코드베이스를 비우고, 최신 CI/CD / Financial / Mobile 세 프로젝트에서 실제로 구현·검증되어 있는 공통 메커니즘만 선택적으로 추출해 하나의 domain-independent Red-Team Core를 새로 재구성한다.**

이 작업은 세 프로젝트를 합쳐 거대한 단일 Agent를 만드는 작업이 아니다. **도메인 의미론과 도구는 source project에 남겨두고, 여러 도메인에서 반복 재사용할 가치가 있는 mechanism / contract / invariant / orchestration만 core로 승격한다.**

## 0.1 구현 상태 — 2026-09-08 Batch A+B+C+D+E+F

Batch A부터 Batch F까지 실제 코드와 테스트 기준으로 완료됐다. 계획만 완료됐다는 의미가 아니다.

| Phase | 상태 | 실제 결과 |
|---|---|---|
| Phase 0 | **COMPLETE** | donor 3개 HEAD lock, extraction map/conformance matrix/baseline receipt 생성; CI/CD 85 tests PASS, Financial 89 tests PASS, Mobile characterization PASS |
| Phase 1 | **COMPLETE** | stale generic `src/` 117 files + `tests/` 264 files 제거 후 새 `redteam_core` / `redteam_app` skeleton 재구성 |
| Phase 2 | **COMPLETE** | strict model, stable ID, TriState, provenance/evidence/raw-artifact boundary, typed payload envelope, Relation, ExpectationRef, ClaimContract/Candidate, ValidationOutcome/Finding 구현 |
| Phase 3 | **COMPLETE** | typed schema/relation/requirement/validator registries, bundle manifest/dependency validation, namespace/owner validation, explicit composition, freeze, semantic manifest hash, source fingerprint helper 구현 |
| Phase 4 | **COMPLETE** | typed requirement, 0..N resolver registry + deterministic ordered fallback, permission/call-budget gates, typed `ResolverOutput`, snapshot/expiry freshness, conflict-preserving KnowledgeSlot, CAS/atomic commit, duplicate idempotency, Observation→Knowledge→Relation semantic recompute seam/E2E 구현 |
| Phase 5 | **COMPLETE** | registered `RelationSemantics` 기반 generic Frontier, guard/context bridge, bounded traversal, dominance/diversity, completeness/truncation, unknown/fail-closed ceiling 구현 |
| Phase 6 | **COMPLETE** | Finding 선행 없는 `CandidateDeriver`, qualified root-cause identity, durable coverage ledger, family-diverse bounded portfolio, stale revision gate, multi-batch terminal rotation 구현 |
| Phase 7 | **COMPLETE** | immutable `ArtifactBoundary`, path/junction escape 방어, bounded inventory/search/read, scanned-file/byte budget, oversize skip/report, advisory-only `AnalysisHint`, untrusted-artifact typing 구현 |
| Phase 8 | **COMPLETE** | bounded revisioned WorkingMemory, canonical grounding-derived evidence, CAS/reconcile, free-text redaction, deterministic bounded context projection, explicit trust channels, fail-closed history compaction 구현 |
| Phase 9 | **COMPLETE** | manifest-bound Validator applicability, exact runtime set validation, deep snapshot input, canonical ExperimentResult binding, run-all-applicable, fail-closed UNKNOWN, all-outcome durable ledger, deterministic 0..N Finding materialization 구현 |
| Phase 10 | **COMPLETE** | typed experiment action/result/policy constraint registration, manifest-bound executor/evaluator, immutable policy snapshot + active-policy binding, permit-authorized final effect boundary, attempt/retry/UNCERTAIN lifecycle, explicit cleanup ledger 구현 |
| Phase 11 | **COMPLETE** | atomic checkpoint, hash-chained fsync journal, runtime-composition fingerprint, opaque DomainState codec/envelope, single-writer lease, crash-cut recovery, deterministic termination/budget, unified finalizer 구현 |
| Phase 12 | **COMPLETE** | `static_graph`, `stateful_experiment`, `artifact_runtime`, `request_response` 4개 synthetic reference bundle을 core 수정 없이 구현하고 generic graph/state/experiment/artifact/request-response 확장성 검증 |
| Phase 13 | **COMPLETE** | 4개 reference + integration bundle 동시 composition, registration-order independence, bundle-removal isolation, candidate/coverage/validator/checkpoint deterministic composite proof 구현 |
| Phase 14 | **COMPLETE** | locked donor mandatory conformance 재실행: CI/CD 94 PASS, Financial 87 PASS, Mobile read-only characterization PASS; `SOURCE_CONFORMANCE_MATRIX` mandatory rows green |

Current Batch A+B+C+D+E+F gates:

```text
uv run ruff check src tests                         PASS
uv run pytest -ra -o addopts=''                     175 passed
uv run python -m compileall -q src tests            PASS
uv build                                            PASS
PYTHONHASHSEED=1/7/777 runtime checkpoint suite      24/24 each PASS
domain-specific term / architecture gates           PASS
Batch-F four-reference + composite suite             7/7 PASS
reference-stage core-source freeze                   PASS
donor HEAD drift / dirty check                      PASS
CI/CD Batch-B donor regression                      32 passed
CI/CD Batch-C memory/context regression              13 passed
Mobile Batch-C characterization                     PASS
CI/CD Batch-D validator regression                   19 passed
Financial Batch-D oracle/policy/cleanup              68 passed
CI/CD Batch-E checkpoint/journal/resume regression   15 passed
Financial Batch-E oracle/execute/policy/termination  69 passed
CI/CD Batch-F mandatory conformance                  94 passed
Financial Batch-F mandatory conformance              87 passed
Mobile Batch-F characterization                     PASS
```

구현 상세와 exact command/receipt는 `docs/BATCH_A_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_B_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_C_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_D_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_E_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_F_IMPLEMENTATION_RECEIPT.md`를 따른다.
Phase 0 source traceability는 `docs/SOURCE_EXTRACTION_MAP.md`, `docs/SOURCE_CONFORMANCE_MATRIX.md`, `docs/SOURCE_BASELINE_RECEIPT.md`에 고정돼 있다.

---

# 1. 한 줄 목표

```text
CI/CD
  semantic state / relation graph / candidate / frontier / coverage

Financial
  controlled experiment / baseline & delta / policy / oracle

Mobile
  bounded acquisition / heuristic priority / static→runtime verification

                         ↓

             Domain-Independent Red-Team Core
```

최종 core의 mental model은 다음 한 줄이다.

```text
Evidence
→ Knowledge / Relation
→ Candidate
→ Coverage / Investigation
→ Resolver or Controlled Experiment
→ Observation
→ Semantic Recompute
→ Deterministic Validator / Oracle
→ Finding
```

---

# 2. 이번 재설계의 전제

## 2.1 기존 generic 코드에는 호환성 의무가 없다

현재 `WHS-Red-Teaming-Agent-generic`은 이전 CI/CD 코드 복사본에서 출발한 stale baseline이다. 새 architecture를 이 복사본에 맞추지 않는다.

구현 시작 시 다음 원칙을 적용한다.

1. 이 계획 문서와 source extraction manifest는 보존한다.
2. 기존 generic `src/`, `tests/`와 CI/CD 복사본 메타데이터는 제거한다.
3. 새 package skeleton을 만든다.
4. 세 source repo를 read-only donor/source-of-truth로 사용한다.
5. 필요한 symbol은 **그대로 복사 / generic rewrite / domain-only / discard** 중 하나로 분류한 뒤 이식한다.

따라서 migration 비교축은 다음이다.

```text
old generic vs new generic      ← 중요하지 않음

source behavior vs new core     ← 중요함
```

## 2.2 세 프로젝트의 역할은 다르다

현재 최신 source를 다시 분석한 결과 핵심 역할은 다음처럼 나뉜다.

```text
CI/CD
= canonical Evidence / Relation / Knowledge
+ typed missing evidence / Observation / recompute
+ SecurityCandidate / AttackFrontier
+ root-cause CoverageLedger / multi-batch investigation
+ revisioned WorkingMemory / bounded context / checkpoint

Financial
= candidate→experiment 분리
+ baseline / before-after / StateDelta
+ normative(entitled) != observed
+ deterministic oracle / verifier
+ Engagement / final effect-boundary policy
+ cleanup / budget / stop conditions

Mobile
= bounded artifact inventory/search/read
+ root confinement
+ cheap heuristic ranking
+ static suspicion → runtime verification
+ device/runtime evidence acquisition pattern
```

Core는 이 구조를 그대로 복사하지 않는다. **각 source에서 이미 필요성이 증명된 공통 축만 가져온다.**

---

# 3. 외부 객관적 설계 근거

본 계획은 세 내부 코드베이스만 보고 만든 추상화가 아니다. 아래 공식 문서와 오픈소스 source architecture를 대조해 boundary가 합리적인지 검증했다.

## 3.1 CodeQL — path engine과 domain flow definition 분리

공식 문서:

- https://codeql.github.com/docs/writing-codeql-queries/creating-path-queries/

CodeQL path query는 source, sink, 그리고 이 둘을 잇는 data-flow steps를 별도로 정의한다. 즉 **공통 path computation과 언어/프레임워크별 source/sink semantics를 분리하는 구조**다.

우리 설계에 주는 의미:

- `FrontierEngine`과 relation ontology를 분리한다.
- core traversal은 `GITHUB_TOKEN`, `Activity`, `Account`를 몰라야 한다.
- source / mechanism / sink / guard의 의미는 domain registration이 제공한다.

## 3.2 Joern CPG — 여러 frontend → 공통 IR

공식 문서:

- https://docs.joern.io/code-property-graph/
- https://docs.joern.io/frontends/

Joern은 여러 language frontend가 생성한 정보를 language-agnostic CPG로 수렴시키고 syntax/control/data-flow를 한 graph에 통합한다.

우리 설계에 주는 의미:

- YAML, JADX, HTTP recon 등 raw frontend는 달라도 canonical Evidence/Relation contract는 공통일 수 있다.
- frontend와 semantic graph engine을 같은 layer에 박아 넣지 않는다.

## 3.3 OWASP WSTG — 모든 취약점이 source→sink path는 아니다

공식 문서:

- https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/10-Business_Logic_Testing/
- https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/10-Business_Logic_Testing/10-Test-Payment-Functionality
- GitHub source: https://github.com/OWASP/www-project-web-security-testing-guide/blob/master/latest/4-Web_Application_Security_Testing/10-Business_Logic_Testing/10-Test-Payment-Functionality.md

WSTG는 process timing, function-use limits, workflow circumvention, payment logic 등을 별도 테스트 축으로 둔다.

우리 설계에 주는 의미:

- `AttackFrontier`는 강력한 investigation primitive지만 모든 Candidate/Finding의 필수 표현이 아니다.
- idempotency, race, value conservation, state-transition violation은 path 없이도 canonical Candidate가 될 수 있어야 한다.

## 3.4 OWASP MASTG — static fact와 runtime verification의 결합

공식 문서:

- https://mas.owasp.org/MASTG/best-practices/MASTG-BEST-0052/
- https://github.com/OWASP/mastg

MASTG는 exported component 같은 static attack surface와 실제 runtime 동작 검증을 함께 다룬다.

우리 설계에 주는 의미:

```text
Static Evidence
→ UNKNOWN / Candidate
→ EvidenceRequirement
→ Runtime Verifier
→ Observation
→ Recompute
```

패턴을 core investigation mechanism으로 보존한다.

## 3.5 Python Protocol + pluggy — 작은 host/plugin contract

공식 문서/소스:

- https://docs.python.org/3/library/typing.html#typing.Protocol
- https://github.com/pytest-dev/pluggy/blob/main/docs/index.rst
- https://github.com/pytest-dev/pluggy

Python `Protocol`은 structural typing을 제공하지만 runtime check는 실제 signature type까지 검증하지 않는다. pluggy는 host가 hookspec을 정의하고 1:N implementation을 등록·검증하는 구조를 쓴다.

우리 설계에 주는 의미:

- God `DomainPlugin` 하나를 만들지 않는다.
- 작은 Protocol + static type check + startup registration/schema validation을 조합한다.
- 하나의 requirement/hook에 여러 implementation이 존재할 수 있는 구조를 허용한다.
- 실제 독립 package 배포 요구가 생기기 전에는 auto-discovery를 만들지 않는다.

## 3.6 Pydantic — strict contract / discriminator

공식 문서:

- https://docs.pydantic.dev/2.1/api/config/
- https://docs.pydantic.dev/2.12/errors/usage_errors/

`extra='forbid'`, strict validation, discriminated schema가 제공되는 만큼 core boundary에서도 free-form `dict[str, Any]`를 canonical contract로 쓰지 않는다.

## 3.7 Durable side effect / idempotency — retry는 명시적 계약이어야 한다

공식 문서:

- AWS Powertools Idempotency: https://docs.aws.amazon.com/powertools/python/latest/utilities/idempotency/
- Temporal Retry Policy source: https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx

AWS Powertools는 동일 input의 반복 실행이 추가 side effect를 만들지 않도록 idempotency key와 `INPROGRESS/COMPLETE` 상태를 persistence layer에 둔다. Temporal도 external/failure-prone activity retry를 declarative policy로 명시한다.

우리 설계에 주는 의미:

- “요청을 보냈는데 응답 전에 crash”를 단순 FAILED로 축약하지 않는다.
- retry 여부는 generic runtime의 추측이 아니라 executor의 retry semantics와 durable attempt state를 근거로 한다.
- state-changing experiment에는 attempt identity와 persistence가 필요하다.

---

# 4. 절대 불변식

어떤 domain이 붙어도 아래는 core가 보장해야 한다.

## 4.0 Trust model

이번 framework의 trust boundary를 과장하지 않는다.

```text
TRUSTED CODE
- redteam_core
- application composition root
- 사용자가 설치/선택한 domain/integration bundle implementation

UNTRUSTED DATA
- 분석 대상 repository/APK/web response
- target-generated text/config/resource
- tool/provider가 target에서 수집한 raw content
- LLM-generated free text
```

Python Protocol/registry는 **악성 plugin sandbox가 아니다.** trusted bundle이 직접 `httpx`/`subprocess`를 호출해 정책 경계를 고의로 우회하는 것까지 Python 타입 시스템이 막아준다고 주장하지 않는다.

이번 core는 trusted extension에 대해 architecture/import tests와 designated effect boundary를 강제한다. 향후 제3자 **untrusted plugin**을 실행해야 한다면 별도 process/container sandbox, OS 권한, IPC capability model을 새로운 security boundary로 설계해야 하며 이번 범위에는 포함하지 않는다.

## 4.1 LLM은 truth authority가 아니다

LLM이 가능한 것:

- 어떤 root cause / Candidate를 먼저 조사할지 제안
- bounded action 선택
- WorkingMemory hypothesis 제안
- 추가 evidence가 필요한 이유 설명

LLM이 불가능한 것:

- authoritative Evidence 생성
- authoritative Observation 생성
- Relation verdict 임의 변경
- UNKNOWN을 TRUE/FALSE로 승격
- validator/oracle set 축소
- claim ceiling 상승
- ExecutionPolicy 우회
- raw external side-effect boundary 직접 호출

## 4.2 UNKNOWN은 일급 상태다

```text
모름
→ UNKNOWN
→ typed missing reason / EvidenceRequirement
→ resolver or runtime verifier
→ Observation
→ recompute
```

다음은 FALSE 또는 안전의 증거가 아니다.

- timeout
- 401 / 403 / 404 / 429
- network error
- parser failure
- unsupported representation
- unavailable runtime/device

## 4.3 Observed는 Normative로 자동 승격되지 않는다

Financial의 가장 강한 불변식을 core trust rule로 승격한다.

```text
observed access succeeded
!=
entitled / allowed / expected
```

관측 성공은 관측일 뿐이다. normative claim은 별도 trusted evidence가 필요하다.

## 4.4 Heuristic은 priority이지 truth/filter가 아니다

Mobile keyword candidate나 LLM suspicion은:

```text
priority hint 가능
truth authority 불가
hard scope filter 불가
```

다. cheap ranking이 recall을 닫지 못한다.

## 4.5 Candidate와 Finding은 분리한다

```text
Candidate = 검증 전 보안 가설
Finding   = deterministic validation/oracle 후 materialize된 결과
```

Candidate가 항상 Finding보다 먼저 존재한다.

## 4.6 Path는 optional grounding이다

Candidate는 다음 중 하나 이상으로 grounding될 수 있다.

- Evidence
- Relation
- Graph path
- State / baseline / invariant
- Experiment result

`path_variants`가 없다는 이유만으로 Candidate를 거부하지 않는다.

## 4.7 실제 effect boundary에서 policy를 다시 강제한다

Financial `Session._request()`의 이중 방어 원리를 일반화한다.

```text
Planner / LLM
→ preflight policy
→ executor
→ FINAL effect-boundary policy
→ external effect
```

상위 검사가 빠져도 실제 I/O 직전 authoritative boundary가 fail-closed 해야 한다.

---

# 5. 최종 목표 패키지 구조

```text
src/redteam_core/
├─ model/
│  ├─ schema.py
│  ├─ ids.py
│  ├─ tristate.py
│  ├─ provenance.py
│  ├─ evidence.py
│  ├─ relation.py
│  ├─ candidate.py
│  ├─ finding.py
│  └─ outcome.py
├─ evidence/
│  ├─ catalog.py
│  ├─ artifact_ref.py
│  └─ storage_policy.py
├─ knowledge/
│  ├─ state.py
│  ├─ observation.py
│  ├─ expectation.py
│  ├─ revision.py
│  └─ semantic_manifest.py
├─ acquisition/
│  ├─ artifact_boundary.py
│  ├─ inventory.py
│  ├─ bounded_reader.py
│  ├─ search.py
│  └─ hint.py
├─ investigation/
│  ├─ requirement.py
│  ├─ resolver.py
│  ├─ task.py
│  ├─ coverage.py
│  ├─ portfolio.py
│  └─ policy.py
├─ graph/
│  ├─ semantics.py
│  ├─ registry.py
│  └─ frontier.py
├─ reasoning/
│  ├─ working_memory.py
│  ├─ context_projection.py
│  └─ objective.py
├─ validation/
│  ├─ validator.py
│  ├─ oracle.py
│  ├─ applicability.py
│  └─ materialize.py
├─ execution/
│  ├─ experiment.py
│  ├─ policy.py
│  ├─ attempt.py
│  ├─ effect_boundary.py
│  └─ cleanup.py
├─ runtime/
│  ├─ budget.py
│  ├─ termination.py
│  ├─ checkpoint.py
│  ├─ journal.py
│  ├─ domain_state.py
│  ├─ lease.py
│  ├─ finalizer.py
│  └─ orchestrator.py
├─ extension/
│  ├─ contracts.py
│  ├─ registries.py
│  ├─ bundle.py
│  └─ composition.py
└─ security/
   ├─ redaction.py
   └─ boundary.py

src/redteam_app/
└─ composition_root.py

tests/reference_domains/
├─ static_graph/
├─ stateful_experiment/
├─ artifact_runtime/
├─ request_response/
└─ composite/
```

중요:

- `redteam_core`에는 GitHub/Workflow/GITHUB_TOKEN/AWS OIDC concrete type가 없다.
- `redteam_core`에는 Account/Card/Transaction/Money concrete type가 없다.
- `redteam_core`에는 APK/Activity/WebView/JADX/ADB concrete type가 없다.
- `execution/`은 HTTP/송금/ADB action을 구현하지 않는다. generic contract/policy/attempt lifecycle만 가진다.
- `acquisition/`은 APK/YAML parser를 구현하지 않는다. safe bounded acquisition contract만 가진다.
- raw response/log/screenshot/file bytes는 canonical model에 무제한 inline 저장하지 않고 evidence storage boundary를 거친다.

---

# 6. Source Extraction Matrix

모든 donor symbol은 Phase 0에서 아래 네 분류 중 하나로 기록한다.

```text
DIRECT_ADAPT     거의 그대로 가져오되 이름/의존성만 정리
REWRITE_AS_CORE  알고리즘/불변식만 보존하고 domain 결합 제거
DOMAIN_ONLY      좋은 코드지만 generic core가 소유하면 안 됨
DISCARD          stale/monolithic/compatibility debt라 새 core에 이식하지 않음
```

## 6.1 CI/CD에서 추출

### DIRECT_ADAPT / REWRITE_AS_CORE

| Current source | 추출 자산 | 처리 |
|---|---|---|
| `domain/models.py::SchemaModel` | strict schema | DIRECT_ADAPT |
| `domain/models.py::TriState` | TRUE/FALSE/UNKNOWN | DIRECT_ADAPT |
| `Evidence` + stable IDs/hash | evidence grounding | REWRITE_AS_CORE |
| `Relation` | source/type/target/verdict/evidence/requirements | REWRITE_AS_CORE |
| `CandidateDisposition` | investigation lifecycle | ADAPT |
| `CoverageLedgerEntry` | terminal coverage accounting | REWRITE_AS_CORE |
| `framework/evidence_resolver.py` | failure taxonomy / descriptor | REWRITE_AS_CORE |
| investigation/Observation/perception | requirement→observation→revision | REWRITE_AS_CORE |
| `framework/context_window.py` | bounded context | ADAPT |
| `framework/working_memory.py` | revision CAS + grounded hypotheses | REWRITE_AS_CORE |
| checkpoint/resume/journal | durable investigation state | ADAPT |
| redaction/repository boundary | untrusted artifact boundary | REWRITE_AS_CORE |

### 알고리즘만 보존

| Current source | 보존할 알고리즘 | 제거할 CI/CD 결합 |
|---|---|---|
| `graph_frontier.py` | bounded traversal, guard, dominance, diversity, completeness | job/trigger context, GHA guard names, attacker source special-case, permission state |
| `investigation_policy.py` | root-cause representatives, bounded batches, coverage rotation | GHA impact maps, workflow/job context ranking |
| `security_candidate.py` | root-cause identity, evidence/contradiction/guard grounding | Finding→Candidate normalize, GHA rule map, workflow/job/step fields |
| `build_semantics.py` | static hypothesis→runtime evidence requirement→recompute | npm/package lifecycle semantics |

### DOMAIN_ONLY

- Workflow / Job / Step / Trigger
- GitHub event / actor / condition semantics
- GITHUB_TOKEN permission lattice
- Artifact / Cache / workflow_run semantics
- GitHub/AWS/OIDC resolvers and models
- GitHub relation vocabulary
- `scenario_rules.py`
- GHA `CandidateKind`
- GHA impact/scoring tables

## 6.2 Financial에서 추출

### DIRECT_ADAPT / REWRITE_AS_CORE

| Current source | 추출 자산 | 처리 |
|---|---|---|
| `models/finding.py` | Candidate→ExecutionResult→Finding 분리 | REWRITE_AS_CORE |
| `models/state.py` | before/after/delta + missing | generic state-observation contract로 ADAPT |
| `models/knowledge.py`, `knowledge/graph.py` | normative vs observed 분리 | provenance/authority invariant로 ADAPT |
| `knowledge/baseline.py` | normal behavior를 먼저 실측 | BaselineProvider seam으로 ADAPT |
| `oracles/base.py` | PASS/VIOLATION/ANOMALY/UNKNOWN | ADAPT |
| `oracles/registry.py` | candidate가 oracle set을 못 줄임 | applicability registry로 REWRITE |
| `oracles/verifier.py` | deterministic outcome materialization | ADAPT |
| `engagement/policy.py` | compiled hard policy + final boundary enforcement | REWRITE_AS_CORE |
| `engagement/cleanup.py` | created resource + cleanup outcome | ADAPT |
| budget/termination | stop authority > LLM | ADAPT |
| `probing/executor.py` | before→execute→after→delta | Experiment contract로 ADAPT |

### DOMAIN_ONLY / DISCARD

- Money / Account / Card / Merchant / Transaction
- transfer/read_balance 등 operation
- 금융 INV-* 의미론
- SQLi/JWT/financial payloads/probes
- vuln-bank target config
- auth/operation discovery implementation
- 1,194-line `agent/react.py` monolith 자체
- giant `run_probe` if/elif dispatcher

### Financial 최신 코드가 증명한 generic requirement

현재 Financial에는 `allow_race_testing`, `max_concurrency`가 있지만 race 실행 semantic과 최종 boundary의 결합이 완전히 닫혀 있지 않다. HTTP method-level check만으로는 “이 요청 묶음이 race test인가”를 최종 I/O 경계가 알 수 없다.

따라서 generic experiment contract는 최소 다음을 가진다.

```text
ExperimentDescriptor
- experiment_id
- action_type_id
- effect_class
- required_capabilities
- requested_concurrency
- retry_semantics
- cleanup_expectation
```

즉 policy는 transport method만 아니라 **실험 의미 자체**를 검증할 수 있어야 한다.

## 6.3 Mobile에서 추출

### DIRECT_ADAPT / REWRITE_AS_CORE

| Current source | 추출 자산 | 처리 |
|---|---|---|
| `_safe_path()` | root confinement | ArtifactBoundary로 ADAPT |
| `list_files()` limit | bounded enumeration | ADAPT |
| `search_text()` hit/file-size limits | bounded search | ADAPT |
| `read_file()` char limit | bounded acquisition | ADAPT |
| `security_selector.py` | cheap heuristic ranking | AnalysisHint contract로 ADAPT |
| `dynamic_tool.py` | runtime verifier / observation source pattern | provider seam으로 REWRITE |
| `mobile_agent.py` static→dynamic flow | static uncertainty runtime 보강 | Requirement→Observation pattern으로 ADAPT |

### DOMAIN_ONLY / DISCARD

- AndroidManifest concrete model
- Activity/Service/Receiver/Provider type
- WebView semantics
- JADX/ADB subprocess implementation
- Android keyword list
- Gemini prompt
- screenshot/logcat concrete schema
- global `ANALYSIS_ROOT`, `ADB_PATH`, `PACKAGE_NAME` mutable state

Mobile donor code는 문자열 JSON과 module-global state를 그대로 가져오지 않는다.

```text
Provider input/output = typed model
Provider config       = immutable instance state
Raw text              = bounded artifact payload
Runtime result        = typed Observation payload
```

---

# 7. Canonical Core Model

## 7.1 Schema / Stable ID

- canonical model은 strict schema를 사용한다.
- semantic identity field만 stable ID 입력으로 쓴다.
- timestamp/randomness는 semantic ID에 넣지 않는다.
- 동일 canonical input은 동일 ID를 만든다.

Semantic identity와 operational execution identity를 구분한다.

```text
semantic ID
= 같은 의미의 Evidence/Relation/Candidate/Experiment plan이면 동일

operational ID
= 실제 resolver/experiment/cleanup 시도 하나를 식별하는 unique attempt ID
```

timestamp/random UUID 같은 operational 값은 semantic ID hash에 섞지 않는다.

## 7.2 Provenance

`Evidence`와 `Observation`은 provenance를 명시한다.

최소 개념:

```text
Provenance
- source_kind
- producer_id
- trust_class
- acquired_at
- content_hash
- location/ref
```

최소 trust class:

```text
OBSERVED
DECLARED_TRUSTED
SPEC_DERIVED
INFERRED
```

`INFERRED` 단독 evidence는 authoritative truth ceiling을 올릴 수 없다.

### Evidence storage / sensitivity

세 source 모두 evidence를 다루지만 형태가 다르다.

- CI/CD: source excerpt / hash
- Financial: HTTP response / sensitive field redaction
- Mobile: file content / logcat / screenshot

따라서 canonical Evidence와 raw artifact storage를 분리한다.

```text
Evidence
- evidence_id
- evidence_type_id
- provenance
- content_hash
- location/ref
- redacted_preview
- sensitivity_class
- raw_artifact_ref: optional
```

`raw_artifact_ref`가 존재하더라도 raw content 자체는 `Evidence` object에 박지 않는다.

`EvidenceStoragePolicy`가 최소 다음을 결정한다.

- raw storage 허용 여부
- max artifact bytes
- sensitivity별 retention/redaction
- export/report 허용 수준

LLM context에는 raw secret/sensitive artifact를 기본 노출하지 않는다.

Financial의 현재 `allow_raw_response_storage`가 완전한 enforcement boundary로 소비되지 않는 한계와 Mobile logcat/storage dump의 민감정보 가능성을 generic layer에서 반복하지 않는다.

## 7.3 Relation

```text
Relation
- relation_id
- source_id
- relation_type_id
- target_id
- verdict: TriState
- evidence_ids
- missing_reason_ids
- requirement_ids
- context_refs
```

`relation_type_id`는 namespaced registered ID다. Core는 의미 문자열을 해석하지 않는다.

## 7.4 Candidate

```text
SecurityCandidate
- candidate_id
- candidate_type_id
- root_cause_family
- root_cause_key
- claim_contract_id
- max_claim_status
- current_claim_status
- disposition
- evidence_ids
- supporting_relation_ids
- contradicting_relation_ids
- guard_relation_ids
- requirement_ids
- grounding_refs
```

CI/CD `CandidateKind` closed enum을 core에 가져오지 않는다. candidate type은 bundle registry가 제공하는 namespaced ID다.

`root_cause_family`와 `root_cause_key`도 전역 free-form string 충돌을 허용하지 않는다.

```text
RootCauseRef
- owner_bundle_id
- family_id
- key
```

Coverage grouping은 이 qualified identity 전체를 사용한다.

## 7.5 Claim ceiling

현재 CI/CD의 “static candidate는 CONFIRMED 금지”를 core 전역 법칙으로 복사하지 않는다.

대신:

```text
ClaimContract.max_status
```

를 둔다.

예:

```text
static-only producer      → max SUPPORTED
deterministic live proof  → max CONFIRMED 가능
```

Core는 현재 status가 contract ceiling을 넘지 않는지만 강제한다.

공통 claim lifecycle은 명시적으로 정의한다.

```text
CANDIDATE
SUPPORTED
CONFIRMED
REJECTED
```

단 `REJECTED`를 단순 숫자 ordering의 가장 낮거나 높은 값으로 취급하지 않는다. allowed transition/ceiling rule을 명시적으로 검사한다.

## 7.6 Candidate lifecycle과 validation verdict 분리

```text
CandidateDisposition
- UNREVIEWED
- INVESTIGATING
- ACCEPTED
- REJECTED
- BLOCKED_EXTERNAL
- MERGED
```

```text
ValidationVerdict
- PASS
- VIOLATION
- ANOMALY
- UNKNOWN
```

를 별도 축으로 둔다.

## 7.7 Expected / normative evidence contract

Financial `BaselineRegistry`의 핵심은 Money가 아니라 **공격 전에 정상 동작을 실제 관측해 expected behavior의 trusted 근거를 만든다**는 점이다.

Core가 특정 StateDelta 구조를 소유하지는 않는다. 대신 expected/normative material의 provenance를 표현할 seam을 둔다.

```text
ExpectationRef
- expectation_id
- expectation_type_id
- provenance
- evidence_ids
- typed_payload_ref
```

Domain-owned `BaselineProvider` 또는 semantic producer가 typed expectation payload를 만들 수 있다.

규칙:

- baseline/expectation이 없으면 만들어내지 않는다.
- LLM inference는 trusted baseline으로 승격되지 않는다.
- expected와 observed를 같은 field/channel로 overwrite하지 않는다.

---

# 8. Relation Semantics / Frontier

## 8.1 Typed RelationSemantics registry

```text
RelationSemantics
- traversable
- source_tags
- mechanism_tags
- sink_tags
- guard_effect
- context_bridge
- proves_influence
```

Core `FrontierEngine`은 relation name을 직접 비교하지 않는다.

현재 CI/CD에서 제거할 hardcoding:

- `_MAY_BLOCK_GUARDS`
- `_INFORMATIONAL_GUARDS`
- `_EXTERNAL_UNKNOWN_TERMS`
- `_LOCAL_UNKNOWN_TERMS`
- attacker ID special case
- `can_trigger_event_context` special case
- Job/Trigger permission state

Unknown 종류도 영어 문자열 keyword 추론이 아니라 typed missing reason/requirement에서 온다.

## 8.2 보존할 Frontier 알고리즘

- max depth
- max expansions
- cycle prevention
- context bridge
- BLOCKS / MAY_BLOCK guard semantics
- dominance pruning
- deterministic stable order
- diversity selection
- `search_complete` / `truncated` / `depth_limit_reached` 분리

## 8.3 Frontier는 investigation aid

```text
graph/path Candidate      → Frontier 가능
state/invariant Candidate → Frontier 없이 가능
experiment Candidate      → Frontier 없이 가능
```

Finding materialization은 path 존재를 전역 요구하지 않는다.

---

# 9. Candidate Derivation / Root-Cause Coverage

## 9.1 Finding→Candidate legacy 제거

현재 CI/CD의:

```text
scenario_rules
→ Finding/AttackPath
→ SecurityCandidate normalization
```

은 generic에 이식하지 않는다.

새 구조:

```text
Evidence / Relations / DomainState
→ CandidateDeriver
→ SecurityCandidate
→ Investigation / Validation
→ Finding
```

## 9.2 최신 CI/CD multi-batch coverage 보존

현재 최신 CI/CD에서 실제 구현된 root-cause coverage를 core asset으로 승격한다.

```text
all candidates
→ group by root_cause_key
→ representative per root cause
→ family diversity
→ bounded batch
→ terminal disposition commit
→ rotate uncovered roots
→ coverage complete
```

Core가 소유:

- CoverageLedger
- terminal accounting
- representative rotation
- stable deterministic tie-break
- incomplete coverage 상태에서 premature finalization 방지

Domain이 제공:

- root_cause_family/key
- impact / priority hint
- domain cost estimate

CI/CD `_CANDIDATE_IMPACT` 같은 vocabulary는 core에 하드코딩하지 않는다.

---

# 10. Evidence Requirement / Resolver / Observation

## 10.1 Typed requirement registration

GitHub/AWS closed enum을 제거한다.

```text
RequirementTypeRegistration
- type_id
- schema_version
- payload_model
- owner_bundle
```

미등록 requirement string은 허용하지 않는다.

## 10.2 1 requirement → 1 resolver 제한 제거

현재 CI/CD `ResolverRegistry`는 kind 당 하나만 허용한다. Web/Mobile/Runtime에서는 같은 requirement를 static/local/browser/device 등 여러 provider가 풀 수 있다.

새 구조:

```text
Requirement
→ 0..N eligible resolvers
→ deterministic ResolverSelectionPolicy
→ ordered attempts
```

LLM은 resolver를 임의 선택하지 않는다.

선택 기준:

- supported kind
- capability
- trust
- side-effect class
- freshness
- cost
- budget
- stable resolver ID tie-break

## 10.3 Observation

Observation은 최소:

- requirement ID
- resolver/verifier ID
- provenance
- typed payload discriminator
- acquisition timestamp
- semantic content hash

를 가진다.

동일 semantic Observation을 재적용하면 revision은 증가하지 않는다.

Observation ledger는 append-only를 기본으로 한다. **같은 subject에 새 Observation이 왔다고 이전 Observation을 last-write-wins로 덮어쓰지 않는다.**

## 10.4 Freshness / conflict contract

다중 resolver를 허용하면 서로 다른 시점·출처의 Observation이 충돌할 수 있다. 이 문제를 domain 구현에 암묵적으로 떠넘기지 않는다.

최소 공통 계약:

```text
ObservationValidity
- snapshot_ref: str | None
- observed_at
- not_after: datetime | None

EvidenceRequirement
- required_snapshot_ref: str | None
- max_age_seconds: int | None
```

Core 규칙:

1. requirement freshness 조건을 만족하지 않는 Observation은 current truth 근거로 사용하지 않는다.
2. resolver success 자체가 requirement의 semantic truth를 뜻하지 않는다.
3. 서로 모순되는 authoritative Observation을 silent overwrite하지 않는다.
4. domain conflict policy가 명시적으로 우열을 해소하지 못하면 dependent relation/candidate는 `UNKNOWN` 또는 contradiction 상태를 유지한다.
5. conflict에 참여한 Observation/Evidence ref를 모두 보존한다.
6. freshness/authority tie-break는 deterministic해야 한다.

즉 0..N resolver 지원은 “마지막 응답이 정답”이라는 뜻이 아니다.

### Explicit evaluation time

freshness 판단에서 module 곳곳의 `datetime.now()`를 직접 호출하지 않는다.

```text
RuntimeClock
→ evaluation_time
→ freshness evaluation
```

runtime은 각 semantic transaction/checkpoint에 명시적인 `evaluation_time`을 기록한다. test에서는 deterministic fake clock을 주입한다. 동일 checkpoint를 같은 semantic manifest + evaluation time으로 replay하면 같은 freshness 판단이 나와야 한다.

## 10.5 Semantic recompute

```text
Observation
→ canonical knowledge update
→ relations recompute
→ candidates recompute
→ working-memory reconcile
→ coverage reconcile
```

LLM에게 “다시 판단해”라고 위임하는 구조가 아니다.

### Commit ordering / concurrency

resolver가 미래에 병렬 실행되더라도 network completion 순서를 semantic ordering으로 사용하지 않는다.

초기 구현은 correctness를 위해 **ordered resolver attempts + serialized canonical commit**을 기본으로 한다.

여러 Observation을 한 transaction에 적용할 경우:

```text
validate
→ canonical Observation ID sort
→ expected_revision check
→ atomic apply
→ one semantic recompute
```

를 사용한다.

provider completion timing이 Relation/Candidate 결과를 바꾸면 실패다.

---

# 11. Acquisition Core — Mobile + CI/CD에서 추출

## 11.1 ArtifactBoundary

Mobile `_safe_path()`와 CI/CD repository boundary의 공통 mechanism을 합친다.

```text
ArtifactBoundary
- canonical root
- path containment
- symlink/canonical-path handling
- excluded internal paths
- max file bytes
- text/binary policy
```

## 11.2 Bounded acquisition budget

최소 budget:

- max listed files
- max search hits
- max bytes per artifact
- max returned chars
- max total acquisition bytes

provider가 core runtime budget을 임의로 우회하지 못한다.

## 11.3 AnalysisHint

```text
AnalysisHint
- subject_ref
- category_id
- priority
- evidence_refs
- reason_code
```

Hint는 Candidate/Finding이 아니며 hard scope filter 권한도 없다.

---

# 12. Controlled Experiment Core — Financial에서 추출

이전 계획에서는 experiment를 후속 domain 구현으로 미뤘다. 그러나 현재 목표가 세 프로젝트의 generic mechanism을 **이번 core에서 융합**하는 것이므로 **generic experiment contract + policy boundary + attempt lifecycle은 이번 core scope에 포함**한다.

실제 HTTP/송금/ADB 공격 동작은 포함하지 않는다.

## 12.1 Hypothesis와 side-effect execution 분리

```text
SecurityCandidate
→ ExperimentProposal
→ policy authorization
→ ExperimentAttempt
→ ExperimentResult / Observation
→ Validator / Oracle
```

LLM이 raw side-effecting tool args로 외부 effect를 직접 발생시키지 않는다.

## 12.2 ExperimentDescriptor

최소 필드:

```text
experiment_id
action_type_id
subject_ref
target_refs
required_capabilities
effect_class
requested_concurrency
retry_semantics
cleanup_expectation
input_payload_ref
idempotency_key_spec
```

`effect_class` 최소 vocabulary:

```text
READ_ONLY
STATE_CHANGING
HIGH_IMPACT
```

`experiment_id`는 semantic plan identity다. 실제 실행마다 별도 `attempt_id`를 만든다.

```text
ExperimentAttempt
- attempt_id                 # operational unique identity
- experiment_id              # semantic plan identity
- idempotency_key: optional
- reconciliation_key: optional
```

retry-safe를 선언하는 executor는 동일 semantic effect를 중복시키지 않을 근거를 제공해야 한다. 가능한 방식은 domain마다 다르므로 core가 HTTP idempotency header 같은 concrete mechanism을 하드코딩하지 않고 `idempotency_key/reconciliation_key` contract만 소유한다.

state-changing 작업에서 안전한 dedup/reconciliation 근거가 없으면 retry policy는 `NEVER_AUTOMATIC`이 기본이다.

## 12.3 ExecutionPolicy

Financial `CompiledPolicy` 원리를 일반화한다.

Core policy는 `max_amount` 같은 금융 field를 알지 않는다.

```text
ExecutionPolicy
- allowed scopes/targets
- allowed effect classes
- allowed capabilities
- call/request budget
- concurrency ceiling
- cleanup requirement
- stop conditions
- typed domain constraints
```

금액/HTTP host/Android package 같은 concrete constraints는 domain-owned typed policy provider가 해석한다.

`typed domain constraints`는 임의 dict가 아니다.

```text
PolicyConstraintRegistration
- constraint_type_id
- schema_version
- payload_model
- evaluator_id
- owner_bundle
```

으로 등록한다. 미등록 constraint payload는 policy compile/startup에서 거부한다.

Policy는 mutable runtime dict가 아니라 immutable snapshot으로 실행에 바인딩한다.

```text
ExecutionPolicySnapshot
- policy_id
- policy_hash
- approval_ref
- approved_at
- effective_constraints_hash
- created_from_manifest_hash
```

규칙:

- LLM은 policy snapshot을 생성·확장·수정하지 못한다.
- `AUTHORIZED` 이후의 ExperimentAttempt는 정확한 `policy_hash`에 묶인다.
- checkpoint/resume 시 policy hash가 달라졌다면 기존 authorization을 새 policy에 자동 승계하지 않는다.
- policy가 더 엄격해진 경우 fail-closed re-authorization이 필요하다.
- final effect boundary는 planner가 본 policy가 아니라 attempt에 바인딩된 immutable snapshot을 검증한다.

## 12.4 Preflight + final boundary

- preflight: 빠른 사용자 feedback / early reject
- final boundary: 실제 side effect 직전 authoritative enforcement

두 번째가 진짜 security boundary다.

실제 external I/O adapter는 core runtime이 발급한 immutable permit을 받도록 한다.

```text
ExecutionPermit
- permit_id
- experiment_descriptor_hash
- policy_hash
- semantic_manifest_hash
- allowed_effect_class
- capability_refs
- expires_at / evaluation_time
```

designated `EffectBoundary`는 permit binding을 확인한 뒤 adapter를 호출한다. 정상 architecture에서 semantic/candidate/validator code가 직접 network/subprocess effect를 내는 경로는 architecture test로 금지한다.

다만 이 보장은 위 4.0의 **trusted bundle code** 가정 아래의 architecture enforcement다. 악성 Python plugin sandbox라고 주장하지 않는다.

## 12.5 Attempt ledger

side-effecting task는 crash/retry ambiguity를 기록해야 한다.

```text
PLANNED
AUTHORIZED
STARTED
SUCCEEDED
FAILED
UNCERTAIN
```

`STARTED` 뒤 응답을 못 받으면 자동으로 FAILED라고 단정하지 않는다. retry-safe/idempotent proof 또는 reconciliation evidence가 없으면 자동 재실행하지 않는다.

각 attempt는 최소 다음 audit binding을 가진다.

```text
policy_hash
semantic_manifest_hash
experiment_descriptor_hash
```

따라서 resume 후 다른 plugin semantics나 다른 정책으로 예전 authorization을 조용히 재사용하지 않는다.

이 lifecycle은 새 공격 기능이 아니라 active execution을 안전하게 generic화하기 위한 최소 glue다.

## 12.6 Cleanup ledger

```text
CreatedResource
CleanupRequirement
CleanupAttempt
CleanupOutcome
```

정리 방법 없음/실패는 숨기지 않고 terminal report에 남긴다.

---

# 13. Validator / Oracle Core

CI/CD validator와 Financial oracle의 장점을 합친다.

```text
Candidate
+ canonical state
+ optional ExperimentResult
      ↓
deterministic ApplicabilityResolver
      ↓
ALL applicable Validators / Oracles
      ↓
ValidationOutcome[]
      ↓
FindingMaterializer → 0..N Findings
```

Financial 현재 verifier처럼 사람이 보기 위한 대표 verdict를 고르는 기능은 report projection에서 가능하지만, **canonical validation state에서 여러 outcome을 하나로 덮어쓰지 않는다.**

규칙:

- 모든 applicable validator outcome을 durable하게 보존한다.
- 서로 다른 invariant가 동시에 VIOLATION이면 둘 다 남는다.
- PASS와 VIOLATION이 섞였다는 이유로 PASS outcome을 버리지 않는다.
- FindingMaterializer는 deterministic rule로 0..N Finding을 만들 수 있다.
- report가 대표 severity/verdict를 뽑더라도 source outcome refs를 유지한다.

## 13.1 Purity gate

validator/oracle package에서 금지:

- LLM import
- network client import
- subprocess import
- artifact mutation
- external side effect

source-level architecture test로 강제한다.

## 13.2 Candidate/LLM은 validator 범위를 못 줄인다

Candidate type이 applicability facts를 제공할 수는 있어도:

```text
candidate.allowed_validators
```

처럼 자신에게 유리한 검사만 선택하는 field는 두지 않는다.

---

# 14. Working Memory / Context / LLM

## 14.1 CI/CD에서 보존

- revision CAS
- grounded relation/evidence IDs
- stale patch rejection
- bounded hypotheses
- canonical recompute 후 reconcile/discard
- free-text redaction

제거:

- repository-specific ID naming
- GitHub capability names
- Frontier path만 readiness의 유일 조건으로 보는 부분

## 14.2 Financial ReAct에서 보존

- LLM은 한 step에서 bounded decision 하나만 낸다.
- action result는 코드가 만든다.
- budget/termination은 LLM finish보다 우선한다.
- hypothesis 등록과 effecting experiment 실행은 분리한다.

가져오지 않음:

- giant `_dispatch`
- `run_probe` switch
- domain operation string catalog

## 14.3 Context projection

```text
Canonical State
→ deterministic bounded projection
→ LLM
```

projection에는 필요한 범위만 포함한다.

- active portfolio
- coverage summary
- candidate grounding summary
- missing requirements
- working hypotheses
- allowed capabilities
- budget
- recent observations

raw domain state/artifact dump 전체를 모델에게 던지지 않는다.

### Trust boundary / prompt-injection rule

CI/CD repository content와 Mobile APK/code/resource 안의 자연어는 모두 공격자가 조작할 수 있는 **untrusted data**다.

Context projector는 최소 다음 channel을 분리한다.

```text
SYSTEM_POLICY          authoritative instruction
EXECUTION_POLICY       authoritative constraint
TRUSTED_OBJECTIVE      ranking/focus only, policy override 불가
CANONICAL_STATE        code-generated structured state
UNTRUSTED_ARTIFACT     repository/APK/HTTP/tool/domain content
LLM_WORKING_TEXT       hypothesis/note only
```

규칙:

- artifact/tool output의 자연어를 instruction channel로 재주입하지 않는다.
- trusted objective도 Evidence semantics / validator / ExecutionPolicy를 바꾸지 못한다.
- free-text output은 redaction + length bound를 거친다.
- model-visible ref를 canonical ID로 다시 검증한 뒤에만 capability input으로 사용한다.

---

# 15. Extension Architecture

## 15.1 God Plugin 금지

역할별 seam 후보는 다음과 같다.

```text
SemanticExtractor
SemanticRecomputer
CandidateDeriver
RequirementProvider
ResolverProvider
ValidatorProvider
PriorityProvider
DomainStateCodec
AcquisitionProvider
ExperimentProvider
PolicyProvider
EvidenceStore
```

모든 bundle이 모든 역할을 구현할 필요는 없다. **그리고 위 이름을 전부 Day 1 public Protocol로 만들지도 않는다.**

### Public SPI promotion gate

어떤 seam을 public Protocol로 승격하려면 아래 중 하나를 만족해야 한다.

1. 현재 donor source 두 곳 이상에서 같은 substitution boundary가 실제로 존재한다.
2. donor 한 곳 + reference domain 한 곳에서 서로 다른 implementation 교체가 필요함을 test로 증명한다.
3. runtime이 implementation을 모른 채 registry를 통해 호출해야 하는 명확한 inversion-of-control boundary다.

그 외에는 private callable/dataclass/helper로 시작한다. 실제 두 번째 consumer가 생긴 뒤 public SPI로 승격한다.

예를 들어 `RequirementProvider`, `PriorityProvider`는 단순 registered function으로 충분하면 별도 Protocol을 만들지 않는다. 이 규칙으로 “interface를 미리 다 만들어 놓는 프레임워크 뇌절”을 막는다.

`EvidenceStore`는 CI/CD source evidence와 Financial raw-response evidence라는 두 donor에서 실제 교체 boundary 필요성이 확인되므로 public SPI 후보가 될 수 있다. 다만 storage backend 자체는 core가 구현하지 않고 contract/policy만 소유한다.

## 15.2 DomainBundle은 조립 manifest

```text
DomainBundle
- bundle_id
- version
- schema_fingerprint
- bundle_kind: domain | integration | reference
- requires_bundle_ids
- provided registrations
- implementation_fingerprint
```

runtime orchestration은 core가 소유한다.

### Cross-domain integration rule

향후 Mobile→Web→Finance 같은 연결 때문에 sibling domain끼리 내부 state를 import하게 두면 core 밖에서 다시 결합도가 터진다.

따라서 integration도 별도 특권 API가 아니라 **일반 Bundle의 한 종류**로 처리한다.

```text
mobile bundle ─┐
               ├→ integration bundle → canonical Relation/Candidate
web bundle ────┤
finance bundle ┘
```

Integration bundle 규칙:

- `requires_bundle_ids`를 manifest에 선언한다.
- dependency graph는 cycle-free여야 한다.
- 다른 bundle의 private `DomainState` 타입을 직접 import하지 않는다.
- 입력은 canonical `Evidence/Observation/Expectation/Relation/Candidate` 및 해당 canonical object가 참조하는 registered typed payload만 사용한다.
- cross-domain link는 namespaced opaque `subject_ref` 사이의 Relation으로 표현한다.
- core에 universal `Account|Activity|Endpoint` Entity union을 만들지 않는다.
- composition root는 dependency를 canonical order로 topological sort하고 순서에 따라 결과가 바뀌지 않게 한다.

## 15.3 Explicit composition 우선

이번 단계에서는 filesystem/plugin entry-point auto-discovery를 구현하지 않는다.

```python
build_runtime(bundles=(bundle_a, bundle_b))
```

형태의 composition root로 충분하다.

## 15.4 Registry lifecycle

```text
compose
→ register
→ schema/collision validation
→ canonical sort
→ semantic manifest hash
→ FREEZE
→ analysis
```

analysis 시작 후 registry mutation은 금지한다.

bundle dependency topological sort에서 서로 의존하지 않는 node의 tie-break는 canonical `bundle_id` ordering을 사용한다. filesystem discovery order나 registration insertion order를 semantic order로 사용하지 않는다.

`implementation_fingerprint`는 runtime `inspect.getsource()` 같은 불안정한 introspection에 의존하지 않는다. build/test 단계에서 bundle의 semantic implementation source hash를 생성해 manifest에 넣고 checkpoint fingerprint에 포함한다. semantic code가 바뀌었는데 bundle version/fingerprint가 그대로인 상태는 CI gate에서 거부한다.

---

# 16. State / Serialization / Checkpoint

## 16.1 CoreState와 domain state 분리

```text
CoreState
+
DomainStateEnvelope[]
```

Core에 다음 union을 만들지 않는다.

```python
WebState | MobileState | FinanceState
```

각 DomainStateCodec이 encode/decode/validate를 소유한다. Core는 opaque serialized domain payload를 임의 해석하지 않는다.

`DomainStateEnvelope`는 generic dict를 노출하지 않는다.

```text
DomainStateEnvelope
- bundle_id
- schema_version
- codec_id
- payload_bytes_or_canonical_json
- payload_hash
```

Core가 payload 내부 key를 참조하는 코드가 생기면 architecture violation이다.

## 16.2 Semantic manifest fingerprint

checkpoint에는 최소 다음 canonical manifest hash를 기록한다.

- core schema/runtime version
- bundle IDs/versions
- relation type registry
- requirement schemas
- observation payload schemas
- candidate deriver identities
- validator/oracle identities
- priority provider identities
- resolver routing policy
- execution policy schema

fingerprint mismatch는 explicit migrator 없이는 resume 거부한다.

## 16.3 Crash atomicity matrix

최소 cut point:

- Observation 수신 전/후
- knowledge revision 전/후
- candidate recompute 전/후
- coverage commit 전/후
- Experiment `STARTED` 직후
- ExperimentResult persistence 전/후
- cleanup 시작 전/후

resume 때문에 authoritative evidence/commit/side effect가 조용히 중복되면 실패다.

## 16.4 Finalization / cleanup authority

Financial의 `_finalize()` 원리를 일반화한다.

다음 종료 경로는 모두 하나의 runtime finalizer를 거친다.

- normal completion
- budget termination
- policy stop
- LLM finish request
- handled external failure
- unhandled internal exception

cleanup이 required인 경우 finalizer가 cleanup ledger를 실행/마감한다. cleanup 실패는 run manifest/final report를 없애지 않고 실패 상태로 명시한다. 이미 시작된 side effect의 결과/cleanup 상태를 숨긴 채 run을 “정상 종료”로 표시하지 않는다.

---

# 17. 구현 순서

## Phase 0 — Three-source freeze & extraction inventory

**현재 상태: COMPLETE (Batch A)**

**Status: COMPLETE (Batch A, 2026-09-07).**

### 작업

- 세 source HEAD/hash/clean status 기록
- source repo read-only 운영 규칙 고정
- 모든 donor symbol을 `DIRECT_ADAPT / REWRITE_AS_CORE / DOMAIN_ONLY / DISCARD` 분류
- `docs/SOURCE_EXTRACTION_MAP.md` 생성
- 원본의 필수 behavior를 `docs/SOURCE_CONFORMANCE_MATRIX.md`로 작성
- source file + symbol + donor commit + target core module + test mapping 기록
- donor source 자체의 focused characterization test를 먼저 실행하고 `docs/SOURCE_BASELINE_RECEIPT.md`에 command/result/hash를 기록
- Phase 0 완료 후 donor HEAD를 `SOURCE_EXTRACTION_MAP`에 lock하고 구현 phase 진입 전마다 drift check

Focused baseline 후보:

```text
CI/CD
- test_graph_frontier.py
- test_investigation_policy.py
- test_investigation_runtime.py
- test_security_candidates.py
- test_working_memory.py
- test_evidence_resolvers.py

Financial
- test_graph.py
- test_oracles.py
- test_execute_spine.py
- test_concurrency.py
- test_engagement_policy.py
- test_policy_enforcement.py
- test_session.py
- test_termination_policy_stop.py
```

Mobile에는 현재 별도 `tests/` 디렉터리가 없으므로 donor source를 수정하지 않고 generic workspace의 migration characterization harness에서 최소 다음을 고정한다.

- root escape reject
- list/search/read bounds
- oversize search skip
- heuristic candidate가 비후보 파일을 hard exclude하지 않는 현재 agent contract
- fake/substituted runtime result를 static 판단과 구분

baseline test가 원본에서 이미 실패하면 새 core의 regression으로 세지 않고 receipt에 pre-existing failure로 명시한다.

Source HEAD가 lock 이후 바뀌면 자동으로 새 source를 섞어 쓰지 않는다. 작업을 중단하고:

1. commit diff 확인
2. extraction map 영향 분석
3. conformance baseline 재실행
4. 필요한 plan delta 기록

후 다시 진행한다.

### Exit gate

모든 core 후보에 **왜 generic인지 / 어떤 source behavior를 보존하는지 / 어떤 test가 지키는지**가 명시돼야 한다.

## Phase 1 — Old generic workspace reset

**현재 상태: COMPLETE (Batch A)**

**Status: COMPLETE (Batch A, 2026-09-07).**

### 작업

- 정확한 generic root 검증
- recursive delete 전에 resolved absolute root가 `C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent-generic` 내부인지 재검증
- 삭제 대상 preview/inventory를 receipt에 기록하고 parent/source repo 경로가 포함되면 즉시 중단
- 계획 문서와 extraction/conformance docs 보존
- stale `src/`, `tests/` 제거
- old CI/CD README/PLANS/pyproject 재생성
- 새 `src/redteam_core`, `src/redteam_app`, tests skeleton 생성

### Exit gate

빈 새 core가 source project package를 import하지 않는다.

## Phase 2 — Foundational models & provenance

**현재 상태: COMPLETE (Batch A)**

**Status: COMPLETE (Batch A, 2026-09-07).**

### 구현

- strict SchemaModel
- stable semantic ID
- authoritative boundary에서 forged requirement/observation/relation stable ID 재검증
- TriState
- Provenance
- Evidence
- EvidenceArtifactRef / EvidenceStoragePolicy
- Observation base
- Relation
- ExpectationRef
- ClaimContract / Candidate
- ValidationOutcome / Finding

### Source basis

CI/CD canonical models + Financial finding/provenance discipline.

### Exit gate

domain concrete type 0 상태에서 serialization round-trip, stable ID, ceiling, provenance, sensitive raw-artifact boundary tests green.

## Phase 3 — Typed registries & extension freeze

**현재 상태: COMPLETE (Batch A)**

**Status: COMPLETE (Batch A, 2026-09-07).**

### 구현

- relation type registry
- evidence type/payload registry
- requirement type registry
- observation payload registry
- expectation type/payload registry
- candidate type registry
- experiment action/input registry
- validator registry
- policy constraint registry
- bundle manifest
- schema/collision validation
- registry freeze
- semantic manifest hash
- build-time implementation fingerprint
- stale expected implementation fingerprint 검증 helper/CI test

### Exit gate

registration order permutation 결과 동일. duplicate/conflicting registration은 startup fail. semantic implementation 변경 후 fingerprint 미갱신도 CI fail.

## Phase 4 — Knowledge revision & Evidence resolution

**현재 상태: COMPLETE (Batch A)**

**Status: COMPLETE (Batch A, 2026-09-07).**

### 구현

```text
Requirement
→ deterministic resolver selection
→ Observation
→ apply
→ revision
→ recompute
```

- 0..N resolvers
- deterministic ordered fallback after a higher-ranked resolver exhausts its call budget
- resolver required-permission gate
- per-resolver call budget enforcement
- failure taxonomy
- idempotent Observation
- typed missing reason
- freshness validity (`max_age`, required snapshot, explicit expiry)
- conflicting Observation preservation / fail-closed merge
- explicit RuntimeClock/evaluation_time
- serialized atomic semantic commit
- typed `ResolverOutput` carrying payload + Observation validity metadata
- generic `SemanticRecomputer` seam
- canonical Observation → KnowledgeSlot → Relation recompute in the same atomic revision

### Source basis

CI/CD investigation / evidence resolver / perception.

### Exit gate

resolver failure가 FALSE/SAFE로 승격되지 않고 duplicate Observation은 semantic revision +0. conflicting authoritative observations도 silent last-write-wins 없이 보존된다. resolver completion/registration order가 semantic result를 바꾸지 않으며, reference E2E에서 `Requirement → Resolver → Observation → Knowledge → Relation TRUE`, conflicting authoritative Observation 추가 후 `Relation UNKNOWN` 재계산까지 증명한다.

## Phase 5 — Generic graph / Frontier engine

**현재 상태: COMPLETE (Batch B)**

**Status: COMPLETE (Batch B, 2026-09-08).**

### 구현

- RelationSemantics registry
- source/mechanism/sink/guard/context bridge
- bounded traversal
- dominance
- diversity
- completeness metadata
- unregistered relation fail-closed non-traversable
- registered intermediate-sink continuation
- context-scoped guard applicability
- generic claim ceiling (`SUPPORTED/CANDIDATE/REJECTED`) based on path certainty

### Source basis

CI/CD Frontier + CodeQL/Joern architecture.

### Exit gate

core Frontier source에 CI/CD relation/source/context special-case 0. `RelationSemantics`는 frozen
registration으로 semantic manifest hash에 포함되고, unit tests가 bounded traversal, context bridge,
guard, UNKNOWN ceiling, hard block, dominance, diversity, truncation/depth metadata, unregistered relation
fail-closed를 증명한다.

## Phase 6 — Candidate-first + Coverage engine

**현재 상태: COMPLETE (Batch B)**

**Status: COMPLETE (Batch B, 2026-09-08).**

### 구현

- CandidateDeriver Protocol
- root-cause family/key
- CoverageLedger
- bounded portfolio
- family diversity
- multi-batch root rotation
- terminal accounting
- `CandidateDeriverRegistration` + implementation fingerprint in semantic manifest
- runtime deriver set == frozen manifest exact-match gate
- candidate/knowledge revision-bound portfolio
- duplicate batch-record / unrecorded terminal commit rejection
- disappeared candidate terminal-history preservation
- pathless state/invariant candidate support

### Source basis

latest CI/CD `security_candidate.py`, `investigation_policy.py`.

### Exit gate

Finding 없이 Candidate가 먼저 생성되고, path가 없는 state/invariant candidate도 허용되며,
portfolio size보다 많은 root causes도 family-diverse batch rotation으로 전부 terminal accounting된다.
Batch B integration test가 `Relation graph → Frontier → CandidateDeriver → Coverage portfolio → terminal
root-cause decision` 전체 spine을 검증한다.

## Phase 7 — Acquisition subsystem

**현재 상태: COMPLETE (Batch C)**

**Status: COMPLETE (Batch C, 2026-09-08).**

### 구현

- immutable `ArtifactBoundary` + strict relative-path resolution
- parent/absolute/symlink/junction escape rejection
- bounded inventory/search/read
- scanned-file + total-byte + single-file acquisition budget
- TOCTOU-aware stable-size read check
- oversize direct-read rejection and search skip/report
- explicit truncation metadata
- `UNTRUSTED_ARTIFACT` typed content marker
- advisory-only `AnalysisHint` + deterministic ranking
- acquisition operation APIs have no hint-based hard-scope input

### Source basis

CI/CD repository boundary + Mobile tools/security selector.

### Exit gate

path escape/symlink/junction escape/oversize/budget exhaustion fail-closed. Hint를 hard filter로 쓰는
code path 0. Batch C unit/integration tests와 최신 Mobile donor one-off characterization이 bounded
list/search/read 및 oversize behavior를 검증한다.

## Phase 8 — WorkingMemory + bounded LLM context

**현재 상태: COMPLETE (Batch C)**

**Status: COMPLETE (Batch C, 2026-09-08).**

### 구현

- 최대 8개의 revisioned hypotheses + 12k serialized-memory ceiling
- relation/candidate canonical grounding validation
- LLM patch가 `evidence_ids`/missing/requirement를 직접 기록할 수 없는 schema
- Evidence/missing/requirement refs는 canonical Candidate/Relation에서 core가 재도출
- stale CAS patch rejection + semantic no-op revision +0
- canonical recompute 후 READY/BLOCKED/DISCARDED deterministic reconcile
- reconciliation marker-only update는 WorkingMemory revision +0
- WorkingMemory가 authoritative Knowledge/Relation/Candidate state를 mutate하지 못하는 architecture gate
- free-text claim/summary/next-check deterministic best-effort redaction
- deterministic bounded context projection
- `SYSTEM_POLICY / EXECUTION_POLICY / TRUSTED_OBJECTIVE / CANONICAL_STATE /
  UNTRUSTED_ARTIFACT / LLM_WORKING_TEXT` explicit trust-channel split
- active portfolio candidate ref revalidation, coverage summary, allowed capabilities, bounded budget view
- recent Observation은 payload raw JSON 없이 typed metadata/hash/ref만 projection
- oversized history는 LLM factual summarization 없이 deterministic omission/tool-hash compaction
- irreducible oversized history는 원본을 그대로 반환하지 않고 fail-closed

### Source basis

CI/CD WorkingMemory/context + Financial bounded ReAct decision principle.

### Exit gate

stale revision reject, unknown grounding reject, LLM free text → authoritative state direct path 0.
WorkingMemory은 bounded/CAS/reconciled 상태를 유지하고, model-visible context는 explicit trust channel과
character/token budget을 통과한 projection만 생성한다. CI/CD donor WorkingMemory/context regression은
13 tests PASS이며 Batch C integration test가 `bounded artifact → canonical Relation → grounded WorkingMemory
→ bounded context` spine을 검증한다.

## Phase 9 — Validation / Oracle subsystem

### 구현

- Validator Protocol
- pure ValidationOutcome
- deterministic applicability
- run-all-applicable
- Finding materialization
- all-outcome durable preservation

### Source basis

CI/CD validator + Financial oracle registry/verifier.

### Exit gate

Candidate/LLM이 validator set을 줄일 수 없음. validator package에서 LLM/network/subprocess import 0. 여러 validator outcome이 single-summary 때문에 유실되지 않음.

### 구현 결과 — Batch D

- `ValidatorRegistration`이 candidate-type applicability와 runtime-result requirement를 semantic manifest에 고정한다.
- runtime registry는 frozen manifest와 exact validator set/applicability가 일치해야 한다.
- candidate가 validator allowlist를 갖는 escape hatch는 없다.
- validator에는 authoritative `CoreSession`이 아니라 deep snapshot candidate/result/session만 전달된다.
- optional `ExperimentResult`는 execution ledger의 canonical current result와 exact match해야 한다.
- validator exception / zero outcome / required result 부재는 해당 validator audit slot을 `UNKNOWN`으로 남긴다.
- validator가 canonical validation context 밖 Evidence ID를 주장하면 그 decision은 `UNKNOWN`으로 강등된다.
- PASS/UNKNOWN/ANOMALY/VIOLATION outcome 전부 durable ledger에 보존하고, Finding은 deterministic 0..N projection으로 materialize한다.
- `tests/integration/test_batch_d_spine.py`가 실제 execution result를 validation에 연결해 Finding까지 검증한다.

## Phase 10 — Controlled Experiment / Policy

### 구현

- ExperimentDescriptor
- effect classes
- capability requirements
- ExecutionPolicy
- immutable ExecutionPolicySnapshot / approval provenance
- preflight + final boundary
- ExperimentAttempt ledger
- retry-safety
- cleanup ledger
- stop/budget hooks

### Source basis

Financial candidate/experiment split + engagement policy + executor + cleanup.

### Exit gate

- state-changing experiment는 policy 없이 실행 불가
- final boundary에서 concurrency/effect/capability 재검사
- race/high-impact capability는 explicit allow 없이 실행 불가
- `STARTED` 후 outcome 불명은 `UNCERTAIN`
- retry-unsafe automatic replay 0
- policy hash 변경 시 기존 authorization 자동 재사용 0

### 구현 결과 — Batch D

- `EXPERIMENT_ACTION`, `EXPERIMENT_RESULT`, `POLICY_CONSTRAINT` payload는 전부 registered typed envelope이다.
- `ExperimentExecutorRegistration`과 `PolicyConstraintEvaluatorRegistration` 구현 fingerprint/applicability가 semantic manifest에 포함된다.
- `TypedPayloadEnvelope` 자체를 immutable로 만들어 frozen policy 내부 constraint의 사후변조도 막는다.
- `ExecutionPolicySnapshot`은 deterministic effective policy hash + approval provenance + semantic manifest hash를 보존한다.
- 실행 전 명시적으로 한 snapshot을 `active_policy_id`로 활성화해야 하며 새 policy가 활성화되면 옛 permit/authorization은 final boundary에서 거부된다.
- preflight와 final effect boundary 모두 scope/effect/capability/concurrency/attempt budget/cleanup/typed constraints를 검사한다.
- 외부 executor는 descriptor + policy + semantic manifest에 binding된 unexpired `ExecutionPermit` 이후에만 호출된다.
- semantic `experiment_id`와 operational `attempt_id`를 분리하고 `PLANNED→AUTHORIZED→STARTED→SUCCEEDED|FAILED|UNCERTAIN`을 durable state로 기록한다.
- executor가 STARTED 이후 예외/결과 검증 실패를 내면 결과를 임의 FAILED로 단정하지 않고 `UNCERTAIN`으로 기록한다.
- automatic retry는 `IDEMPOTENT` + operational idempotency key 또는 `RECONCILABLE` + reconciliation key가 있을 때만 가능하며 semantic manifest 변경을 넘지 못한다.
- created-resource / cleanup-requirement / cleanup-attempt / cleanup-outcome ledger를 분리하고 cleanup executor 부재를 `NOT_CONFIGURED`로 명시한다.

## Phase 11 — Runtime / Checkpoint

**현재 상태: COMPLETE (Batch E)**

**Status: COMPLETE (Batch E, 2026-09-08).**

### 구현

- atomic state snapshot
- semantic manifest fingerprint
- safe resume
- event journal
- deterministic termination/budget authority
- RuntimeClock / explicit evaluation time
- unified finalizer / cleanup-on-all-terminal-paths

### Exit gate

fingerprint mismatch resume fail + crash-cut matrix green.

### 구현 결과 — Batch E

- `AtomicCheckpointStore`가 canonical `CoreSession + RuntimeState + DomainStateEnvelope[]`를 temp-file
  fsync + atomic replace로 저장하고 기존 checkpoint가 corrupt/tampered이면 다음 write로 덮지 않는다.
- `EventJournal`은 sequence + previous-event hash를 가진 fsync append-only chain이며 torn final JSON line만
  repair하고 중간 corruption/semantic tamper/hash-chain break는 fail-closed 한다.
- `RuntimeCompositionFingerprint`는 frozen semantic manifest뿐 아니라 resolver descriptor set,
  resolver routing policy ID, priority-provider identity까지 묶는다. mismatch는 explicit migrator 없이
  resume 거부한다.
- `DomainStateCodecRegistration`은 bundle-owned codec schema/implementation fingerprint를 semantic manifest에
  포함한다. runtime codec set은 manifest와 exact-match해야 하며 `DomainStateEnvelope`는 opaque canonical JSON
  + content hash만 보존한다. checkpoint layer는 domain payload 내부 key를 해석하지 않는다.
- `RunLease`는 OS advisory lock으로 한 run directory의 process-level single writer를 강제하고 process death 시
  OS가 lock을 회수하므로 stale PID 추측 삭제에 의존하지 않는다.
- experiment effect는 `STARTED state checkpoint → durable journal start marker → external executor` 순서이며,
  terminal result도 checkpoint가 journal terminal marker보다 먼저 durable해진다.
- resume 시 checkpointed/post-checkpoint `STARTED` experiment 또는 cleanup effect는 결과를 꾸며내지 않고
  `UNCERTAIN`으로 복구한다. 자동 replay는 Phase 10 retry-safety 계약 없이는 발생하지 않는다.
- Observation/knowledge 및 Candidate/Coverage의 checkpoint 전/후 crash cut에서 동일 authoritative commit을
  재적용해도 duplicate semantic revision/coverage commit이 생기지 않는 integration test를 고정했다.
- `RuntimeUsage`는 monotonic하고 termination authority는 `policy stop → budget → internal/external failure →
  normal completion → LLM finish` 순서로 deterministic하게 판정한다.
- normal/budget/policy/LLM-finish/handled-external/internal-error 여섯 종료 종류는 모두 같은 finalizer를 거친다.
  FINALIZING state와 cleanup STARTED/terminal state를 각각 checkpoint하며 cleanup failure/uncertainty/
  NOT_CONFIGURED를 성공처럼 숨기지 않는다.
- FINALIZING checkpoint resume는 `requires_finalization=True`를 반환해 일반 investigation loop로 잘못
  재진입하지 않고 finalizer 재진입이 필요함을 명시한다.
- final `FINALIZED` checkpoint가 `RUN_FINALIZED` journal marker보다 먼저 durable해져 마지막 crash window에서도
  journal만 terminal이고 state snapshot은 비terminal인 모순을 만들지 않는다.
- Batch E runtime + crash-cut suite는 `PYTHONHASHSEED=1`, `7`, `777`에서 각각 26/26 PASS했고 전체 generic
  suite는 165/165 PASS했다.

## Phase 12 — Four Reference Domains

**현재 상태: COMPLETE (Batch F)**

**Status: COMPLETE (Batch F, 2026-09-08).**

실제 CI/CD/Finance/Mobile 기능을 이식하지 않고 donor 세 architecture와 미래 Web adapter를 대표하는 tiny test bundle을 만든다.

### A. `static_graph`

```text
untrusted_input
→ transform
→ execution
→ sensitive_sink
```

검증:

- UNKNOWN
- guard
- context bridge
- Frontier
- candidate root coverage

### B. `stateful_experiment`

```text
normal baseline
→ controlled state change
→ before/after delta
→ invariant validator
```

검증:

- path 없는 Candidate
- observed != normative
- experiment policy
- validator applicability
- in-memory fake state only

### C. `artifact_runtime`

```text
bounded artifact scan
→ heuristic hint
→ static UNKNOWN
→ runtime requirement
→ fake verifier Observation
→ TRUE/FALSE recompute
```

실제 APK/ADB는 쓰지 않는다.

### D. `request_response`

향후 Web domain 확장성 검증용 fake request/response bundle이다. 실제 network는 사용하지 않는다.

```text
principal
→ authenticated request
→ attacker-controlled resource identifier
→ authorization UNKNOWN
→ fake cross-principal experiment
→ typed Observation
→ deterministic authorization validator
```

검증:

- HTTP/Web concrete type를 core에 추가하지 않고 request/response security flow 표현 가능
- BOLA-like authorization candidate를 canonical relation/state로 표현 가능
- 403/404/transport-failure를 안전 증거로 오판하지 않음
- read-only와 state-changing experiment policy를 서로 다르게 적용 가능

### Exit gate

각 reference bundle 추가/변경에 core source 수정 0.

### 구현 결과 — Batch F

- `tests/reference_domains/static_graph.py`가 UNKNOWN relation, MAY_BLOCK guard, context bridge,
  Frontier, Candidate-first derivation, root-cause coverage를 synthetic graph로 검증한다.
- `tests/reference_domains/stateful_experiment.py`가 path 없는 Candidate, typed Expectation baseline,
  controlled state change, policy constraint, ExperimentResult, deterministic validator를 연결한다.
  inferred expectation만 있을 때 validation은 `UNKNOWN`이고 authoritative baseline이 생긴 뒤 동일한
  observed success가 normative expectation과 다르면 `VIOLATION`이 된다.
- `tests/reference_domains/artifact_runtime.py`가 bounded untrusted artifact → advisory hint → UNKNOWN
  relation → typed requirement → fake runtime Observation → TRUE recompute를 검증한다.
- `tests/reference_domains/request_response.py`가 concrete HTTP type을 core에 추가하지 않고
  cross-principal authorization flow를 표현한다. synthetic not-found는 `AMBIGUOUS_NOT_FOUND` failure로
  남아 SAFE/FALSE가 되지 않고, read-only policy와 state-changing policy가 분리된다.
- final acceptance 감사에서 기존 `EvidenceStoragePolicy`가 helper 수준에 머문 gap을 발견해
  `redteam_core.evidence.storage`의 지정 raw-artifact policy boundary를 추가했다. retain/export/LLM
  exposure는 각각 explicit policy를 통과해야 한다.
- expectation을 실제 validator 입력으로 쓰면서 validation cache identity가 expectation revision을
  포함하도록 보강했다. inferred/authoritative expectation은 provenance-bound identity로 분리된다.
- 위 generic acceptance 보강 후 core source를 SHA-256
  `53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c`로 freeze했고, reference
  suite는 그 이후 core source 수정 0을 hash gate로 검증한다.

## Phase 13 — Composite proof

**현재 상태: COMPLETE (Batch F)**

**Status: COMPLETE (Batch F, 2026-09-08).**

세 reference bundle이 한 runtime에 함께 존재하는지 검증한다.

목적은 Web+Mobile+Finance 기능 구현이 아니라 **multi-domain composition seam** 검증이다.

검증:

- namespace collision 없음
- bundle A가 bundle B 내부 state 직접 import하지 않음
- `requires_bundle_ids` 기반 integration bundle이 shared canonical refs를 연결 가능
- bundle dependency cycle startup reject
- coverage/validator/runtime deterministic
- registration order permutation 결과 동일

### 구현 결과 — Batch F

- 4개 reference bundle과 `refintegration` integration bundle을 한 catalog에 동시에 compose한다.
- normal/reverse registration order의 semantic manifest hash가 동일하다.
- integration bundle은 `requires_bundle_ids`만 선언하며 sibling reference implementation/private state를
  import하지 않는 AST gate를 통과한다.
- static bundle 제거 후 stateful/request 등 남은 bundle registration과 실제 stateful Candidate semantic
  output이 동일함을 검증한다.
- one-runtime composite test에서 multiple candidate deriver, deterministic portfolio, validator idempotency,
  multi-bundle checkpoint round-trip을 검증한다.
- Observation 이후 relation recompute → Candidate reconcile → WorkingMemory reconcile → coverage state가
  deterministic하게 수렴하고 동일 입력 재적용 시 안정적으로 유지됨을 reference test에서 검증한다.

## Phase 14 — Source Conformance Regression

**현재 상태: COMPLETE (Batch F)**

**Status: COMPLETE (Batch F, 2026-09-08).**

새 core를 원본 프로젝트와 runtime-link하지는 않는다. 대신 donor mechanism의 핵심 behavior를 conformance test로 보존한다.

### CI/CD mandatory behavior

- TriState UNKNOWN
- Observation idempotency
- bounded Frontier / dominance / guard
- root-cause multi-batch coverage
- WorkingMemory CAS
- validator grounding/ceiling

### Financial mandatory behavior

- Candidate / Experiment 분리
- observed != entitled
- baseline missing → UNKNOWN
- all-applicable oracle principle
- final-boundary policy enforcement
- cleanup failure visibility

### Mobile mandatory behavior

- root containment
- bounded list/search/read
- heuristic != hard filter
- static→runtime Observation pattern

### Exit gate

`SOURCE_CONFORMANCE_MATRIX` mandatory rows 100% green.

### 구현 결과 — Batch F

- CI/CD locked donor에서 domain/investigation/resolver/Frontier/coverage/WorkingMemory/validator/candidate
  mandatory focused suites를 재실행해 **94/94 PASS**를 확인했다.
- Financial locked donor에서 graph/oracle/execute/concurrency/engagement-policy/final-boundary/cleanup/
  termination focused suites를 donor를 수정하지 않는 ephemeral environment로 재실행해 **87/87 PASS**를
  확인했다.
- Mobile donor는 여전히 `tests/` 디렉터리가 없음을 숨기지 않는다. donor-read-only one-off
  characterization으로 root escape, list/search/read clamp, >2 MB search skip, read truncation,
  heuristic-not-filter, untrusted artifact, static→runtime verification contract를 재확인해
  `MOBILE_CHARACTERIZATION_PASS`를 얻었다.
- donor 3개는 검사 후에도 locked HEAD 그대로이며 worktree clean이다.
- `F-01`, `F-02`, `M-06`의 과거 partial 항목은 Batch F reference/conformance proof로 generic mechanism
  기준 COMPLETE가 됐다. 실제 금융 target adapter나 Mobile ADB adapter 구현은 domain/adaptor 범위이며
  generic core 완료 조건으로 거짓 포함하지 않는다.

---

# 18. Architecture Gates

## 18.1 Import gate

1. `redteam_core -> source project package` import 0
2. `redteam_core -> concrete domain package` import 0
3. `validation -> LLM/network/subprocess` import 0
4. `core model -> adapter` import 0

## 18.2 Domain contamination gate

AST/import/public-type 기준 core API에서 concrete vocabulary를 검사한다.

```text
GitHub Actions / GITHUB_TOKEN / Workflow / AWS OIDC
Account / Card / Merchant / Transaction / Money
APK / Activity / WebView / JADX / ADB
```

단 일반 영어 단어 raw grep만으로 false positive를 만들지 않는다. import path, type name, enum/value, semantic registration context를 함께 검사한다.

## 18.3 Determinism gate

다음 permutation에서 semantic output이 같아야 한다.

- registry registration order
- candidate input order
- relation input order
- resolver registration order
- repeated identical Observation

## 18.4 Fail-closed gate

다음 상태는 affirmative truth를 만들지 않는다.

- unknown schema
- unregistered relation semantics
- resolver timeout/forbidden/rate-limit
- missing validator material
- stale memory revision
- checkpoint fingerprint mismatch
- denied/missing policy capability
- unsafe retry ambiguity

---

# 19. 성능 / 비용 보존 기준

Generic refactor가 “구조는 예쁜데 더 느리고 비싸진” 결과가 되지 않게 다음을 별도 gate로 둔다.

1. genericization 자체가 mandatory LLM call을 추가하지 않는다.
2. registry/composition은 network/subprocess call을 하지 않는다.
3. Observation reapply는 expensive recompute를 의미 없이 반복하지 않는다.
4. Frontier/coverage는 explicit bounds를 유지한다.
5. acquisition은 bytes/files/hits budget을 유지한다.
6. 실제 wall-clock 성능 향상/저하는 benchmark 전에는 단정하지 않는다.
7. Phase 14에서 reference benchmark baseline을 기록해 후속 regression budget을 정한다.

즉 “더 빠르다”를 계획서에서 주장하지 않고, **추가 LLM call 0 / unbounded work 0 / 중복 semantic work 억제**만 구조적으로 보장한다.

Batch F의 최초 로컬 reference smoke baseline은 다음과 같다.

```text
tests/integration/test_batch_f_reference_domains.py
7 passed in 0.81s
```

이 수치는 현재 Windows 개발 호스트에서 얻은 **비규범적(non-normative) 회귀 참고값**이다. 하드 성능
SLO나 donor 대비 성능 우월 주장으로 사용하지 않으며, 환경/CPU/cache 변화가 있는 후속 실행에서는
동일 조건의 비교가 가능한 경우에만 regression signal로 사용한다.

---

# 20. 최종 Acceptance Gate

아래를 전부 통과해야 `Generic Core COMPLETE`로 표시한다.

1. stale generic `src/tests`가 제거되고 새 core로 재구성됨.
2. 세 source HEAD/hash가 extraction manifest에 고정됨.
3. 모든 donor symbol에 source file/symbol/commit/처리방식/test mapping이 있음.
4. Core public API에 CI/CD/Finance/Mobile concrete type 0.
5. Core에서 세 source package 직접 import 0.
6. Strict schema + stable semantic ID tests green.
7. TriState UNKNOWN 보존.
8. Observed evidence가 normative truth로 자동 승격되지 않음.
9. Heuristic hint가 truth/hard-filter 권한을 갖지 않음.
10. Unregistered RelationSemantics는 fail-closed non-traversable.
11. Frontier engine에 domain relation-name special-case 0.
12. Frontier 없이 state/invariant Candidate 생성 가능.
13. Candidate가 Finding보다 먼저 생성됨.
14. Finding→Candidate legacy normalization 경로 0.
15. Candidate status가 ClaimContract ceiling을 넘지 못함.
16. Root-cause multi-batch coverage가 전체 eligible roots를 terminal accounting함.
17. Domain impact vocabulary가 core scoring table에 하드코딩되지 않음.
18. Typed EvidenceRequirement가 Relation/Candidate에서 직접 발행 가능.
19. Requirement 당 0..N resolver 지원.
20. Resolver routing deterministic.
21. Resolver failure가 FALSE/SAFE evidence로 승격되지 않음.
22. Duplicate Observation은 semantic revision +0.
23. Observation apply 후 relation/candidate/memory/coverage recompute deterministic.
24. ArtifactBoundary path/symlink escape 차단.
25. bounded inventory/search/read budget 강제.
26. Candidate/LLM이 validator/oracle set을 축소하지 못함.
27. validator/oracle package에서 LLM/network/subprocess import 0.
28. path 없는 ExperimentResult도 validation 가능.
29. state-changing experiment는 ExecutionPolicy 없이 실행 불가.
30. final effect boundary에서 scope/effect/capability/concurrency 재검사.
31. race/high-impact capability는 explicit allow 없이 실행 불가.
32. `STARTED` 후 결과 불명 attempt는 `UNCERTAIN`; retry-safe proof 없이 자동 재실행 0.
33. cleanup required resource 누락/실패는 terminal output에 남음.
34. LLM output이 authoritative Evidence/Observation/Relation을 직접 만들 수 없음.
35. WorkingMemory stale CAS patch 거부.
36. bounded context projection만 LLM에 전달.
37. checkpoint semantic fingerprint mismatch resume 거부.
38. crash-cut matrix에서 duplicate authoritative commit 없음.
39. `reference_static_graph` 추가/변경에 core 수정 0.
40. `reference_stateful_experiment` 추가/변경에 core 수정 0.
41. `reference_artifact_runtime` 추가/변경에 core 수정 0.
42. composite bundle integration에 core 수정 0.
43. bundle registration order permutation 결과 동일.
44. `SOURCE_CONFORMANCE_MATRIX` mandatory behavior 100% green.
45. genericization 자체 때문에 LLM call count 증가 0.
46. registry/orchestration 자체 external I/O side effect 0.
47. source extraction 후 새 core가 원본 donor repo를 runtime dependency로 요구하지 않음.
48. test/reference fixture 이름을 바꾸거나 순서를 바꿔도 semantic IDs가 무관하게 안정적임.
49. domain bundle 제거 시 해당 domain registration만 사라지고 다른 bundle 결과는 변하지 않음.
50. 모든 architecture gate를 CI에서 자동 검사할 수 있음.
51. stale/fresh Observation이 구분되고 requirement freshness를 만족하지 않는 observation은 current truth로 사용되지 않음.
52. conflicting authoritative Observations가 silent last-write-wins로 소실되지 않음.
53. conflict policy로 해소할 근거가 없으면 dependent truth가 `UNKNOWN`/contradiction으로 남음.
54. ExperimentAttempt가 immutable `policy_hash`와 `semantic_manifest_hash`에 바인딩됨.
55. resume 시 policy hash 변경이 기존 authorization을 자동 승계하지 않음.
56. integration bundle dependency graph cycle이 startup에서 거부됨.
57. integration bundle이 sibling bundle private DomainState를 직접 import하지 않음.
58. public Protocol은 SPI promotion gate를 만족한 seam에만 존재함.
59. donor focused baseline test receipt가 source commit과 함께 남음.
60. Mobile donor에 test suite가 없다는 사실을 숨기지 않고 별도 characterization harness로 보완함.
61. raw sensitive evidence는 Evidence canonical object에 무제한 inline 저장되지 않음.
62. EvidenceStoragePolicy가 raw retention/redaction/export를 boundary에서 강제함.
63. LLM context에 sensitive raw artifact가 기본 노출되지 않음.
64. expected/normative evidence와 observed evidence channel이 분리됨.
65. LLM-inferred expectation이 trusted baseline으로 승격되지 않음.
66. root-cause identity가 bundle-qualified되어 cross-domain key collision이 없음.
67. 모든 validator outcome이 canonical state에 보존되고 대표 summary 때문에 유실되지 않음.
68. FindingMaterializer는 0..N Findings를 만들 수 있고 source outcome refs를 유지함.
69. untrusted artifact/tool text가 SYSTEM_POLICY instruction channel로 승격되지 않음.
70. trusted objective는 ranking/focus만 바꾸고 truth/policy/validator semantics는 못 바꿈.
71. bundle semantic implementation 변경은 implementation fingerprint에 반영됨.
72. `reference_request_response` 추가/변경에 core 수정 0.
73. request/response reference에서 403/404/error가 SAFE/FALSE authorization evidence가 되지 않음.
74. generic workspace reset 전에 absolute path + deletion inventory safety gate를 통과함.
75. freshness 판단이 explicit RuntimeClock/evaluation_time을 사용하고 hidden wall-clock dependency가 없음.
76. resolver completion order가 달라도 canonical semantic result가 동일함.
77. multi-Observation commit은 expected revision + atomic transaction을 사용함.
78. policy domain constraint는 registered typed schema이며 `dict[str, Any]` escape hatch가 없음.
79. `DomainStateEnvelope` payload를 core가 key-level로 직접 해석하지 않음.
80. source HEAD drift가 감지되면 extraction/conformance 재검증 없이 구현을 계속하지 않음.
81. normal/error/policy-stop/budget-stop/LLM-finish 모든 종료가 unified finalizer를 거침.
82. required cleanup failure가 final report/manifest에 명시됨.
83. semantic object ID와 operational attempt ID가 분리되고 random/timestamp가 semantic hash에 섞이지 않음.
84. retry-safe experiment는 idempotency/reconciliation 근거를 선언하며 state-changing default는 `NEVER_AUTOMATIC`임.
85. bundle dependency topological tie-break가 canonical bundle ID 기준이라 discovery order에 무관함.
86. Evidence/Expectation/Experiment action type ID가 registered payload schema와 연결됨.
87. designated EffectBoundary는 descriptor/policy/manifest에 바인딩된 ExecutionPermit 없이는 external effect를 호출하지 않음.
88. semantic/candidate/validator layer의 direct network/subprocess effect import가 architecture gate에서 거부됨.
89. 문서와 test가 trusted bundle code vs untrusted target/artifact/LLM data의 trust model을 명시함.
90. untrusted third-party plugin sandboxing을 현재 core가 제공한다고 과장하지 않음.

## 20.1 Batch F 최종 구현 closure

2026-09-08 Batch F 종료 시 위 90개 acceptance를 코드/architecture/reference/conformance gate에 다시
대조했다. 단순 체크리스트 서명만 한 것이 아니라 그 과정에서 `EvidenceStoragePolicy`가 실제 raw
artifact use boundary로 소비되지 않던 gap과 Expectation 기반 validation cache identity gap을 발견해
수정했다. 수정 후 full suite, four-reference/composite proof, donor conformance, hash-seed runtime gate를
재실행했으며 계획 범위의 mandatory acceptance는 현재 **90/90 green**이다.

이 COMPLETE는 실제 CI/CD/Finance/Mobile/Web 공격 adapter 구현 완료를 뜻하지 않는다. 그 구체
도메인 기능은 bundle/adapter 계층의 별도 작업이고, 여기서 COMPLETE라고 부르는 것은 이 문서가
정의한 **domain-independent Generic Core와 그 확장/안전성/conformance proof**다.

---

# 21. 구현 중 금지사항

- donor 세 프로젝트를 수정하면서 core를 만드는 것
- 파일 통째 복사 후 이름만 generic으로 바꾸는 것
- `dict[str, Any]`를 canonical domain payload 쓰레기통으로 쓰는 것
- core `CandidateKind`에 web/mobile/finance enum을 계속 추가하는 것
- `CoreState`에 domain field를 계속 추가하는 것
- Frontier를 모든 Finding의 필수 조건으로 만드는 것
- LLM-derived expected state를 trusted baseline으로 승격하는 것
- observed success를 entitlement/authorization 근거로 승격하는 것
- heuristic keyword hit를 Candidate/Finding truth로 승격하는 것
- resolver failure를 FALSE로 바꾸는 것
- Candidate가 validator set을 선택하도록 하는 것
- policy를 planner에서만 검사하고 final I/O boundary에서는 빼는 것
- retry ambiguity가 있는 state-changing experiment를 자동 반복하는 것
- plugin auto-discovery/package ecosystem을 실제 요구 전에 구현하는 것
- CodeQL/Joern/pluggy/OWASP 코드를 복사하는 것 — 외부 자료는 architecture evidence로만 사용한다.

---

# 22. 계획 자기평가 Rubric

| 평가축 | 배점 | 100점 조건 |
|---|---:|---|
| 도메인 독립성 | 20 | 세 source concrete semantics가 core contract/algorithm에서 제거되고 bundle-owned registry로 이동 |
| 기존 메커니즘 보존 | 15 | CI/CD/Financial/Mobile 필수 behavior가 Source Conformance Matrix로 보존 |
| Web/Mobile/Finance 확장성 | 15 | graph/state/experiment/artifact/request-response 형태가 core 수정 없이 표현 가능 |
| 타입 안정성 | 15 | strict model, typed registry/payload/codec, startup validation, Any-bag 금지 |
| migration 위험 | 10 | source snapshot, donor mapping, conformance, crash/fingerprint gate로 위험 통제 |
| 테스트/acceptance | 15 | 90 acceptance + reference 4종 + composite + source baseline/conformance |
| 과추상화 억제 | 10 | explicit composition, 작은 Protocol, source에서 필요가 증명된 seam만 구현 |

**100/100은 구현 품질 점수가 아니다. 계획이 현재 알려진 요구/위험을 acceptance gate까지 닫았는지에 대한 설계 점수다. 구현 후에는 실제 code/test/benchmark로 다시 채점한다.**

Acceptance 항목 90개는 반드시 90개의 개별 test function을 만들라는 뜻이 아니다. 하나의 architecture/property test가 여러 gate를 동시에 증명할 수 있다. **gate 수를 test framework complexity로 1:1 확대하지 않는다.**

---

# 23. 자기평가 반복 기록

같은 모델 내부의 blind-lens review이므로 별도 사람/별도 모델의 완전 독립 peer review라고 주장하지 않는다. 각 round는 앞 round의 점수에 맞추는 대신 서로 다른 failure lens로 계획 자체를 다시 공격했고, 외부 공식 문서/오픈소스 architecture와 current donor source를 근거로 결함을 수정했다.

| Round | 점수 | 주된 감점 사유 | 수정 |
|---|---:|---|---|
| R1 | 86/100 | multi-resolver conflict/freshness 부재, policy-attempt binding 부재, cross-domain integration 경계 부족, Protocol 과추상화 | conflict/freshness contract, immutable policy snapshot, integration bundle, SPI promotion gate 추가 |
| R2 | 94/100 | expectation/baseline core seam 불명확, validator outcome 축약 손실, sensitive evidence storage/prompt-injection 경계 약함, future Web proof 부족 | ExpectationRef, all-outcome preservation, EvidenceStoragePolicy, trust channels, request-response reference 추가 |
| R3 | 98/100 | implicit wall clock과 resolver completion order가 determinism을 깨뜨릴 가능성, typed policy constraint 미완, source drift 위험 | RuntimeClock, atomic canonical commit, PolicyConstraintRegistry, source HEAD drift gate 추가 |
| R4 | 99/100 | semantic experiment identity와 operational attempt identity/idempotency가 불명확, reference heading 일관성 | attempt/idempotency contract와 canonical bundle ordering 보강 |
| R5 | **100/100** | malicious plugin까지 sandbox한다고 오해할 수 있는 trust-model 과장 가능성, 일부 namespaced type ID의 schema binding 누락 | trusted-code threat model 명시, typed registries 완결, ExecutionPermit/effect-boundary 계약 추가 |

R5 이후 동일 rubric으로 다시 반증한 결과 **현재 계획 범위 내에서 추가 감점 사유를 찾지 못했다.** 이 100점은 위에 명시했듯 구현 결과가 아니라 plan completeness 점수다.

---

# 24. 최종 Mental Model

```text
                    Raw Targets / Artifacts
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
 semantic/static        bounded artifact       runtime/active
    frontend               frontend              frontend
       │                      │                      │
       └──────────────────────┼──────────────────────┘
                              ▼
                    Evidence / Observation
                              │
                              ▼
                 Canonical Knowledge / Relation
                              │
               ┌──────────────┴──────────────┐
               ▼                             ▼
          Graph Frontier               State / Invariant
               │                             │
               └──────────────┬──────────────┘
                              ▼
                     SecurityCandidate
                              │
                              ▼
               Root-Cause Coverage / Portfolio
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
                Resolver       Controlled Experiment
                    │                   │
                    └─────────┬─────────┘
                              ▼
                         Observation
                              │
                              ▼
                     Semantic Recompute
                              │
                              ▼
              ALL Applicable Validators / Oracles
                              │
                              ▼
                           Finding
```

최종적으로 기억할 문장은 이것이다.

> **CI/CD는 core의 canonical state / graph / candidate / coverage를 제공하고, Financial은 controlled experiment / policy / oracle을 제공하며, Mobile은 bounded acquisition / static→dynamic verification을 제공한다. 새 Generic Core는 셋 중 어느 하나의 domain model도 소유하지 않고, 셋에서 검증된 공통 메커니즘만 소유한다.**
