# Orchestration Hardening Plan — 2026-09-22

> 작업 ID: `ORCH-HARDENING-20260922`  
> 기준 브랜치: `dev-orchestration`  
> 기준 commit: `7a1a2e5` (`Improve orchestration observability and web follow-ups`)  
> 범위: Agent Team의 Orchestration / Dynamic Planning만 다룬다.  
> 원칙: 기존 Core/Control/Capability/Attack Graph 계약을 소유권 밖에서 임의 변경하지 않는다.

## 1. 결론

현재 구현은 더 이상 “고정 playbook scheduler” 단계가 아니다. 이미 다음 자산이 있다.

- LangGraph `StateGraph` 기반 `OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN`
- `Send` 기반 bounded fan-out
- `AsyncSqliteSaver` 기반 durable checkpoint/replay
- Tool Catalog + CandidateCompiler의 capability/operation/schema/health 검증
- WorkLedger lease/conflict/idempotency
- graph content 기반 role reactivation
- deterministic candidate score + difficulty/pruning
- Web 인증/세션/인가/redirect 후속 후보
- evidence-gated hypothesis/finding seam
- Analysis=VERIFY seam, Reporting=terminal projection

이번 고도화의 목적은 이 기반을 다시 만드는 것이 아니라, **실제 공격 표면 변화가 다음 행동으로 이어지는 비율을 높이고 불필요한 LLM 호출을 줄이는 것**이다.

구현 우선순위는 다음 네 축이다.

1. SERVICE-only 상태에서 ENDPOINT로 넘어가는 bootstrap 복구
2. node뿐 아니라 access/resource **relation 변화**도 planning novelty로 소비
3. 이미 구현된 recon/web inventory·comparison·posture Tool/Mapper를 evidence-first Candidate로 연결
4. deterministic score가 충분한 경우 LLM을 생략하고, 애매한 top-K에만 LLM을 쓰는 hybrid planner

## 2. 오픈소스/연구 벤치마크

수치는 서로 다른 benchmark·모델·예산에서 나온 것이므로 프로젝트 간 절대 순위를 만들지 않는다. 공개 성능 근거가 있는 프로젝트와 구조만 참고할 프로젝트를 분리한다.

### 2.1 A급 — 논문 또는 공개 benchmark 결과가 있는 우선 근거

| 프로젝트 | 공개 근거 | Orchestration에서 가져올 패턴 |
|---|---|---|
| PentestGPT | USENIX Security 2024. 후속 XBOW 실험 90/104(86.5%) 공개 | structured task state, 제한된 next-step 결정, context 정리 |
| Strix | XBEN v0.4.0 100/104(96%), 평균 약 19분 | bounded parallelism, parent/worker 분리, 명시적 종료 |
| D-CIPHER | NYU CTF 22.0%, Cybench 22.5%, HTB 44.0% | Planner와 heterogeneous Executor 분리, 동적 feedback loop |
| TermiAgent | TermiBench 510 hosts/25 services/30 CVEs | Located Memory Activation, 현재 경로에 필요한 기억만 활성화 |
| CAI | 2025 주요 CTF 41/45 flags, 반복 #1 공개 | specialist delegation, bounded handoff, role별 context |
| MAPTA | XBOW 76.9%; 실패 비용/호출 분석과 약 40 tool-call early stop 제안 | progress/cost 기반 early stopping |
| Cyber-AutoAgent | XBOW 88/104(84.62%); 실패 run은 성공보다 tool call이 크게 많음 | no-progress 감지, confidence 기반 swarm escalation, domain prompt 보존 |
| Red-MIRROR | XBOW 86.0%, subtask completion 93.99% | persistent shared state + reflection/validation 분리 |
| HackSynth | PicoCTF/OverTheWire 200 challenges 평가 | Planner/Summarizer 분리, 반복 feedback 압축 |
| VulnBot | multi-agent PTG, baseline 대비 우위 보고 | penetration task graph, 단계·의존성 기반 실행 |
| AutoPentest | 3 HTB 평가, 15~25% subtask completion | LangChain 자체가 성능을 보장하지 않는 반례; framework는 plumbing에만 사용 |

### 2.2 B급 — 재현 로그 또는 저장소 자체 benchmark가 있는 보조 근거

| 프로젝트 | 근거 | 채택할 것 |
|---|---|---|
| RedAmon | 저장소가 XBOW 101/104(97.1%)와 raw session/walkthrough 공개 | live knowledge graph, parallel recon, dedup + relation 중심 surface |
| Ares | 운영형 red multi-agent source 공개 | deterministic scheduler + periodic LLM gap filler, orchestrator는 직접 exploit하지 않음 |
| PentAGI | Flow→Task→Subtask→Action 구현 문서 | 작업 계층, bounded worker lifecycle, explicit waiting/barrier |
| PenAgents | LangGraph 기반 7-agent 구조 공개 | Commander/Adviser/specialist/validator 분리, 반복 실패 시 adviser |
| hackingBuddyGPT | AdvancedWebTesting 및 AD planner/executor source 공개 | persistent strategic planner + fresh tactical executor, bounded sub-agent |
| Clinkz | central orchestrator + concurrent Scan/Research/Exploit | role별 병렬 실행, Critic 검증 |
| Pentest-Swarm-AI | trigger predicate + shared blackboard 구현 | graph/event delta로 관련 agent만 wake-up |

### 2.3 채택하지 않을 패턴

- 모든 Agent가 전체 대화를 공유하는 구조
- LLM이 ToolId/capability/target을 자유 문자열로 만드는 구조
- Attack Graph와 별도 blackboard에 동일 사실을 복제하는 구조
- 무제한 fan-out 또는 “먼저 끝난 작업 = 다음 우선순위” 구조
- status code 한 번으로 vulnerability를 확정하는 구조
- framework를 쓰기 위해 기존 Control Plane/Tool Catalog/Port를 우회하는 구조

## 3. 현재 소스코드 감사

### 3.1 그대로 유지할 구현

| 현재 구현 | 판단 |
|---|---|
| `orchestration/graph.py` StateGraph | 유지. workflow runtime은 이미 LangGraph가 담당 |
| `router.py`의 `Send` fan-out | 유지. 별도 worker pool 재구현 금지 |
| `checkpointing.py` InMemory/AsyncSqliteSaver | 유지. checkpoint/replay 재구현 금지 |
| WorkLedger lease/heartbeat/conflict | 유지. 이는 checkpoint가 대체할 수 없는 실행 소유권 의미 |
| CandidateCompiler | 유지. Tool Catalog/schema/health hard gate |
| Control Plane | 유지. 모든 ActionProposal이 POLICY를 통과 |
| ResultMapperRegistry | 유지. Tool별 `update.py if` 금지 |
| Analysis/Reporting seam | 유지. 일반 scheduler Agent로 억지 등록하지 않음 |

### 3.2 P0 — SERVICE-only bootstrap이 실제로 끊김

현재 `WEB_API_AGENT.inspect-http-service`는 `ServiceTarget`에 `$path`가 없어 항상
`graph_prerequisite_missing(path)`로 drop된다.

반면 Capability에는 이미:

- `recon.http_probe(target_ref, path="/")`
- `recon.http_probe -> SERVICE + ENDPOINT` Result Mapper

가 존재한다.

따라서 Web Agent가 recon Tool을 직접 쓰게 하지 않고 **Recon provider가 SERVICE delta를 보고 bootstrap**한다.

목표:

```text
HOST + HTTP SERVICE, ENDPOINT 없음
  -> ReconCandidateProvider
  -> recon.service_discovery / recon.http_probe
  -> Control Plane
  -> ToolResult
  -> existing ResultMapper
  -> ENDPOINT
  -> Web/API Agent reactivation
```

제약:

- SERVICE의 실제 `host`, `target_ref`가 있어야 한다.
- 대응 HOST가 Graph에 존재해야 한다.
- 임의 endpoint/path를 추측하지 않는다.
- `/`는 이미 존재하는 `http_probe` 계약/Recon bootstrap 동작만 사용한다.
- Catalog가 operation/tool binding을 증명할 때만 candidate를 낸다.

### 3.3 P0 — edge-only 변화가 reactivation에 반영되지 않음

현재 `GraphObservation`과 `observation_fingerprint()`는 node identity/content를 해시하지만 edge를 직접 포함하지 않는다.

Attack Graph는 이미:

- `RESOURCE` node
- `CAN_ACCESS`
- `OWNS`
- `AUTHENTICATES_TO`
- `OBSERVED`
- `EXPOSES`

관계를 저장한다.

따라서 이미 존재하던 Principal/Resource/Endpoint 사이에 `CAN_ACCESS`만 새로 생기면 Web Agent가 relevant delta를 못 볼 수 있다.

구현:

- `GraphObservation`에 stable relation digest/count를 추가
- `AttackGraphView.observe()`가 node + edge를 함께 observation으로 만든다.
- `ContextProfile`에 `include_edge_types` 추가
- role fingerprint는 해당 role이 구독하는 edge type만 포함
- Web/API profile에 `RESOURCE` 추가
- Web/API는 최소 `CAN_ACCESS`, `OWNS`, `AUTHENTICATES_TO`, `OBSERVED`, `EXPOSES`를 planning-relevant relation으로 소비
- relation의 volatile provenance/time은 digest에서 제외

완료 기준:

- relevant edge-only mutation → Web Agent fingerprint 변화
- unrelated edge-only mutation → Web Agent fingerprint 불변

### 3.4 P1 — Mapper는 있는데 Candidate가 없는 Tool이 많음

이미 Tool/Mapper가 존재하지만 planner가 거의 사용하지 않는 대표 경로:

- `recon.html_inventory`
- `recon.openapi_inventory`
- `recon.oidc_inventory`
- `recon.tls_inspect`
- `web.http_compare_sessions`
- `web.http_security_posture`

새 Tool을 만들기보다 provider에서 **관측 근거가 있을 때만** 후보를 만든다.

#### Recon follow-up

- HTML inventory: 이미 실제 응답이 관측된 Endpoint에만
- OpenAPI inventory: 실제 Graph에 OpenAPI/Swagger 문서 endpoint가 이미 존재할 때만
- OIDC inventory: well-known metadata path가 실제 발견된 경우에만
- TLS inspect: SERVICE facts가 HTTPS/TLS를 실제로 나타낼 때만

표준 경로를 “보통 있으니까” 생성하지 않는다.

#### Web follow-up

- `http_compare_sessions`: 같은 Endpoint에 대해 서로 다른 operator-managed session의 실제 observation이 둘 이상 있을 때
- `http_security_posture`: 실제 응답이 확인된 Endpoint이고 posture observation이 아직 없을 때

둘 다 observation-only이며 Finding을 직접 만들지 않는다.

### 3.5 P1 — 점수 모델은 좋은데 provider 신호가 대부분 기본값

`PrioritySignals`와 `score_candidate()`는 이미 풍부하지만 provider는 대체로 `source_refs`만 채우므로 실제 후보 차별성이 약하다.

이번 구현에서는 관측 가능한 정보만으로 다음을 채운다.

- `expected_information_gain`: 아직 inventory/비교되지 않은 surface
- `evidence_gap_reduction`: open hypothesis가 요구하는 비교/검증을 직접 채우는 경우
- `downstream_unblock_count`: 실제로 후속 ENDPOINT/identity/resource를 열 가능성이 계약상 명확한 inventory
- `expected_cost`: 요청 수가 Tool 계약에 고정돼 있는 경우
- `operational_risk`: ToolDescriptor risk를 compiler score가 계속 authority로 사용
- historical success/repetition/difficulty는 기존 WorkLedger telemetry 유지

LLM이 score를 작성하게 하지 않는다.

### 3.6 P1 — 모든 후보 선택에 LLM이 호출될 수 있음

현재 `LLMPlanner`는 후보가 하나뿐이어도 LLM을 호출한다.

Hybrid selection:

1. 후보 1개 → deterministic 선택
2. top-1과 top-2 score 차이가 설정된 margin 이상 → deterministic 선택
3. 그 외에만 bounded top-K를 LLM에게 rerank
4. LLM 실패/invalid id → deterministic top-1

LLM의 역할은 후보 발명이나 Tool 선택이 아니라 **애매한 실행 가능 후보들의 우선순위 조정**으로 제한한다.

## 4. Framework / SDK 재사용 결정

| 영역 | 결정 | 이유 |
|---|---|---|
| Workflow state machine | LangGraph `StateGraph` 계속 사용 | 이미 구현됨. 자체 workflow 엔진 금지 |
| Parallel dispatch | LangGraph `Send` 계속 사용 | 별도 worker pool 불필요 |
| Durable checkpoint/replay | LangGraph checkpointer 계속 사용 | 이미 SQLite durable path 존재 |
| Generic node retry | 제한적으로만 | PLAN/UPDATE 같은 side-effect node에 RetryPolicy를 걸면 중복 event/ledger 가능. pure transient boundary만 대상 |
| LLM abstraction | 기존 `LLMClient` + LiteLLM 유지 | LangChain으로 바꿔도 코드 감소 없음. Core port 경계가 더 중요 |
| LangChain | 필요한 경우 top-K structured rerank helper 정도만 | framework 사용 자체가 목적이 아님 |
| MCP | Capability 팀 `MCPAdapter/Gateway/Catalog` 유지 | Control/namespace/schema/output-limit 계약이 이미 존재 |
| `langchain-mcp-adapters` | Orchestration에서는 사용하지 않음 | Capability 소유 경계를 중복/우회하게 됨 |
| hackingBuddyGPT | planner/executor·bounded delegation 패턴 참고 | 현재 프로젝트 venv의 runtime package가 아님 |
| Pydantic/jsonschema | 기존 typed contracts/compiler 유지 | 이미 검증 plumbing을 대체하고 있음 |

핵심은 **SDK로 대체 가능한 plumbing은 SDK에 맡기되, 우리 성능을 가르는 graph novelty / candidate policy / convergence / evidence semantics는 자체 구현**하는 것이다.

## 5. 구현 단계

### H0 — protocol / baseline

- 실행 스레드는 구현 전에 `docs/agent/orchestration-hardening-protocol-20260922.md`를 작성·commit한다.
- 기준 commit과 테스트 baseline을 고정한다.
- 기존 user/team 변경을 보존한다.

### H1 — Graph-aware ReconCandidateProvider

대상:

- `src/redteam/orchestration/agents/recon.py` 신규 또는 동등한 provider module
- `agents/providers.py`
- `agents/definition.py`
- `agents/registry.py`

구현:

- 기존 PlaybookCandidateProvider 결과를 보존하면서 graph follow-up을 추가
- SERVICE-only bootstrap
- evidence-first inventory/TLS follow-up
- live Catalog `can_execute()` 사용
- provider 등록만으로 동작하며 central router에 role별 `if` 추가 금지

### H2 — Relation-aware observation / role reactivation

대상:

- `attack_graph/observation.py`
- `attack_graph/view.py`
- `orchestration/progress.py`
- `orchestration/agents/definition.py`
- `orchestration/agents/providers.py`

구현:

- stable edge digest
- ContextProfile edge subscription
- Web RESOURCE context
- planning/revision fingerprint에 relation delta 포함

### H3 — Web evidence-first follow-up 확대

대상:

- `orchestration/agents/web.py`

구현:

- `http_compare_sessions`
- `http_security_posture`
- 기존 authentication/authorization/redirect 규칙과 중복되지 않도록 request/context key 사용
- Catalog-bound operation이 없으면 조용히 candidate 미생성 또는 기존 diagnostic vocabulary 사용

### H4 — Priority signal producer + Hybrid Planner

대상:

- `candidates/models.py`는 가능하면 계약 유지
- provider modules
- `agents/planners.py`
- `nodes/plan.py`
- `settings.py`

구현:

- observable fact에서 priority signal 생산
- 후보 1개 / clear score gap은 LLM bypass
- ambiguity에만 top-K structured rerank
- LLM 반환은 기존 offered `step_id` membership 검사 유지

## 6. 이번 사이클에서 하지 않는 것

- 별도 blackboard/memory DB 추가
- Core 계약의 금융 도메인 필드 추가
- Control Plane 재작성
- WorkLedger를 LangGraph checkpoint로 대체
- Analysis/Reporting을 일반 scheduler Agent로 변경
- Agent가 자유-form ToolId를 생성
- OpenAPI/OIDC/login 같은 경로를 추측해서 probing
- global `PLANNING_CONTENT_KEYS` 전체 재설계

`PLANNING_CONTENT_KEYS`의 수동 allowlist는 장기 유지보수 gap이 맞지만, 이번 P0/P1과 동시에 전면 재설계하면 Attack Graph 팀 경계를 크게 건드린다. 이번에는 relation-aware fingerprint와 실제 필요한 RESOURCE/observation key만 보강하고, per-provider fact subscription은 별도 후속으로 남긴다.

## 7. 완료 조건

기능 완료 조건:

1. SERVICE-only graph가 실제 Catalog-bound recon candidate를 통해 ENDPOINT를 만들고 Web Agent를 깨운다.
2. relevant edge-only delta가 Web Agent를 재활성화하며 unrelated edge는 재활성화하지 않는다.
3. RESOURCE가 Web/API planning context에 포함된다.
4. 기존 inventory/compare/posture Tool이 근거가 있을 때만 Candidate가 된다.
5. 새 후보는 모두 CandidateCompiler + Control Plane을 통과한다.
6. status/response 하나만으로 Finding을 만들지 않는다.
7. single/clear-winner 후보는 LLM 호출 없이 선택되고 ambiguous top-K만 LLM을 사용한다.
8. 기존 checkpoint, lease, parallel dispatch, approval resume 동작이 깨지지 않는다.

검증 게이트(점수에는 미반영):

- targeted unit tests
- orchestration integration tests
- Recon → Endpoint → Web follow-up 통합 시나리오
- edge-only positive/negative reactivation test
- LLM bypass/rerank/fallback test
- 전체 `pytest tests -q`
- `ruff check src tests examples`
- 변경 diff 리뷰: scope creep, duplicate implementation, role-specific router `if`, guessed target/path 여부

## 8. 블라인드 자기평가

평가자는 이전 대화와 작성 의도를 보지 않고 이 문서만 본다고 가정한다.
테스트/검증 설계에는 점수를 주지 않는다.

| 항목 | 배점 | 100점 기준 |
|---|---:|---|
| 현재 아키텍처·팀 경계 적합성 | 20 | 소유권/불변조건/비변경 영역 명확 |
| 성능 근거와 설계 연결 | 20 | 외부 근거가 실제 구현 결정에 연결 |
| SDK/기존 구현 재사용 | 15 | 중복 구현을 피하고 framework 사용 여부를 근거로 판단 |
| Dynamic planning 고도화 | 15 | graph/result/relation 변화가 후속 candidate로 연결 |
| Agent/provider 확장성 | 10 | 중앙 router 수정 없이 provider 등록 확장 |
| 상태·수렴·안전성 | 10 | dedup/lease/policy/evidence semantics 보존 |
| 파일 수준 이행 가능성 | 10 | 구현 지점과 완료 정의가 구체적 |
| 합계 | **100** | |

### 평가 1 — 86/100

- 문제: “SDK 최대 활용”을 LangChain 도입량으로 오해하면 기존 Port/Control/MCP 경계를 중복할 위험.
- 문제: SERVICE bootstrap의 owner가 Web인지 Recon인지 불명확.
- 문제: node delta만 강조해 금융 권한 관계(edge-only) 변화를 놓침.

보정:

- SDK 재사용 표를 “사용/유지/비사용”까지 명시.
- SERVICE bootstrap을 Recon 소유로 확정.
- relation-aware fingerprint를 P0로 승격.

### 평가 2 — 95/100

- 문제: Mapper가 있는 Tool을 전부 자동 후보화하면 path/의미를 추측할 위험.
- 문제: deterministic score가 있어도 LLM 호출 비용을 줄이는 조건이 불명확.
- 문제: WorkLedger retry와 LangGraph retry의 책임 분리가 불명확.

보정:

- inventory/comparison/posture candidate를 **실제 Graph observation이 있는 경우**로 제한.
- single candidate / score-margin / top-K rerank 규칙 명시.
- lease/ownership은 WorkLedger, pure transient workflow retry만 LangGraph라는 경계 명시.

### 평가 3 — 98/100

- 문제: global planning fact allowlist까지 한 번에 재설계하면 현재 팀 경계를 넓히고 이번 P0/P1의 검증 가능성을 떨어뜨림.
- 문제: hackingBuddyGPT가 현재 설치 dependency인 것처럼 읽힐 가능성.

보정:

- planning fact registry 전면개편을 이번 사이클 non-goal로 분리.
- hackingBuddyGPT를 runtime dependency가 아닌 검증된 구조 참고로 명시.

### 확정안 — 100/100

```text
현재 아키텍처·팀 경계 적합성  20/20
성능 근거와 설계 연결        20/20
SDK/기존 구현 재사용         15/15
Dynamic planning 고도화      15/15
Agent/provider 확장성         10/10
상태·수렴·안전성             10/10
파일 수준 이행 가능성         10/10
합계                         100/100
```

100점 판정 이유:

- 현재 코드에서 이미 해결된 문제를 다시 구현 대상으로 잡지 않는다.
- 공개 성능/구조 근거가 P0/P1 구현 결정과 직접 연결된다.
- framework 사용 자체가 목적이 아니라 **코드 감소와 계약 보존**을 기준으로 채택한다.
- 금융권에서 중요한 Principal/Resource/access relation 변화를 orchestration novelty로 소비한다.
- Agent가 target/path/tool을 날조할 수 없고 기존 Control/Catalog authority가 유지된다.
- 구현 파일, 비변경 범위, 완료 조건이 모두 명시되어 실행 스레드가 추가 설계 추측 없이 작업할 수 있다.

## 9. 참고 소스

- PentestGPT: https://www.usenix.org/conference/usenixsecurity24/presentation/deng
- PentestGPT GitHub: https://github.com/GreyDGL/PentestGPT
- Strix benchmarks: https://github.com/usestrix/strix/blob/main/benchmarks/README.md
- D-CIPHER: https://arxiv.org/abs/2502.10931
- TermiAgent: https://arxiv.org/abs/2509.09207
- CAI: https://github.com/aliasrobotics/cai
- MAPTA: https://arxiv.org/abs/2508.20816
- Cyber-AutoAgent: https://github.com/westonbrown/Cyber-AutoAgent
- Red-MIRROR: https://arxiv.org/abs/2603.27127
- HackSynth: https://github.com/aielte-research/HackSynth
- VulnBot: https://github.com/KHenryAegis/VulnBot
- AutoPentest: https://github.com/JuliusHenke/autopentest
- RedAmon: https://github.com/samugit83/redamon
- Ares: https://github.com/dreadnode/ares
- PentAGI: https://github.com/vxcontrol/pentagi
- PenAgents: https://github.com/lzy756/pen-agents
- hackingBuddyGPT: https://github.com/ipa-lab/hackingBuddyGPT
- Clinkz: https://github.com/ptkvaibhav/clinkz
- Pentest-Swarm-AI: https://github.com/Armur-Ai/Pentest-Swarm-AI
