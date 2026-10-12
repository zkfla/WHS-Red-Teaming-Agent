# Runtime Pipeline Audit — WHS-Red-Teaming-Agent

> 목적: "실제로 동작하는 Red Teaming Agent를 만드는 데 필요한 코드인가"의 관점에서
> 현재 구현이 실제 실행 파이프라인에 연결되어 있는지 진단한다. **코드는 수정하지 않았다.**
>
> 판정 기준: `정의됨 → 생성/등록됨 → 호출됨 → 결과가 소비됨 → 다음 단계에 영향`.
> 근거는 모두 `파일:라인` 코드 reference로 제시한다.
> 감사 대상 브랜치: `dev` (HEAD `f849d29`, `origin/dev` 최신 기준).
>
> 주의: 루트 `app.yaml`, `demo-engagement.yaml`은 `dev`에 커밋된 파일이 아니라 워킹트리에
> 남아있는 untracked 파일이다(다른 브랜치 작업 잔재). `dev`에 tracked된 동등 근거는
> `examples/app.yaml`(`provider: fake`, `mcp_servers: []`)이며, 결론은 동일하다.

---

## 1. Executive Summary

**기본 Agent Loop는 end-to-end로 연결되어 있고 실제로 닫힌다.** `redteam run` →
`build_application` → `run_engagement` → `AgentLoopRunner`(LangGraph 9-node) →
`settle_engagement` → `ReportingAgent` 경로가 전부 실제 호출로 이어지며, OBSERVE →
PLAN → POLICY → EXECUTE → VERIFY → UPDATE → REPLAN 루프가 그래프/증거를 갱신하고
다음 turn이 그것을 다시 읽는다.

그러나 **"실제로 실행되는 런타임"과 "구현되어 있는 런타임"이 크게 다르다.** 저장소에
포함된 모든 설정(`app.yaml`, `examples/app.yaml`)은 다음으로 고정되어 있다:

- LLM: `provider: fake` → `FakeLLMClient` (`app/bootstrap.py:110-114`)
- MCP: `mcp_servers: []` → `ExampleMCPAdapter` (`capability/bootstrap.py:95-107`).
  이 어댑터의 유일한 툴은 인자를 그대로 되돌려주는 `recon.ping` echo 툴이다
  (`capability/adapters/mcp/example.py:22,58-67`).

즉 **현재 "동작"하는 파이프라인은 가짜 LLM + 인프로세스 echo 툴로 도는 vertical slice**이며,
실제 스캐너(web/recon/mobile/zap/nuclei) 및 실제 LLM(litellm)으로 도는 경로는 코드로는
완비되어 있으나 **어떤 shipped config로도 활성화되지 않는다.** CLI 자체가 이 상태를
"development/test only; no real tool execution" 경고로 표시한다
(`app/cli/commands/run.py:551`, `renderer.py:399`, `commands/tools.py:50`).

또한 기본 설정에서 다음 두 개의 대형 서브시스템이 **경로상 꺼져 있다**:

- **병렬 fan-out (WORKER/AGGREGATE 노드 + DispatchItem map/reduce)** — `max_actions_per_turn`
  기본값이 `1`이라 `dispatch_items`가 1개를 넘지 못하고, 순차 POLICY 경로만 탄다
  (`settings.py:66`, `nodes/plan.py:751`, `router.py:route_after_plan`).
- **LLM 후보 생성 (`LLMCandidateProvider`)** — `llm_candidate_generation` 기본값이 `False`
  라서 plan 노드에서 호출되지 않는다 (`settings.py:93`, `nodes/plan.py:186`).

순수 orphan(런타임 어디서도 import되지 않고 테스트만 참조)은 소수다: `evidence/validation.py`,
`orchestration/reconciliation.py`, `attack_graph/visualize.py`(+`AttackGraphService.export_html`).

---

## 2. Actual Runtime Pipeline

`redteam run engagement.yaml --config app.yaml` 기준의 실제 production call flow.
(괄호는 실제 호출되는 파일/함수.)

```text
CLI  redteam run
 └─ app/cli/__init__.py:main → _dispatch → commands/run.py:run → _run
    └─ _shared.load_app_config_for_run  → AppConfig
    └─ commands/run.py:build → app/bootstrap.py:build_application(config, mcp_stack)
        ├─ build_llm_client(config.llm)          → FakeLLMClient   (provider=fake)
        ├─ open_mcp_tool_gateway(mcp_servers=[])  → MCPToolGateway(ExampleMCPAdapter)
        ├─ DefaultControlPlane(                    control/service.py
        │     roe/scope/policy/rate_limit/budget,  control/{roe,scope,policy,rate_limit,budget}.py
        │     ApprovalService, AuditLog,           control/{approval,audit}.py
        │     RegistryRiskResolver(catalog))       control/risk.py
        ├─ EngagementScopedAttackGraphStore(       attack_graph/stores/{sqlite|memory}.py
        │     InMemory|SQLite)
        ├─ FilesystemEvidenceStore                 evidence/stores/filesystem.py
        ├─ default_registry()  (recon/web/mobile)  orchestration/agents/registry.py
        ├─ default_provider_registry()             orchestration/agents/providers.py
        ├─ default_result_mapper_registry()        orchestration/observations/*
        └─ RuntimeDeps                             orchestration/context.py
    └─ commands/run.py:execute
        └─ app/bootstrap.py:run_engagement(engagement, deps)
            ├─ seed_from_engagement(...)            orchestration/seed.py     (scope → graph frontier)
            ├─ bootstrap_mission(...)               orchestration/mission_bootstrap.py (Mission+Tasks)
            ├─ publish RUN_STARTED / MISSION_CREATED / TASK_CREATED
            └─ AgentLoopRunner(deps).run(...)       orchestration/runner.py
                └─ build_graph(deps)                orchestration/graph.py  (LangGraph StateGraph)
                   loop until terminal:
                   OBSERVE  nodes/observe.py  service.observe() → GraphObservation
                            │                 + OperatorIntentService.process_pending (drain PAUSE/STOP)
                            │                 + AgentRegistry.next_agent()
                   PLAN     nodes/plan.py     candidate_providers.propose_graph()
                            │                 CandidateCompiler(tool_catalog) + scoring
                            │                 build_planner("llm") → HybridPlanner (fake LLM tie-break)
                            │                 WorkScheduler.enqueue + WorkStore.acquire(lease)
                            │                 → ActionProposal
                   POLICY   nodes/policy.py   deps.control_plane.evaluate() → ALLOW/DENY/APPROVAL
                   EXECUTE  nodes/execute.py  deps.tool_gateway.invoke(invocation) → ToolResult
                   VERIFY   nodes/verify.py   evidence_store.append(); deps.verifier.verify()
                            │                 FindingPromoter.promote() → Finding
                   UPDATE   nodes/update.py   result_mappers.map_result() → GraphMutation
                            │                 service.observe_target / record_attempt / resolve_hypothesis
                   REPLAN   nodes/replan.py   evaluate_convergence(); task transition; next turn or END
            └─ settle_engagement                    app/bootstrap.py
                ├─ finalize_mission                  orchestration/replanning.py
                └─ _publish_terminal_artifacts
                    └─ ReportingAgent().project(...) orchestration/reporting.py → TerminalReportProjection
                    └─ save_terminal_report          app/report_store.py  (data/reports/*)
                    └─ publish REPORT_PROJECTED / RUN_FINISHED
 └─ (렌더링) commands/run.py:_render_event / renderer.py
```

병렬 경로(정의는 되어 있으나 기본 설정에서 미사용):

```text
PLAN --(dispatch_items>1)--> WORKER (nodes/worker.py) --> AGGREGATE (nodes/aggregate.py) --> REPLAN
        ^ router.py:route_after_plan → Send("worker", ...)
        기본 max_actions_per_turn=1 이므로 이 분기는 진입하지 않음
```

---

## 3. Pipeline Gaps

| 위치 | 기대 동작 | 실제 상태 | 영향 | 근거 |
|---|---|---|---|---|
| Capability (MCP) | 실제 스캐너 툴로 타깃 probe | shipped config 전부 `mcp_servers: []` → `ExampleMCPAdapter`의 `recon.ping` echo 툴만 실행 | 실제 취약점 탐지 불가; 그래프는 정적 `observed_attrs`만 채워짐 | `app.yaml:9`, `examples/app.yaml:56`, `capability/bootstrap.py:95-107`, `adapters/mcp/example.py:58-67` |
| LLM | 실제 모델 판단 | shipped config 전부 `provider: fake` → `FakeLLMClient` | Agent "판단"은 결정론적 playbook/scoring이 지배; LLM은 tie-break에서만 fake 응답 | `app.yaml:2`, `examples/app.yaml:41`, `app/bootstrap.py:110-114` |
| Result mapping | 실행 결과 → 그래프 관측 | 기본 툴 `recon.ping`에 등록된 mapper가 **없음** (매퍼는 `recon.http_probe/dns_resolve/...`, `web.*`에만 존재) | 기본 슬라이스에서 `map_result`가 "nothing new"로 degrade → 그래프 확장이 정적 attrs에 국한 | `observations/mapper.py:188-197`, `nodes/update.py:119-122` |
| 병렬 dispatch | 한 turn에 다중 액션 fan-out | `max_actions_per_turn=1` 기본값이 상한 → `dispatch_items≤1` → WORKER/AGGREGATE 미진입 | 병렬 실행/`merge_worker_outcomes` 코드가 런타임에서 도달 불가 | `settings.py:66`, `nodes/plan.py:751`, `router.py:route_after_plan`, `dispatch.py` |
| LLM 후보 생성 | 모델이 playbook 밖 후보 제안 | `llm_candidate_generation=False` 기본값 → 분기 미실행 | `LLMCandidateProvider.propose_graph`가 런타임 기본 경로에서 호출 안 됨 | `settings.py:93`, `nodes/plan.py:186,243` |
| Reconciliation host | 터미널/APPROVAL_PENDING run을 주기적으로 재방문 | `run_reconciliation_loop`를 호출하는 src 코드 없음 (CLI는 자체 `reconcile_once` 폴링만 사용) | 장기 미해결 approval을 자동 재개하는 상시 호스트 없음(단발 폴링만) | `orchestration/reconciliation.py:1-40`, src import 0건 |

---

## 4. Orphan Implementations

| Component | 구현 위치 | 연결 상태 | 분류 | 근거 |
|---|---|---|---|---|
| `run_reconciliation_loop` (reconciliation host) | `orchestration/reconciliation.py` | src에서 import 0건, 테스트만 참조 | ORPHAN / FUTURE | src grep 0건; 모듈 docstring "not a CLI entrypoint" `reconciliation.py:18-20`; `tests/unit/test_reconciliation_host.py` |
| Evidence linkage validator | `evidence/validation.py` | src에서 import 0건, `tests/unit/test_evidence_validation.py`만 참조 | TEST-ONLY / ORPHAN | src grep 0건 |
| Attack Graph 시각화 (`render_html`/`write_html`) | `attack_graph/visualize.py` | `AttackGraphService.export_html`이 감싸지만 그 메서드를 부르는 src 호출자 없음 | TEST-ONLY / ORPHAN | `service.py:736-750`; `export_html/export_snapshot` 호출자 = `tests/*`만 |
| `LLMCandidateProvider` | `orchestration/agents/llm_provider.py` | plan 노드가 import하나 `llm_candidate_generation=False` 게이트로 미실행 | FUTURE (설정 시 활성) | `nodes/plan.py:186,243`, `settings.py:93` |
| WORKER/AGGREGATE 노드 + `DispatchItem`/`WorkerOutcome`/`merge_worker_outcomes` | `nodes/worker.py`, `nodes/aggregate.py`, `orchestration/dispatch.py` | 그래프에 등록·엣지 연결되어 있으나 `max_actions_per_turn=1`로 진입 조건 미충족 | FUTURE (설정 시 활성) | `graph.py`(노드 등록), `router.py:route_after_plan`, `settings.py:66` |
| 실제 MCP 어댑터 4종 (`WebAPI/Recon/Mobile/Analysis MCPAdapter`) | `capability/adapters/mcp/*` | `_create_adapter`에서 생성 가능하나 `mcp_servers`가 비어 있어 shipped config에서 미생성 | OPTIONAL (env/config 필요) | `capability/bootstrap.py:113-127,205-236` |
| MCP 서버 구현체 (`servers/{web_http,recon,active,proxy,zap,nuclei,mobile,analysis}`) | `capability/servers/*` | 별도 프로세스(`python -m redteam.capability.servers ...`)로만 기동; 인프로세스 call path 아님, 설정된 server_id가 있을 때만 접속 | OPTIONAL (배포/설정 필요) | `servers/__main__.py`, `deploy/mcp/README.md:126,248` |

> 참고: `servers/{network,session_state,inspection}`는 위 서버 구현체들이 공유하는
> 서버측 헬퍼이므로 orphan이 아니다(서버 쪽에서 상호 import). `connectors/proxy_capture`는
> `servers/zap.py`에서만 쓰인다.

---

## 5. Dead / Legacy / Test-only Code

| 대상 | 유형 | 현재 사용처 | 삭제 후보 여부 | 근거 |
|---|---|---|---|---|
| `evidence/validation.py` | Test-only | `tests/unit/test_evidence_validation.py` | P1 — 연결 결정 필요 (Track B 잔재) | src import 0건 |
| `orchestration/reconciliation.py` | Future/미연결 host loop | `tests/unit/test_reconciliation_host.py` | P1 — CLI 통합 전까지 보류 (docstring이 "later integration"이라 명시) | src import 0건 |
| `attack_graph/visualize.py` + `service.export_html/export_snapshot` | 미연결 기능 | `tests/{unit/test_graph_visualize,integration/test_agent_loop}.py` | P1 — CLI로 노출하거나 보류 결정 | src 호출자 0건 |
| `build_runtime_deps` (Engagement-only 부트스트랩) | Legacy 병행 경로 | `examples/minimal_run.py`, 다수 테스트. CLI는 미사용 | P2 — 유지 (테스트/예제 진입점) | `app/bootstrap.py:117-198`; docstring "never the CLI" |
| shipped TODO 마커 (backend swap 안내 등) | Skeleton 주석 | 문서화된 확장 지점 | P2 — 유지 | `bootstrap.py:146`, `adapters/mcp/example.py:8`, `control/*.py` (총 ~9건, 대부분 안내성) |

> 문자열 스캔상 `NotImplementedError`는 `adapters/mcp/transport.py:155`의 Protocol 추상
> 메서드 1건으로, 실제 구현이 별도 존재하므로 dead code가 아니다.

---

## 6. Feature Necessity

기준: "정상적인 Agent Tool Calling Loop 완성"에 직접 필요한가.

| Feature | Runtime 사용 | 현재 필요성 | 분류 |
|---|:---:|---|---|
| CLI `run` / bootstrap / RuntimeDeps 조립 | O | 루프의 시작점 | CORE |
| Orchestrator 9-node 그래프 (observe→...→replan) 순차 경로 | O | 루프 본체 | CORE |
| Agent 후보 생성(결정론 playbook: recon/web) + HybridPlanner 선택 | O | Agent 판단 | CORE |
| Control Plane (roe/scope/policy/rate_limit/budget/risk/approval/audit) | O | ActionProposal 게이트 | CORE |
| Tool Gateway + MCP 어댑터 추상 | O | 툴 호출 경계 | CORE |
| `ExampleMCPAdapter` (echo 툴) | O | 슬라이스 구동용, 실탐지 불가 | CORE(임시) — 실어댑터로 교체 대상 |
| Evidence 저장 + Result Mapper → Graph 갱신 | O | 결과 소비 → 다음 판단 | CORE |
| Finding 승격 + ReportingAgent 최종 리포트 | O | 산출물 | CORE |
| 보조 CLI: `resume`, `approvals` | O(HITL 시) | approval lifecycle 재개 | CORE(HITL) |
| 보조 CLI: `init/validate/status/roe/tools/findings/report/evidence` | O(온디맨드) | 상태 조회/검증, 루프 밖 | OPTIONAL |
| 실제 MCP 어댑터(web/recon/mobile/analysis) + `servers/*` | X(기본) | 실제 스캔에 필수, config/배포 필요 | OPTIONAL→(실운영 시 CORE) |
| litellm 실 LLM 경로 | X(기본) | 실제 모델 판단에 필요 | OPTIONAL→(실운영 시 CORE) |
| 자연어 인테이크 (`intake/*`) + interactive 명령 | X(explicit run) | 셋업 UX 프런트엔드 | OPTIONAL |
| Operator Intent 채널 (`operator_intents/*`, observe drain) | O(플러밍)/X(피드) | interactive에서만 intent 투입; run은 no-op drain | OPTIONAL |
| LLM 후보 생성(`LLMCandidateProvider`) | X | 게이트 off | FUTURE |
| 병렬 fan-out(WORKER/AGGREGATE/DispatchItem) | X | `max_actions_per_turn=1`로 미진입 | FUTURE |
| Reconciliation host loop | X | 미연결 | FUTURE |
| Evidence linkage validator | X | 미연결 | DEAD/TEST-ONLY |
| Attack Graph HTML 시각화 | X | CLI 미노출 | DEAD/TEST-ONLY |

---

## 7. Test vs Runtime Gap

**"Unit PASS 이지만 실제 Runtime에서 도달 못 하는" 영역:**

- **병렬 dispatch (WORKER/AGGREGATE).** `tests/integration/test_agent_loop.py` 등에서
  fan-out을 커버하지만, 실제 실행은 `max_actions_per_turn=1` 기본값으로 항상 순차 경로만
  탄다. 테스트는 설정을 올려 검증하므로 "테스트 구성 ≠ production 구성"이다.
- **LLM 후보 생성.** `llm_candidate_generation=False`가 기본이라, 이 분기를 켠 테스트만
  `LLMCandidateProvider`를 검증한다. 기본 run에서는 도달하지 않는다.
- **실제 MCP 어댑터/서버.** `examples/finance_*_smoke.py`와 관련 테스트가 실어댑터/서버를
  검증하지만, shipped engagement/app config는 이를 활성화하지 않는다. E2E `redteam run`은
  `ExampleMCPAdapter`로만 통과한다 — 즉 실툴 경로는 CLI E2E에서 검증되지 않는다.
- **`recon.ping` mapper 부재.** 기본 슬라이스가 실행하는 유일한 툴에 result mapper가 없어,
  "결과 → 그래프 관측" 경로의 실질 매핑 로직은 기본 run에서 실행되지 않는다(정적 attrs만).
  매퍼 자체는 다른 tool_id로 단위 테스트된다.
- **완전 미도달 기능의 테스트.** `evidence/validation.py`, `reconciliation.py`,
  `attack_graph/visualize.py`는 단위 테스트가 PASS하지만 어떤 런타임 경로도 이들을 호출하지
  않는다.

**요약:** 테스트 스위트는 "구현된 능력"을 잘 검증하지만, CLI E2E가 실제로 밟는 경로는
가짜 LLM + echo 툴 + 순차 단일 액션이라, **실제 도구 호출·병렬·LLM 판단은 production
구성에서 검증되지 않고 있다.**

---

## 8. Cleanup Candidates

### P0 — 지금 제거 또는 연결 여부 결정 필요
기본 Agent Loop 이해를 흐리게 만드는 것.

- **없음(삭제 대상 기준).** 기본 루프 자체는 깨끗하게 연결되어 있다. 다만 **결정이 필요한 P0**는
  코드가 아니라 구성이다: shipped config가 `fake`/`ExampleMCPAdapter`로 고정되어 있어 "동작함"이
  실제 능력으로 오인될 수 있다. → 최소 하나의 실 MCP/LLM 구성을 shipped 예제로 승격하거나,
  README/CLI 경고를 유지하는 방향을 명시적으로 확정할 것.

### P1 — 기본 동작 완성 이후 검토
현재 필요 없는 부가기능·미연결 코드.

- `evidence/validation.py` (Track B 잔재, 미연결) — 연결하거나 제거.
- `orchestration/reconciliation.py` (`run_reconciliation_loop`) — CLI/배포 통합 여부 결정.
- `attack_graph/visualize.py` + `service.export_html/export_snapshot` — CLI 명령으로 노출할지 결정.
- LLM 후보 생성 / 병렬 fan-out — 기본 루프 안정화 후 활성화 검토 (지금은 게이트로 off라 무해).

### P2 — 유지 가능
Future 기능이나 현재 구조를 복잡하게 만들지 않음.

- `build_runtime_deps` (Engagement-only 부트스트랩): 예제/테스트 진입점으로 계속 필요.
- `capability/servers/*` 및 실 MCP 어댑터 4종: 실운영에 필요한 서버측/클라이언트측 구현.
  인프로세스 call path와 분리되어 있어 기본 루프를 복잡화하지 않음.
- `intake/*` + `operator_intents/*`: interactive UX. explicit `run`을 건드리지 않음.
- 문서화된 backend-swap TODO 주석들.

---

## 가장 중요한 질문에 대한 답

**Q1. "코드가 존재해서 구현된 것처럼 보이지만 실제 Agent 실행에는 아무 역할도 못 하는 것"은?**

1. **`evidence/validation.py`** — 런타임 어디서도 import되지 않음 (테스트 전용).
2. **`orchestration/reconciliation.py`의 `run_reconciliation_loop`** — src 호출자 0건, 스스로도
   "not a CLI entrypoint"라 명시.
3. **`attack_graph/visualize.py` + `AttackGraphService.export_html/export_snapshot`** — CLI 어디에도
   노출되지 않아 테스트만 호출.
4. **병렬 fan-out(WORKER/AGGREGATE/DispatchItem)** 과 **`LLMCandidateProvider`** — 그래프/노드에는
   존재하지만 기본 설정(`max_actions_per_turn=1`, `llm_candidate_generation=False`)에서 진입 불가.
5. (능력 착시) **실 MCP 어댑터/서버·litellm 경로** — 코드는 완비되어 있으나 shipped config가
   전부 `fake`/`[]`라, 실제로 도는 것은 echo 툴 + 가짜 LLM이다.

**Q2. "정상적인 Agent Tool Calling Loop 완성에 직접 필요하지 않은 구현"은?**

- 직접 불필요(현 시점): 병렬 fan-out, LLM 후보 생성, reconciliation host, evidence 링크 검증,
  그래프 HTML 시각화, 자연어 intake/interactive, operator-intent 투입 경로.
- 루프 완성에 정말 필요한 것은 이미 다 연결돼 있다. **남은 진짜 과제는 "새 코드"가 아니라
  `ExampleMCPAdapter`/`FakeLLMClient`를 실제 MCP 어댑터/LLM로 교체하고, 그 툴 tool_id에 맞는
  result mapper가 붙는지 확인하는 것**이다 — 즉 능력의 문제가 아니라 배선·구성의 문제다.

<!-- 감사자: Runtime Architecture Auditor / 코드 미수정 진단 전용 -->
