# Complexity / Over-engineering Audit

**범위**: `src/redteam/{orchestration,capability,control,core,attack_graph,evidence,event_bus,intake,llm}`, `src/redteam/app`, `tests/`
**목적**: Runtime 연결성·dead code·pipeline 연결 여부는 조사하지 않음. 오직 "필요한 기능이라고 가정했을 때, 구현 방식(코드량·추상화 계층 수)이 과도한가?"만 평가.
**금지사항 준수**: 코드는 수정하지 않았음. 삭제/사용 여부에 대한 판단 없음.

---

## 1. Executive Summary

이 프로젝트는 연구/프로토타입 단계치고 **아키텍처 설계 자체는 대체로 합리적**이다. `control/{scope,roe,policy,rate_limit,budget}.py`처럼 서로 다른 알고리즘을 실제로 분리한 모듈, `llm/{fake,litellm}.py`처럼 실제 두 구현체만 존재하는 얇은 Protocol, `event_bus`처럼 실제 다수의 독립적 publisher/subscriber가 있는 pub/sub 등은 **과설계가 아니라 정당한 분리**다. 커스텀 예외 계층도 대부분 한 단계(`ValueError` 서브클래스 1개, 호출 지점도 1곳)에 그쳐 "ToolError→ExecutionError→CapabilityError" 같은 과도한 wrapping은 발견되지 않았다.

문제가 되는 복잡성은 "계층이 너무 많다"보다는 **"같은 책임을 서로 다른 방식으로 두 번 이상 구현했고, 그 두 구현이 이미 어긋나기 시작했다"**는 유형에 집중되어 있다. 구체적으로 다음 4가지가 반복되는 패턴이다.

1. **이중 구현(Divergent Duplication)** — 같은 질문("이 tool을 찾아라", "이 action을 실행하라", "이 agent가 뭘 할 수 있나")에 답하는 코드가 두 군데에 따로 있고, 이미 서로 다른 답을 내고 있다.
   - `MCPToolRegistry.list_tools_for_action` vs `MCPToolCatalog.list_tools_for_action` (operation_bindings 반영 여부가 다름)
   - `worker_node`의 손으로 짠 `policy→execute→verify→update` 시퀀스 vs 컴파일된 그래프의 동일 엣지
   - `AgentSpec.capabilities` vs `AgentDefinition.capabilities` (이미 `["web"]` vs `["web","api"]`로 불일치)
   - `task_graph/sqlite.py`(전체 재덤프 방식) vs `work/sqlite.py`(비즈니스 로직 재구현 방식) — durability를 구현하는 서로 다른 두 전략

2. **불필요하게 동적인 메커니즘(Reflection/Dynamic over Explicit)** — 정적 목록 하나면 될 것을 런타임 reflection/패키지 워크로 구성해서, 실패 시 조용히 잘못된 값(raw dict)을 반환하는 구조. (`checkpointing.py`의 `state_type_allowlist`)

3. **검증/기록 로직의 중복 실행** — 같은 데이터에 대해 같은 규칙을 두 계층에서 반복 검사하거나(mapper의 `_validate_mutation` vs service의 `build_properties`/`validate_properties`), 같은 노드에 "성공했는가"를 두 개의 독립된 writer가 각자 기록.

4. **복붙된 검증 헬퍼** — `observations/{recon_http,recon_inventory,recon_network,web_http,web_proxy}.py` 5개 파일에 `_safe_path`, `_HEX_64`, `_context_key` 같은 동일 로직이 문자 그대로 반복.

반대로, 우려했던 "Interface→AbstractBase→Adapter→Wrapper→Service→Manager→Registry→Factory" 식의 깊은 계층 체인은 **실제 tool 호출 경로에서는 나타나지 않는다** (Gateway→Adapter→Transport→Server, 4단계, 모두 필수). 즉 이 프로젝트의 복잡성 비용은 "계층이 너무 깊어서"가 아니라 "같은 로직이 여러 곳에 흩어져 손으로 동기화되고 있어서" 발생한다.

---

## 2. Top Over-engineering Findings

| Severity | Component | 문제 | 현재 구조 | 더 단순한 형태 |
|---|---|---|---|---|
| HIGH | `orchestration/nodes/worker.py`, `aggregate.py` | 병렬(fan-out) 실행 경로가 순차 경로의 노드 시퀀스와 집계 산술을 손으로 재구현 | `worker_node`가 `policy_node→execute_node→verify_node→update_node`를 직접 호출(그래프 엣지가 이미 이 순서를 인코딩); `aggregate_node`는 budget/재시도/검증-reserve 계산을 execute.py에서 "주석으로" 미러링 | 4-호출 시퀀스와 산술 로직을 공용 헬퍼 함수로 추출해 두 경로가 같은 함수를 호출 |
| HIGH | `capability/registry.py` vs `capability/catalog.py` | "capability+operation으로 tool 찾기" 쿼리가 두 곳에 독립 구현되어 이미 다른 결과를 냄 | `Registry.list_tools_for_action`은 operation_bindings 무시, `Catalog.list_tools_for_action`은 반영 (Registry 자신의 docstring이 "호환성용 중복"이라 인정) | Registry 버전 제거, Catalog만 필터링 담당 |
| HIGH | `orchestration/task_graph/sqlite.py` vs `orchestration/work/sqlite.py` | "메모리 저장소를 durable하게 만든다"는 동일 문제를 정반대 전략으로 두 벌 구현 | task_graph는 매 변경마다 전체 테이블 DELETE+재삽입; work는 충돌감지/의존성해석/lease만료를 SQL로 처음부터 재작성(이미 memory.py에 존재하는 로직) | 하나의 durability 전략(예: 공통 write-behind 패턴)으로 통일 |
| HIGH | `orchestration/checkpointing.py` (`state_type_allowlist`) | LangGraph 직렬화 허용목록을 `importlib`/`pkgutil`로 3개 패키지 전체를 reflection해서 동적 생성 | 알려지지 않은 클래스는 "조용히" raw dict로 되돌아감(자체 docstring이 실패 모드를 인정) | 알려진 ~30개 상태 타입의 명시적 tuple 하나로 고정 |
| HIGH | `orchestration/agents/base.py` + `definition.py` + `registry.py` | 동일 agent를 기술하는 두 모델(`AgentSpec`, `AgentDefinition`)이 각각 `capabilities`를 따로 선언하고 별도로 등록되어 이미 값이 어긋남 | `WEB_API_AGENT.capabilities == ["web"]`이지만 대응 `AgentDefinition.capabilities == ["web","api"]` | 하나의 모델로 병합하거나 `AgentDefinition`이 `AgentSpec`에서 파생 |
| HIGH (지엽) | `orchestration/observations/*.py` 5개 파일 | `_safe_path`, `_HEX_64`, `_context_key` 등 검증/키생성 로직이 문자 그대로 복붙 | recon_http/recon_inventory/recon_network/web_http/web_proxy 각각 동일 정규식·해시 로직 재정의 | 공용 `observations/_validate.py`(또는 `mapper.py`)로 추출 — 매핑 로직 자체는 tool별로 달라도 되므로 완전 통합은 불필요 |
| MEDIUM-HIGH | `orchestration/runner.py` | 체크포인트 재개 안전성 검증이 두 함수에서 거의 동일하지만 커버리지가 미묘하게 다르게 중복 구현 | `_register_pending_checkpoint`(489-521)와 `_resume_pending_approval`(552-568)이 각각 ~10줄짜리 검증 블록을 독립적으로 유지 | `_validate_resumable_snapshot(...)` 하나로 통합 |
| MEDIUM | `capability/gateway.py` | Gateway의 읽기 API(`get/list_by_capability/all/list_tools_for_action/list_candidate_metadata`)가 Registry/Catalog를 그대로 재노출하는 pass-through | 로직 추가 없이 docstring만 있는 forwarding 메서드 다수 | 호출자가 Catalog/Registry를 직접 참조하거나, Catalog를 Registry에 합쳐 단일 forwarding 대상으로 축소 |
| MEDIUM | `control/service.py` (`DefaultControlPlane`, ~630줄) | 파이프라인 평가, 승인 해석, durable-resume 부기, budget 차감, audit/evidence 부작용, risk 해석이 한 클래스에 집중 | 특히 durable-resume/rehydrate 관련 로직이 전체의 약 1/3(`rehydrate`, `register_pending_proposal`, stale-approval 처리) | resume/rehydrate 로직을 별도 협력 객체로 분리(상속이 아닌 합성) |
| MEDIUM | `orchestration/observations/mapper.py` + `attack_graph` upsert 경로 | 동일한 attrs/facts 검증(`validate_attrs`/`validate_facts`)이 mapper 계층과 service 계층(`build_properties`)에서 두 번 실행 | `_validate_mutation`(mapper)과 `build_properties`(service)가 같은 시크릿/형태 스캔을 반복 | 서비스 경계(`upsert_node`/`link`) 한 곳에서만 검증, mapper 쪽은 id/type 정합성만 확인 |
| MEDIUM | `orchestration/nodes/update.py` | 한 tool 결과에 대해 "성공했는가"를 두 개의 독립 writer(범용 `observed_attrs`/`last_operation_succeeded`, tool별 mapper의 status facts)가 각각 같은 노드에 기록 | 경로 두 개가 겹치는 성공/실패 의미를 따로 씀 | mapper가 성공 여부까지 포함해 mutation을 만들거나, mapper가 처리한 노드는 update_node가 자체 attrs를 건너뜀 |
| MEDIUM | `attack_graph/patterns.py` (`resolve`, ~230줄) | precondition 매칭을 위해 itertools 조합 기반의 완전한 CSP 백트래킹 solver(exclusion clique, 20,000-assignment budget)를 구현 | 실제 호출부는 1~3개 pattern, 적은 count로 추정되는데 production급 쿼리 플래너 로직 탑재 | 현재 사용되는 조합(`distinct_from`/`edge_type`)만 처리하는 단순 nested-loop/any() 체크 |
| MEDIUM | `orchestration/findings.py` → `core/convert.py` → `reporting.py` | Finding이 보고 가능해지기까지 5개의 서로 다른 타입을 거침 | `ToolResult→EvidenceRecord→PromotionRequest→Finding→FindingProjection`, 일부는 필드가 거의 동일 | `FindingProjection`을 없애고 `Finding`에 `.for_report()`류 메서드 추가 |
| MEDIUM | `orchestration/reporting.py` (7개 Pydantic 모델) | 런 종료 시 1회성 JSON 리포트를 위해 7개 모델(`RunEffectiveness` 등)을 새로 정의 | `RunEffectiveness`는 검증 로직 없는 4개 float — plain dict로 충분 | 4개 Mission-projection 클래스를 1~2개로 축소하거나 dict 구성으로 대체 |
| MEDIUM | `orchestration/context_projection.py` (`ContextProjectionCache`) | 그래프 프로젝션 캐싱을 위해 async Lock + OrderedDict + deep-copy + JSON 직렬화 키로 손수 LRU 구현 | 캐시 키 생성에 Pydantic 모델 전체를 `model_dump_json()`으로 직렬화 | 현재 규모에서는 캐시 없이도 충분하거나, eviction 없는 plain dict로 대체 |
| MEDIUM | `orchestration/agents/providers.py` | 동일 책임("candidate 제안")을 위한 두 개의 Protocol(`GraphCandidateProvider`/`CandidateProvider`)이 공존, 문서 자체가 하나만 "authoritative"라 명시 | 모든 concrete provider가 두 메서드를 모두 구현 | 하나의 메서드(옵션 context)로 통합, 두 번째 Protocol 제거 |
| MEDIUM | `orchestration/agents/providers.py` + `candidates/compiler.py` | "왜 아무 일도 안 일어났나"를 기록하는 거의 동일한 타입 3개(`CandidateDiagnostic`, `DraftDrop`, `CandidateRejection`) | 각각 다른 파이프라인 단계에서 생성되어 손으로 병합됨 | `stage` 필드를 가진 `DropReason` 하나로 통합 |
| MEDIUM | `orchestration/agents/planners.py` | `HybridPlanner`가 `LLMPlanner`를 상속하지만 실제로는 `plan()`을 완전히 오버라이드하고 부모의 ~90줄짜리 prompt 렌더러를 거의 복붙 | 상속이 속성 재사용 외에 이득이 없고, 60줄짜리 블록이 두 곳에서 각각 유지됨 | 공통 렌더링 로직을 함수로 추출해 양쪽이 호출, 상속 대신 합성 |
| MEDIUM | `capability/mobile_analysis_manifest.py` (209줄) | 외부 MCP 서버(mobile-next, apktool, jadx 등)의 스키마 드리프트를 매 부트스트랩마다 정밀 검증 | 정적 테이블 + `validate_discovered_schema`/`validate_configured_discovery`로 실시간 shape-diff | 리서치 단계에서는 불일치 시 경고 로그 1줄이면 충분 |
| MEDIUM | `control/risk.py` + `control/policy.py` | "이 tool의 risk가 threshold 이상이고 auto-approve ceiling 밖이면 사람 승인 필요"라는 하나의 결정을 6개 심볼(Protocol 1개 + 클래스 1개 + 함수 4개)로 분산 | Registry lookup용 Protocol+클래스까지 별도 정의 | Registry lookup을 callable로 주입받는 함수 하나(`risk_requires_approval`)로 통합 |
| MEDIUM | `control/severity.py` | 코어 계약에 severity 필드가 없어서 `PolicyDecision.matched_rules`에 `"severity:<value>"` 문자열로 우회 인코딩 | 별도 모듈 + `to_tag`/`severity_from_matched_rules` 왕복 변환 | 코어 계약에 severity 필드를 정식 추가(자체 docstring도 이를 "stopgap"으로 인정) |
| MEDIUM | `capability/bootstrap.py` (`_build_gateway`, ~110줄) | 서버별 transport/adapter/tool discovery/manifest 검증 결과를 6개의 병렬 dict 누산기로 관리 | `operation_bindings`, `server_concurrency`, `server_output_limits`, `output_schemas`, `target_types` 등을 각각 별도 dict로 threading | 서버당 하나의 dataclass(`ServerBuildResult`)로 통합 |
| MEDIUM | `orchestration/operator_intents/service.py` (~1000줄 단일 클래스) | 명령 처리(pause/resume/stop/focus 등)와 읽기 전용 프로젝션(`status`, `explain`, 3개 저장소 조회)이 한 클래스/파일에 혼재 | mutation 로직과 evidence-gathering 헬퍼(`_endpoint_matches` 등, ~370줄)가 뒤섞임 | `service.py`(명령)와 `projections.py`(조회/설명)로 분리 |
| MEDIUM | `orchestration/operator_intents/redaction.py` | 시크릿 키 이름 목록이 `core.redaction`, `core.contracts.events._SECRET_KEYS`와 별도로 세 번째 사본(`SECRET_KEY_PARTS`)으로 유지, 자체 docstring이 "수동 동기화 필요"라 인정 | 3벌의 시크릿-키 어휘를 손으로 동기화 | 하나의 공유 상수로 통합, 이 모듈은 dict/opaque-ref 래핑만 담당 |
| MEDIUM | `app/bootstrap.py` | `build_runtime_deps`와 `build_application` 두 개의 거의 동일한 컴포지션 함수가 병렬 유지(레거시 호환용으로 의도적) | 새 포트 추가 시 두 곳 모두에 반영해야 함 | `build_application`이 공통 부분에 대해 `build_runtime_deps`를 호출하도록 위임, 혹은 레거시 경로 정리 |
| MEDIUM | `tests/` 전반 | `conftest.py`가 전혀 없어 6개 이상 파일이 `mission()`/`task()`/`runtime()` 팩토리를 각자 재정의, 이미 시그니처가 서로 다르게 drift됨 | `test_operator_intents.mission()`은 `status:` 인자, `test_mission_task_graph.mission()`은 `revision:` 인자 | 공용 `tests/factories.py` 또는 `conftest.py` fixture로 통합(새 추상화 계층 추가 없이) |
| LOW | `core/ids.py` | 18개 `NewType` 문자열 별칭, 구성 시점마다 `XId(some_str)` 캐스팅이 반복되나 런타임 체크는 없음 | 타입체커만 위한 시각적 변환 단계 | 이 단계 프로젝트에서는 plain str + 타입힌트로도 충분(다만 비용 낮음) |
| LOW | `orchestration/state.py` | `TerminationReason`이 나머지 코드베이스와 달리 `str, Enum`이 아닌 평범한 클래스로 14개 상수를 나열 | 일관성 없는 패턴 | `core/enums.py`의 다른 enum들과 동일하게 `str, Enum`으로 전환 |
| LOW | `attack_graph/service.py` | `AttackGraphService`의 읽기 메서드 9/14개가 `self._view.X(...)`로의 순수 위임 | Public API 표면이 `AttackGraphView`의 두 배 | 호출자가 `service.view()`를 직접 쓰거나, 의도된 편의 API로 유지 |
| LOW | `attack_graph/{cleanup,retraction,snapshot}.py` | 각 78/94/92줄짜리 얇은 모듈(단순 CRUD, 단일 규칙 루프, JSONL 직렬화)이 파일 경계를 가짐 | 12개 파일 분할 중 이 3개만 독립 모듈로 유지할 근거가 약함 | `predicates.py`/`service.py`에 서브모듈로 통합 |

---

## 3. Abstraction Depth

### (a) Tool 호출 경로 — Agent가 실제로 MCP tool을 부르는 과정

```text
Agent
→ ToolGateway (Protocol)                         필수
→ MCPToolGateway.invoke()                        필수 (adapter dispatch, health gating, timeout, output cap)
→ MCPToolRegistry.get()                          필수 (단일 진실 공급원)
→ MCPToolCatalog.supports()                      합칠 수 있음/불필요 — 내부에서 Registry.get()을 다시 호출(동일 조회 중복)
→ (health path) MCPAdapter.health_check()        필수, 그러나 gateway 메서드 하나에 168줄로 뭉쳐 있음
→ MCPAdapter 서브클래스 (Web/Mobile/Recon/Analysis) 필수
→ MCPTransport (stdio/http/allowlist wrapper)    필수
→ 실제 MCP 서버 tool                              —
```
**핵심 판단**: 실질적으로 필요한 계층은 Gateway→Adapter→Transport→Server(4단계)뿐이다. Registry와 Catalog는 같은 데이터를 다른 두 규칙으로 필터링하는 중복 계층으로, 합쳐도 무방하다.

### (b) Agent 판단 루프 (순차 경로)

```text
AgentLoopRunner.run()
→ graph.ainvoke() (LangGraph 엔진)
→ observe_node
→ plan_node
→ route_after_plan → policy_node
→ route_after_policy → execute_node
→ _invoke_with_semaphore → tool_gateway.invoke(...)   ← 실제 side effect
```
약 7홉이며, 이는 LangGraph 구현상 각 홉이 실제 루프의 도메인 단계를 나타내므로 **적절한 수준**으로 평가됨(불필요한 wrapper 계층 아님).

**문제는 이 경로 자체가 아니라, 병렬(fan-out) 실행을 위한 두 번째 경로가 별도로 존재한다는 점**이다:

```text
worker_node (하나의 함수 안에서 수동으로)
→ policy_node
→ execute_node
→ verify_node
→ update_node
```
이 4단계 시퀀스는 컴파일된 그래프의 엣지(`execute→verify→update`)가 이미 인코딩하고 있는 것과 동일한데, worker 경로는 이를 손으로 재현하고 있어 "필요 이상의 계층"이 아니라 "같은 계층 구조를 두 번 유지"하는 문제다.

### (c) 결과 수신 → 상태 갱신 (Observation → AttackGraph)

```text
ToolResult (raw dict)
→ map_recon_http_probe() 등 mapper 함수 (NodeMutation/EdgeMutation 생성, 자체 검증)
→ ResultMapperRegistry.map_result() → _validate_mutation() (attrs/facts 재검증 — 중복)
→ update_node (nodes/update.py) → AttackGraphService.upsert_node/.link
→ _normalize_properties → build_properties (attrs/facts 재검증 — 재중복) → merge_properties
   (_merge_attrs/_merge_facts/_merge_cleanup/_cap_provenance)
→ GraphNode.model_copy

(별도 경로) 동일 ToolResult
→ tool_result_to_evidence() → EvidenceRecord (거의 1:1 필드 복사)

(또 별도 경로) update_node의 observe_target()
→ 같은 노드에 독립적으로 last_operation_succeeded 등 기록
```
하나의 HTTP probe 결과가 "키/값 몇 개"를 표현하기 위해 5~6번의 형태 변환을 거치고, 검증 로직이 두 계층에서 중복 실행되며, "성공 여부"를 두 개의 독립된 writer가 각각 기록한다. 각 변환이 실제 의미를 더하는지 기준으로 보면, mapper 계층의 재검증과 update_node의 이중 기록은 제거 가능하다.

### (d) Finding/Report 파이프라인

```text
ToolResult → EvidenceRecord (core/convert.py) → PromotionRequest (findings.py)
→ Finding (contracts) → FindingProjection (reporting.py)
```
5개 타입 중 `Finding`과 `FindingProjection`은 title/severity/verified/evidence_ids를 거의 동일하게 보유 — 마지막 변환은 의미 추가가 적다.

### (e) Agent/Candidate 생성

```text
PlanStep (agents/base.py) → CandidateDraft (candidates/models.py) → ExecutableCandidate (candidates/models.py)
→ CandidateBatch (providers.py)
```
각 단계는 실제 내용(대상 바인딩 → tool 바인딩+점수)을 더하므로 순수 pass-through는 아니지만, 여기에 "왜 안 됐는가"를 기록하는 3개의 유사 타입(`CandidateDiagnostic`/`DraftDrop`/`CandidateRejection`)까지 더하면 "제안된 action 하나"를 표현하는 데 실질적으로 8개 타입이 관여한다.

---

## 4. Excessive Boilerplate

| 유형 | 사례 |
|---|---|
| **Wrapper/Pass-through** | `capability/gateway.py`의 `get/list_by_capability/all/list_tools_for_action/list_candidate_metadata` (Registry/Catalog 그대로 재노출); `control/approval.py`의 `get/list_pending`, `control/audit.py`의 `entries_for_run/entries_for_action` (단, 실제 2개 백엔드가 있어 LOW로 평가); `attack_graph/service.py`의 9개 읽기 메서드가 `self._view.X()` 순수 위임 |
| **중복 검증(Duplicate Validation)** | mapper의 `_validate_mutation` vs service의 `build_properties`/`validate_properties` — 동일 attrs/facts 규칙을 두 번 실행 |
| **중복 조회(Duplicate Lookup)** | `MCPToolCatalog.supports()`가 이미 gateway가 가져온 descriptor를 Registry에서 다시 조회 |
| **복붙된 헬퍼(Copy-pasted Helper)** | `observations/*.py` 5개 파일의 `_safe_path`, `_HEX_64`, `_context_key`/`_comparison_key` |
| **복붙된 상속(Copy-pasted via Inheritance)** | `HybridPlanner`가 `LLMPlanner`를 상속하되 90줄 prompt 렌더러를 사실상 재작성 |
| **병렬 컴포지션 함수(Parallel Bootstrap)** | `app/bootstrap.py`의 `build_runtime_deps`와 `build_application` |
| **병렬 검증기(Parallel Validator)** | `runner.py`의 `_register_pending_checkpoint`/`_resume_pending_approval` |
| **DTO 변환 체인** | `ToolResult→EvidenceRecord→PromotionRequest→Finding→FindingProjection`; `PlanStep→CandidateDraft→ExecutableCandidate→CandidateBatch` |
| **테스트 팩토리 중복** | `mission()`/`task()`/`runtime()`이 6개 이상 테스트 파일에 각자 재정의, 이미 시그니처 drift |
| **시크릿 키 목록 3중화** | `core.redaction`, `core.contracts.events._SECRET_KEYS`, `operator_intents/redaction.py`의 `SECRET_KEY_PARTS` |

---

## 5. Data Model Complexity

- **ID 타입**: `core/ids.py`의 18개 `NewType`은 런타임 체크가 없어 매 생성 지점의 `XId(str)` 캐스팅이 순수 의례적(ceremony)이지만 비용은 낮음(LOW).
- **Enum vs 평범한 클래스 불일치**: `TerminationReason`은 14개 값을 가진 평범한 클래스인데, 나머지 `core/enums.py`는 모두 `str, Enum`을 사용 — 기술적 이유 없는 스타일 불일치.
- **의미가 겹치는 Enum 값**: `GraphEdgeType`의 `RELATED_TO`/`SUPPORTED_BY`/`DERIVED_FROM`/`OBSERVED`는 문서화된 구분 없이 대체로 "이 노드가 저 노드에 영향을 줬다"는 유사 의미 — 1~2개로 축소 가능해 보임.
- **DTO↔Domain↔Event 반복 변환**: Finding 파이프라인(5단계)과 Candidate 파이프라인(4~8단계)이 대표 사례. 각 홉이 실제 의미를 더하는지 기준으로 보면 마지막 프로젝션 단계(`FindingProjection`, `CandidateBatch`)는 병합 여지가 있음.
- **1회성 보고용 모델 과다**: `reporting.py`의 7개 Pydantic 모델 중 `RunEffectiveness`는 검증 로직 없는 4개 float — plain dict로 충분.
- **거의 동일한 3개 "실패 이유" 타입**: `CandidateDiagnostic`/`DraftDrop`/`CandidateRejection` — 파이프라인 단계별로 다른 이름을 가진 것 외 구조가 동일.
- **같은 사실의 3중 선언**: agent의 capability/operation이 `AgentSpec.capabilities`, `AgentDefinition.capabilities`, provider-local `declared_operations` tuple로 세 가지 다른 shape으로 반복 — 단일 소스 부재로 이미 drift 발생.
- **시크릿 키 목록 3중화**(위 4절 참고)도 데이터 모델이라기보다 "같은 상수를 여러 곳에 재정의"하는 동일 패턴.

**과설계가 아닌 것**: `control` 모듈의 개별 정책 모델(scope/roe/policy/rate_limit/budget)은 실제로 서로 다른 알고리즘을 표현하며, 1필드 dataclass나 의미 없는 세분화 사례로 지목할 것은 발견되지 않음. `core/enums.py`의 `Verdict`/`ExecutionStatus` 등도 문서화된 구분이 명확함.

---

## 6. Control Flow Complexity

- **Strategy/Factory가 사실상 1종류만 생성하는 사례**: 없음으로 확인됨. `deps.agents`/`deps.candidate_providers`/`deps.result_mappers` 레지스트리는 실제로 여러 concrete provider(recon/web/playbook 등)를 등록하고 있어 정당.
- **Event Bus**: `event_bus`(3파일, 179 LOC)는 직접 호출보다 이점이 없는 장식이 아니라, 실제로 서로 다른 3곳의 독립적 구독자(`app/cli/commands/run.py`의 라이브 렌더링, `orchestration/wakeup.py`의 이벤트 기반 wake-up)와 다수의 발행자가 존재 — **정당한 pub/sub**.
- **이중 구현되는 라우팅/실행**: 3(b)에서 설명한 순차 vs 병렬 경로 중복이 이 항목의 핵심 발견.
- **문서와 실제 책임의 불일치**: `router.py`는 자신을 "단순 번역기"라 문서화하지만, `route_after_plan`은 실제로 fan-out 여부(`len(dispatch_items) > 1`)를 결정하는 로직을 담고 있음 — 로직 자체는 단순해 비용은 낮지만(LOW), 독자가 오해하기 쉬움.
- **이름 충돌**: `router.py`의 `END = "end"`(단순 문자열)와 `langgraph.graph.END`(실제 종료 sentinel)가 같은 이름으로 다른 객체를 가리켜 두 파일을 나란히 읽을 때 함정이 됨(LOW).
- **DI/부트스트랩**: `app/bootstrap.py`는 DI 프레임워크 없이 수동 생성자 주입을 사용하는 것 자체는 프로토타입에 적절하나, `build_runtime_deps`/`build_application` 두 함수가 병렬 유지되어 새 포트 추가 시 두 곳 모두 수정해야 하는 비용이 있음(MEDIUM).
- **CSP 백트래킹 solver**(`attack_graph/patterns.py`)는 이 코드베이스에서 가장 단순 대비 구현 복잡도가 큰 control-flow 사례.

---

## 7. Test Complexity

- **tests/fakes/*** (5파일, 252 LOC): 대체 대상 production 코드보다 명백히 작고 단순함(예: `FakeControlPlane` ~40줄 vs `DefaultControlPlane`+audit+approval+risk resolver). **과설계 없음** — 오히려 모범적인 크기.
- **샘플 테스트 파일** (`test_operator_intents.py` 1004줄, `test_orchestration_nodes.py` 1203줄 등): setup은 평범한 함수형 팩토리(`mission()`, `task()`, `runtime()`, `publisher()`, `deps()`)이며, 4개 파일 중 `@pytest.fixture`는 단 1개. Builder/DSL, nested setup, 커스텀 assertion 계층 없음. 파일 길이는 시나리오 수(이중언어 파싱 케이스, 이중 백엔드 등)에 비례하며 scaffolding 비용이 아님 — **과설계 아님**.
- **실제 발견된 문제**: `tests/` 전체에 `conftest.py`가 하나도 없어(확인됨: 0건), 6개 이상 파일이 `mission()`/`task()`/`runtime()` 팩토리를 독립적으로 재정의하고 있고, 이미 시그니처가 서로 다르게 벌어짐(`test_operator_intents.mission()`은 `status:` 인자, `test_mission_task_graph.mission()`은 `revision:` 인자). 이는 "과도한 추상화"가 아니라 "공유 계층 부재로 인한 중복과 drift"이며, 새로운 추상화 계층 없이 공용 `tests/factories.py`/`conftest.py`로 해결 가능(MEDIUM).

---

## 8. Simplification Candidates

**(1) MCP Tool 조회**
```text
현재 구조:
Agent → MCPToolGateway → MCPToolRegistry.get() → MCPToolCatalog.supports()(Registry 재조회) → Adapter → Transport → Server

실제 책임:
capability+operation으로 tool descriptor 찾기 + 호출

단순화 가능 형태:
Agent → MCPToolGateway → MCPToolRegistry(+Catalog 통합) → Adapter → Transport → Server

제거/통합 가능한 개념:
Catalog의 list_tools_for_action 중복 구현(Registry 버전 제거 또는 Catalog로 단일화)

기대 효과:
- 두 구현이 다른 답을 내는 버그 가능성 제거
- 어느 것이 "진짜" 조회 경로인지 명확해짐
```

**(2) 순차/병렬 실행 경로 통합**
```text
현재 구조:
[순차] graph edges: policy→execute→verify→update
[병렬] worker_node 내부에서 동일 4단계를 수동 호출 + aggregate_node가 execute.py의 산술을 미러링

실제 책임:
"리스한 action 하나를 policy→execute→verify→update까지 실행하고 결과를 집계"

단순화 가능 형태:
공용 run_action_sequence() 헬퍼를 그래프 엣지와 worker_node가 함께 호출

제거/통합 가능한 개념:
worker_node의 수동 시퀀스, aggregate_node의 산술 재구현

기대 효과:
- 순차/병렬 경로 간 동작 불일치 위험 제거
- 디버깅 시 로직을 한 곳에서만 확인
```

**(3) Task/Work 이중 저장소 통합**
```text
현재 구조:
task_graph/{memory,sqlite}.py + work/{memory,sqlite}.py — 4개의 독립 backend, 서로 다른 durability 전략

실제 책임:
lease/상태-머신 기반 레코드를 메모리 또는 durable하게 저장

단순화 가능 형태:
하나의 저장소 추상화(하나의 durability 전략)를 두 도메인(Task/Work)이 공유

제거/통합 가능한 개념:
work/sqlite.py의 SQL 재구현 로직(이미 memory.py에 존재) 또는 task_graph/sqlite.py의 전체-재덤프 전략 중 하나

기대 효과:
- 코드량 대폭 감소(4 backend → 2 backend 이하로 추정)
- 두 전략을 손으로 동기화할 필요 제거
```

**(4) Agent/Candidate 메타모델 통합**
```text
현재 구조:
AgentSpec.capabilities + AgentDefinition.capabilities + provider-local declared_operations(3중 선언, 이미 drift)
+ GraphCandidateProvider/CandidateProvider(중복 Protocol)
+ CandidateDiagnostic/DraftDrop/CandidateRejection(3개 유사 타입)

실제 책임:
"이 agent가 무엇을 할 수 있고, 어떤 candidate를 제안했으며, 왜 일부는 버려졌는가"

단순화 가능 형태:
capability/operation 단일 소스 + propose() 메서드 1개 + DropReason(stage 필드 포함) 1개

제거/통합 가능한 개념:
AgentDefinition의 독립 capabilities 필드, CandidateProvider Protocol, DraftDrop/CandidateRejection

기대 효과:
- 이미 발생한 drift(capability 불일치) 재발 방지
- "왜 이 action이 안 나왔나" 추적 시 참조할 타입이 1개로 감소
```

**(5) Observation 검증 헬퍼 공유화**
```text
현재 구조:
recon_http/recon_inventory/recon_network/web_http/web_proxy.py 각각에 _safe_path, _HEX_64, _context_key 복붙

실제 책임:
문자열/다이제스트 형태 검증 + 결정론적 dedup 키 생성

단순화 가능 형태:
observations/_validate.py(또는 mapper.py)에 공용 헬퍼 정의, 5개 파일은 이를 import

제거/통합 가능한 개념:
5벌의 동일 정규식/해시 로직

기대 효과:
- 코드량 감소
- 검증 규칙 변경 시 5곳 대신 1곳만 수정
```

---

## 9. Complexity Hotspots

| Component | 요구사항 난이도 | 구현 복잡도 | 불균형 | Severity |
|---|---:|---:|---:|---|
| MCP Tool 조회 (Gateway/Registry/Catalog) | Low | Medium | O | HIGH |
| 순차/병렬 실행 경로 중복 (worker/aggregate) | Medium | High | O | HIGH |
| Task/Work 이중 저장소 스택 | Medium | High | O | HIGH |
| Checkpointing 리플렉션 허용목록 | Low | Medium-High | O | HIGH |
| Agent/Candidate 메타모델(3중 capability 선언) | Low | Medium | O | HIGH |
| Observation 결과 → AttackGraph 변환 체인 | Medium | High | O | MEDIUM-HIGH |
| Observation 검증 헬퍼 복붙(5파일) | Low | Low(단, ×5 반복) | O | HIGH(지엽) |
| Control Plane 단일 클래스(630줄) | Medium | Medium-High | 부분 O | MEDIUM |
| Operator Intent Service(1000줄 단일 클래스) | Medium-High | High | 부분 O(주로 파일 분리 문제) | MEDIUM |
| attack_graph 패턴 매칭(CSP solver) | Low | High | O | MEDIUM |
| Finding/Report DTO 체인 | Low | Medium | O | MEDIUM |
| 테스트 팩토리 중복(conftest 부재) | Low | Low(반복으로 유지비 증가) | O | MEDIUM |
| control/{scope,roe,policy,rate_limit,budget} | Medium | Medium | X | — (적절) |
| LLM Provider 계층(fake/litellm) | Low | Low | X | — (모범 사례) |
| Event Bus | Low | Low | X | — (적절) |
| tests/fakes | Low | Low | X | — (적절) |

---

## 반드시 답해야 할 질문

**Q1. "10~30줄 정도의 단순 로직으로 해결할 수 있는 일을 여러 클래스/파일/추상화 계층으로 구현한" 대표적인 사례는?**

- `MCPToolRegistry.list_tools_for_action` + `MCPToolCatalog.list_tools_for_action`: 본질은 "capability+operation으로 dict 필터링" 20~30줄짜리 함수인데, 두 클래스가 서로 다른 규칙으로 중복 구현.
- `checkpointing.py`의 `state_type_allowlist`: 알려진 ~30개 클래스의 고정 tuple이면 될 것을 `importlib`/`pkgutil`/`inspect`로 3개 패키지 전체를 reflection.
- `observations/*.py`의 `_safe_path`/`_HEX_64`/`_context_key`: 10~20줄짜리 공용 헬퍼 하나면 될 것이 5개 파일에 복붙.
- Agent capability 선언: `AgentSpec`, `AgentDefinition`, provider-local tuple 세 곳에 "이 agent가 뭘 할 수 있는가"라는 한 가지 사실을 반복 선언 — 본질적으로 하나의 list 필드면 충분.

**Q2. 어떤 abstraction이 실제 flexibility를 제공하고, 어떤 것은 미래 가능성만을 위해 존재하는가?**

- **실제 flexibility 제공**: `control/{scope,roe,policy,rate_limit,budget}.py`(5개의 실제로 다른 알고리즘), `llm/{fake,litellm}.py` + `core/ports/llm.py` Protocol(실제 테스트/운영 두 구현), `event_bus`(실제 다수의 독립 구독자), `capability/approval.py`·`audit.py`의 InMemory/Filesystem 스토어(실제 두 백엔드).
- **미래 가능성만을 위해 존재(YAGNI 후보)**: `task_graph`/`work`의 memory 백엔드가 sqlite와 나란히 "스위칭 가능한 두 번째 구현"으로 존재하지만, 실제로는 두 durability 전략이 서로 다른 방식으로 같은 문제를 풀고 있어 유연성보다는 유지비용을 늘림; `GraphCandidateProvider`/`CandidateProvider` 중복 Protocol(문서 자체가 하나만 "authoritative"라 인정); `attack_graph/patterns.py`의 CSP 백트래킹 solver(현재 사용 규모에 비해 과한 일반화); `mobile_analysis_manifest.py`의 정교한 contract-drift 검증(검증되지 않은 외부 MCP 서버에 대한 production급 방어).

**Q3. 새로운 개발자가 실제 Agent Tool Calling 흐름을 따라가기 어렵게 만드는 구조는?**

- 순차 실행 경로(그래프 엣지)와 병렬 실행 경로(worker_node의 수동 시퀀스)가 이중으로 존재해, "policy→execute→verify→update"가 그래프 정의와 worker 함수 두 곳에 따로 나타남.
- `registry.py`와 `catalog.py`가 같은 질문에 서로 다른 답을 낼 수 있어, 코드만 보고는 어느 경로가 "진짜"인지 알기 어려움.
- `router.py`가 스스로 "번역만 한다"고 문서화했지만 실제로는 fan-out 여부를 결정 — 문서를 신뢰하면 실제 책임 소재를 놓치게 됨.
- `AgentPhase` enum(도메인 어휘)과 `router.py`의 문자열 상수(LangGraph 엣지 이름)가 이름이 다른 두 어휘로 매핑되어 있어, 흐름 추적 시 두 파일을 대조해야 함.

**Q4. 기능을 유지하면서도 코드량과 개념 수를 가장 크게 줄일 수 있는 영역은?**

우선순위 순: (1) Task/Work 이중 저장소 스택 통합 — 구조적으로 가장 큰 절감 영역, (2) 순차/병렬 실행 경로의 공용 헬퍼화, (3) Agent/Candidate 메타모델 통합(capability 3중 선언 + 3개 유사 rejection 타입), (4) `observations/*.py` 검증 헬퍼 공유화.

```text
[Task/Work 저장소]
현재: 4 backend (task_graph memory+sqlite, work memory+sqlite), 각각 독립된 CRUD+동기화 로직
실질 책임: lease 기반 레코드의 durable 저장 하나
예상 최소 구조: 공용 저장소 전략 1벌 + Task/Work 각각의 얇은 도메인 레이어 (backend 수는 줄일 수 있으나
정확한 최종 파일 수는 실제 리팩터링 전까지 확정하기 어려워 추측하지 않음)

[MCP Tool 조회]
현재: Registry + Catalog 2개 클래스가 유사 쿼리를 중복 구현
실질 책임: tool lookup + capability/operation 필터링
예상 최소 구조: 1개 클래스(Catalog 기능을 Registry에 흡수)
```

---

*본 보고서는 Runtime 연결성/Dead code/Pipeline 연결 여부를 판단하지 않았으며, 코드를 수정하지 않았습니다.*
