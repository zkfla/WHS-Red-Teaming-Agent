# 에이전트 도구 실행체계 개편 — TASK-002~008 Worker Packet 초안

Status: DRAFT — root가 ExecPlan(docs/exec-plans/active/agent-tool-execution-redesign.md)에 통합하기 위한 orchestrator 초안.
기준: dev-orchestration @ 0878650b. Recon: B(capability/MCP/skill)·D(test/dev env) 완료, A(native loop)·C(Control)는 429로 미완 — A/C 근거는 "미확인"으로 표시한다.
아키텍처 방향(root 확정, 기술 리뷰 반영): 셸 자체는 내장 provider로 ToolCatalog/ToolGateway/Control/Work/evidence 파이프라인에 넣되 실제 MCP 서버·transport는 쓰지 않는다. LocalToolRegistry 경로는 _bind_call/Control/WorkStore를 건너뛰므로(react.py) target-facing shell.run에는 쓰지 않는다. 순수 로컬 도우미(shell.status/list)만 LocalToolRegistry 허용. 기존 gateway/registry 어댑터 seam을 일반화해 재사용한다. 도구 등록부(MCP)는 유지하고, 셸 프로그램·스크립트는 등록 도구가 아니라 program card로 노출한다.

## 공통 규칙 (모든 packet 적용)

- AGENTS.md: 중앙 코드에 도구·에이전트·서버·모델 이름별 분기 추가 금지. 등록·조립은 조립 지점에서. 생산자만 바꾸지 말고 실행·재개·결과 처리·저장·보고 소비 경로를 확인. 모델 작성 스크립트/설치 프로그램을 자동으로 새 모델 함수·MCP 도구로 등록 금지. LLM은 경로·행동 제안, 코드는 범위·승인·유효성·증거 정합 계약 담당.
- PLANS.md §14 packet, §15 단계별 검증, §16 commit gate, §20 repair loop, §21 test integrity, §24 기존 변경 보호, §28 Completion Gate.
- 보호: 기존 사용자 변경(.gitignore 수정, norma.zip) 불가침. .env·secret 읽지 않음. dev-jm 병합 금지(참고만).
- 대상 연결은 egress 경계에서 사전 차단·계량. 사후 패킷 로그로 이미 전달된 요청을 차단했다고 주장하지 않음. 한 스크립트의 다중 요청을 1 action으로 계산하지 않음.
- 도구 실행 성공/관측/가설/단계 완료/목표 달성을 구분. 도구 성공만으로 확정 금지.
- 기술 리뷰 반영 항목(공통): mission binding(mission_id/run_id 공유 + 인스턴스별 instance_id·thread_id), lease owner=instance_id, warm container label, 네트워크 스파이크 계량.

## Verified anchors (packet 근거, orchestrator 직접 확인)

- orchestration/local_tools.py: RUNTIME_TOOL_KIND="runtime_tool", LocalToolDescriptor(tool_id/description/input_schema/contribution_id/version), LocalToolRegistry.register/get/descriptors/call, runtime_tool_contribution(...). 문서 주석: runtime-local tool은 실행 컨텍스트 내부에서 돌고, caller reasoning budget을 쓰며 target action budget은 쓰지 않고, 결과는 일반 model tool_calls 경로로 돌아와 center가 tool 이름으로 분기하지 않음. LocalToolResult.runtime_outcome은 canonical settlement handler 전용.
- app/runtime_components.py:262-282 validate_kinds에 "runtime_tool" 포함(신규 kind 불필요). :341-346 assembly가 contributions("runtime_tool")를 돌며 contribution.factory(context)->(descriptor,handler) 후 local_tools.register. 기존 declarer: native_local_tools.py, graph_recording.py, job_local_tools.py, authentication_local_tools.py.
- nodes/react.py:353 (native_action이 deps.local_tools.descriptors()를 native tools param에 추가), :339 ("local_tools" context key는 tools param과 중복이라 pop). engine.py:1031-1037 context "local_tools" 투영.
- core/ports/tool_catalog.py: ToolCatalog.get/all_descriptors/list_by_capability/list_tools_for_action. metadata 전용, 실행·정책 판단 없음.
- capability/adapters/mcp/base.py: MCPAdapter Protocol(server_id/discover_tools/invoke/health_check) — transport 비의존 메서드 계약. capability/gateway.py MCPToolGateway.__init__(registry, adapters, operation_bindings, ...) 독스트링 "the only class in the codebase allowed to call an MCPAdapter directly" — 승인된 ToolInvocation을 server_id의 어댑터로 라우팅. capability/bootstrap.py _build_gateway/_create_transport/transport.session()이 MCP 경로.
- orchestration/coordination.py:149 binding(run_id, mission_id, engagement_id) — binding 레코드가 run_id 키 → mission당 1 run 제약. runner.py:270 coordinator.bind(str(runtime.run_id), ...). graph_recording.py:400 _sources_same_run이 refs의 run_id 일치 요구. recovery.py reprocess_evidence(..., run_id) run_id 스코프.
- core/contracts/tool.py:30-33 provider metadata keys(target_binding/requires_target/command_fields/availability_impact), :56 argument_bindings, :60 target_binding, :64 requires_target, :69 command_fields, :75 availability_impact.
- capability/gateway.py:213 catalog.get, :231 supports, :265 refresh_health, :291 input_schema validate, :305 server semaphore, :306 before_start admission, :377 output_schema validate (TOCTOU는 gateway.invoke에서 닫힘, compiler 아님).
- capability/servers/execution.py: run_command @647, job_start @746, job_status @789, job_cancel @807 (별도 factory @1079-1136). command+job triad 이미 존재.
- orchestration/observations/mapper.py:180 "Exact tool_id -> mapper. No wildcard/prefix matching", :262 str(tool_id) 조회, no_mapper outcome. 임의 도구·셸 결과는 evidence-only.
- capability/methodology.py:1 "No MCP Tool or Skill auto-install", :4 pinned hash는 drift 검출만, :91 trust="untrusted_reference", :94 review_status="reviewed_reference_only". Skill은 실행 도구가 아니라 참고 번들.
- capability/bootstrap.py:43 build, :71 open, :141-142 session enter, :219 registry.replace_server_tools. app/bootstrap.py:605 빈 mcp_servers는 정직한 빈 카탈로그, :612 example sentinel, :621-629 engagement-scoped mcp_stack(단일 세션, reconnect/replay 없음). discovery는 build 시 1회 snapshot, Catalog는 재조회 안 함, refresh_health만 liveness 재조회.
- 테스트(D): tests/unit 193 .py, integration 67, contract 4, fakes 6, tests/conftest.py 없음. CI gate: pytest -q, ruff check, mypy, py3.10+py3.14(finance-lab job은 3.11), + mcp-container/finance-lab job. Windows는 platform-dependent 실패 문서화 → 최종 근거는 Linux. live harness는 data/ 하위 ad hoc/gitignored: data/react-loop-20261007/live_harness.py, data/react-bank-live-20261008/bank_live_harness.py.
- 미확인(A/C): native loop 상세(engine.py/runner.py/checkpointing.py), Control admission/approval/budget/evidence 추적. DefaultControlPlane.evaluate가 descriptor 위험도·대상/ROE/승인·action당 rate/budget을 소비한다는 서술은 계획 초안 근거이며 C worker 미검증.

---

## TASK-002 — 인스턴스 정체성·작업실·작업 ID·상태 원장 공통 계약

Task ID: TASK-002
Goal: 같은 역할의 여러 실행 인스턴스를 표현하는 공통 계약(인스턴스 정체성, 작업실 식별자, 장기 작업 ID, 상태 원장)을 기존 저장소·계약 위에 추가하고, 상위 조정과 인스턴스 상태를 분리한다. mission_id/run_id는 인스턴스 간 공유하고 인스턴스마다 고유 instance_id + LangGraph thread_id/runner를 둔다.
Relevant Requirements: FR-001, FR-002, FR-010, NFR-001.
Applicable AGENTS rules: 역할 정의와 실행 인스턴스 분리, 공통 실행 계약 재사용, 인스턴스별 판단·작업 상태 독립 소유, 상위 조정은 배정·의존성·공유 상태 정산 담당. 새 저장소·등록 체계 신설 전 기존 구성요소로 해결 가능한지 확인.
Dependencies: TASK-001A.
Ownership: root(SERIAL_ONLY) — 공유 계약이므로 worker 단독 금지.
Interface Contract:
- 기존 task_graph/work store·engine runtime 위에 인스턴스 식별자와 작업실/작업 ID 참조를 표현한다. 기존 상태 원본을 복제하지 말고 조회·파생 정보로 분리. runtime_outcome 어휘 재사용. (구체 API는 root 확정.)
- 후보 설계(mission binding): mission_id/run_id 공유 + 인스턴스별 instance_id·고유 thread_id/runner. 근거: coordination.py:149 binding이 run_id 키로 mission당 1 run 제약을 가지므로 run_id 공유 시 runner.py:270 coordinator.bind와 이 제약을 유지한다. graph_recording.py:400 _sources_same_run이 refs의 run_id 일치를 요구하므로 run_id 공유 시 인스턴스 간 evidence가 same-run 계약을 만족한다.
- lease owner를 instance_id로 바꾼다(현재 tools_node가 owner=str(runtime.run_id) 사용). work_store acquire/complete·crash reconcile 판정에 영향.
- worker runner의 binding close/goal.settle이 전역 mission을 닫지 않도록 상위 조정 책임을 분리한다(runner.py:188-367 경로).
- 기존 run_id 스코프 복구 함수 영향: recovery.py reprocess_evidence(..., run_id)·_sources_same_run은 run_id 기준 → run_id 공유 시 호환. 인스턴스별 run_id 대안은 same-run evidence·복구 스코프를 깨므로 기각 후보.
Acceptance Criteria:
- 동일 역할 2개 인스턴스가 독립 판단 문맥·작업·작업실을 가지며 서로의 상태를 덮지 않는다.
- mission_id/run_id 공유 하에서 Coordinator.bind의 mission당 run 제약과 graph.record same-run evidence 계약이 유지된다.
- lease owner가 instance_id로 정합하고, worker runner가 전역 mission을 닫지 않는다.
- 장기 작업 ID가 run 재개 후에도 조회 가능하다.
- 한 인스턴스의 작업이 무관한 인스턴스 다음 판단을 막지 않는다.
Required Validation: 두 인스턴스 분리, lease owner instance_id 정합, binding/same-run evidence 유지, 재개 후 조회, 기존 단일 인스턴스 경로 회귀 테스트. 관련 기존 테스트(integration/test_agent_loop.py fan-out/lease/checkpoint) 우선.
Known Risks: 기존 runtime이 실행별 중앙 단일이라고 가정된 부분(A/C 미확인)과 충돌 가능. 저장소 이중화 위험. lease owner 변경과 crash reconcile 상호작용. mission binding 공유 시 상위/개별 책임 경계 모호화.
Forbidden Scope: 새 백엔드/큐 신설, dev-jm 코드 이식, 도구 이름별 분기.

## TASK-003 — 상태형 컨테이너 작업실 및 셸 스크립트 sync/job 실행 제공자

Task ID: TASK-003
Goal: 인스턴스별 상태형 작업실에서 Bash/Python 스크립트·파이프라인을 실행하는 built-in 실행 provider를 ToolCatalog/ToolGateway/Control/Work/evidence 파이프라인에 넣는다(실제 MCP 서버·transport 미사용). 짧은 실행은 결과 반환, 장기 실행은 job ID로 start/status/cancel. 재개 시 기존 작업을 먼저 조회한다.
Relevant Requirements: FR-003, FR-004, NFR-003.
Applicable AGENTS rules: 셸·스크립트는 실행 입력·산출물. 오래 걸리는 작업은 식별자로 추적하고 상태 확인 전 재실행 금지. 실행 환경 프로그램 소유권·수명·실행 경로 구분. 기본/인스턴스 설치/공통 배포/외부 연결형/참고 스킬 구분.
Dependencies: TASK-002.
Ownership: worker, PARALLEL_AFTER: TASK-002.
Interface Contract:
- 셸 자체는 LocalToolRegistry 경로를 쓰지 않는다. 근거: native_action은 local tool이면 _bind_call을 건너뛰고(react.py: deps.local_tools.get() != None), tools_node의 _local_call은 Control/WorkStore를 거치지 않고 deps.local_tools.call을 직접 실행한다 → target-facing shell.run을 local tool로 두면 승인·예산·lease·증거를 우회한다.
- 대신 기존 gateway/registry 어댑터 seam을 일반화해 재사용한다. MCPAdapter Protocol(adapters/mcp/base.py: server_id/discover_tools/invoke/health_check)을 만족하는 in-process provider를 구현하고 MCPToolRegistry + MCPToolGateway(registry, adapters, ...) 구성에 등록해 Control admission(descriptor 위험도·대상/ROE/승인)·WorkStore lease·증거 파이프라인을 통과시킨다. gateway 독스트링대로 어댑터를 직접 호출하는 유일 지점은 gateway다.
- transport.session()/_create_transport 경로는 쓰지 않는다(MCP 서버·transport 미사용). 필요 시 Protocol/타입 이름만 transport-agnostic으로 최소 일반화한다.
- execution MCP job triad·검증 코드(execution.py:647/746/789/807)는 재사용 대상.
- 순수 로컬 도우미(shell.status/shell.list 등 workshop 상태 조회)만 LocalToolRegistry(runtime_tool) 허용. runtime_outcome 미설정.
- program card·스크립트는 등록 도구가 아니다(TASK-004).
- shell.run(및 job_start/status/cancel)은 built-in ToolDescriptor로 등록되어 Control 경로를 통과한다. 금지 대상은 모델 작성 스크립트·설치 프로그램의 자동 도구 등록이다.
- 작업실은 인스턴스별 상태(파일·설치 프로그램)를 보존하고 warm label로 인스턴스 신원을 표시, 다른 인스턴스와 격리한다. warm label은 execution.py의 _MANAGED_LABEL(io.whs-redteam.execution.managed, :41)·context label(io.whs-redteam.execution.context, :42)과 별도 namespace여야 한다.
Acceptance Criteria:
- 같은 인스턴스의 짧은 실행 결과 반환, 장기 작업 start/status/cancel, run 재개 후 상태 조회가 동작한다.
- shell.run이 Control admission·WorkStore lease·증거 파이프라인을 통과한다(local tool 경로 우회 아님).
- MCP 서버·transport 없이 동작한다.
- 인스턴스 A의 장기 작업이 인스턴스 B의 다음 판단을 막지 않는다.
- 인스턴스 설치 프로그램이 다른 인스턴스·공통 도구로 자동 승격되지 않는다.
- 서버 재기동 시 cleanup_orphaned_containers(execution.py:1028, _MANAGED_LABEL/context label 필터)가 warm container를 삭제하지 않는다(별도 namespace 라벨).
Required Validation: 수명·격리·출력 상한·취소·재개 테스트. shell.run이 local tool 경로를 우회하지 않음(Control/Work/evidence 통과)과 warm label·lease owner instance_id 정합 회귀. 서버 재기동 후 warm container 비삭제 확인. Windows 로컬 실행 확인, 최종 근거 Linux.
Known Risks: Docker 작업실 상태 영속·격리 미검증(U-002). 재개 시 실행 중 셸 프로세스 존재를 자동 복원으로 오해 금지(ER-001/ER-003). 어댑터 Protocol 일반화가 기존 MCP 경로를 회귀시킬 수 있음.
Forbidden Scope: 모델 작성 스크립트·설치된 프로그램을 새 모델 함수/MCP 도구로 자동 등록, 셸 제공자를 MCP 서버/transport로 구현, 신규 큐·원장 백엔드, dev-jm 이식.

## TASK-004 — 도구·MCP·셸 프로그램·스킬 카드/상세 조회·소유 범위 연결

Task ID: TASK-004
Goal: 내부 도구·외부 MCP 도구·셸 program card·스킬 참고 번들을 모델이 찾고 필요 시 상세를 조회하게 하되, 등록 원본은 단일로 유지하고 검색이 숨은 허용 목록이 되지 않게 한다.
Relevant Requirements: FR-004, FR-005, FR-006, NFR-003.
Applicable AGENTS rules: 모델이 사용 가능한 도구·스킬을 찾게 하되 상세 정의를 매번 문맥에 넣지 않고 검색 결과를 숨은 허용 목록으로 삼지 않음. 선택 정보와 상세를 목적으로 맞게 제공. 실행 권한·입력 유효성은 실제 실행 경계에서 판단. 기존 등록 정보·SDK 조회로 얻을 정보를 별도 목록으로 복제 금지.
Dependencies: TASK-002.
Ownership: worker, PARALLEL_AFTER: TASK-002.
Interface Contract:
- MCP 도구 등록부는 기존 경로 유지(MCPAdapter + manifest + profile + REDTEAM_MCP_SERVERS). 새 registry/catalog 신설 금지.
- 상세 조회는 기존 Core ToolCatalog port(core/ports/tool_catalog.py) 확장 또는 기존 all_descriptors/list_by_capability/list_tools_for_action 재사용으로 해결. 두 번째 catalog 금지.
- program card는 ToolDescriptor가 아니다. local tool은 tools param으로 호출 가능하게 노출되므로(react.py:353) program card는 호출 불가 정보이므로 별도 context 전용 surface로 둔다(또는 program.describe local tool). 검색 결과는 권한 필터가 아니다.
- 스킬은 capability/methodology.py 참조 번들 패턴 재사용(pinned source/version/hash, trust=untrusted_reference, review_status=reviewed_reference_only, read-only, 권한 변경 불가). Skill 실행 도구 타입 신설 금지.
- 소유 범위 구분: 기본 제공 프로그램 / 인스턴스 설치 프로그램 / 공통 배포 프로그램 / 외부 연결형 MCP 도구 / 참고 스킬.
Acceptance Criteria:
- 모델이 내부 도구·MCP 도구·program card·스킬을 카드로 발견하고 상세를 조회한다.
- 카드 목록이 권한 판정을 대체하지 않는다(범위 밖 실행은 실제 경계에서 거절).
- 인스턴스 설치 프로그램이 카드로 노출되어도 공통 도구로 자동 등록되지 않는다.
Required Validation: discoverability, 상세 조회, 비자동등록, 범위 밖 거절 테스트. 기존 tool_selection/registration 테스트 회귀.
Known Risks: context 비대화(상세를 매 턴 넣지 않기), program card를 ToolDescriptor로 오용.
Forbidden Scope: 새 도구 등록 체계, Skill tool 타입, dev-jm 이식.

## TASK-004A — 셸/임의 도구 결과의 공통 관측 경로

Task ID: TASK-004A
Goal: 셸 실행·미등록 도구 결과가 그래프 지식으로 반영되는 경로를 정의한다(현재는 exact tool_id mapper가 없어 evidence-only).
Relevant Requirements: FR-009.
Applicable AGENTS rules: 관측·가설·확정 구분. 도구별 이름 하드코딩 금지. 결과를 빈 값·성공으로 숨기지 않음.
Dependencies: TASK-003, TASK-004.
Ownership: worker, PARALLEL_AFTER: TASK-003, TASK-004 (두 선행 완료 후).
Interface Contract: observations/mapper.py는 exact tool_id 키, wildcard 없음(:180). 셸 결과는 (a) evidence-only 계약으로 명시하거나 (b) 이름 비의존 generic mapper 기여로 처리한다. 도구 이름별 중앙 분기 금지.
Acceptance Criteria: 셸/임의 도구 결과가 evidence로 보존되고, 그래프 반영 여부가 계약대로 결정되며 no_mapper가 조용히 성공으로 처리되지 않는다.
Required Validation: mapped/empty/no_mapper 경로 테스트, 부분 성공(ok=false) 구분.
Known Risks: generic mapper가 의미 없는 노드를 양산할 위험 → 근거 있는 최소 반영.
Forbidden Scope: 도구별 전용 mapper 남발, 검증 요건 완화.

## TASK-005 — 실행 전 좁은 판정 및 대상 출구 목적지·사용량 강제

Task ID: TASK-005
Goal: 실행 전에는 명확히 판정 가능한 범위·주체·제한·승인 조건만 적용하고, 대상 출구 경계에서 허용 범위 밖 연결을 사전 차단·계량한다. 패키지 설치/검색 출구와 대상 연결 출구를 구분한다.
Relevant Requirements: FR-007, FR-008, NFR-003, C-002.
Applicable AGENTS rules: 실행 전 판정은 좁고 명확하게, 분류 불확실성만으로 정상 작업을 일률 승인 대기로 보내지 않음. 연결 경계가 관측·차단 가능한 대상과 사용량만 보장, 사후 로그로 요청 의미 판정 주장 금지. 한 호출에 여러 요청 가능성을 예산·기록에 반영. 승인 대상·근거는 실행 내용·조건에 연결.
Dependencies: TASK-002, TASK-003 (네트워크 경계).
Ownership: worker, PARALLEL_AFTER: TASK-002, TASK-003 (TASK-003 네트워크 경계 확정 후).
Interface Contract:
- 기존 Control evaluate/현재 허용 검사(control/service.py) 재사용. 도구 이름별 allowlist 추가 금지.
- 셸은 built-in provider로 gateway 경로를 통과하므로 Control이 descriptor 위험도·대상/ROE/승인을 그대로 평가한다. 대상 연결은 TASK-003 네트워크 경계(egress)에서 계량·차단한다.
- 순수 로컬 도우미(shell.status/list)는 target action budget을 쓰지 않으므로 대상 연결 계량 대상이 아니다.
- 네트워크 스파이크(다수 동시 연결·버스트)가 per-action 계량을 우회하지 못하도록 사용량 의미를 정의한다.
- 한 스크립트의 다중 요청을 1 action으로 계산하지 않도록 사용량 의미를 정의한다.
- 승인 필요 내용은 실제 스크립트/행동과 연결되고 fingerprint는 재개 내용을 바꾸지 않는다.
Acceptance Criteria: 허용/범위 밖/승인/다중요청/실패/설치 출구 분리 케이스가 동작한다. 직접 연결 우회(egress 경계를 거치지 않는 대상 연결)가 방지된다. 네트워크 스파이크가 per-action 계량을 우회하지 못한다. Control 원장(사용량·차단 기록) 정합성 테스트를 포함한다. 모호한 위험 추정만으로 모든 셸 호출이 승인 대기가 되지 않는다.
Required Validation: 정책 단위 테스트 + 로컬 격리 대상 egress 허용/거부·예산 소비 측정 + network spike 케이스. (C worker 미확인 → 실제 코드로 재확인 필요.)
Known Risks: egress 강제가 임의 TCP/UDP 도구를 보장하지 못함(U-002). Control 계약 회귀.
Forbidden Scope: 도구 이름별 예외, 모든 셸 승인 대기, 사후 로그 기반 차단 주장.

## TASK-006 — 공통 인스턴스 ReAct와 상위 병렬 배정·정산 연결

Task ID: TASK-006
Goal: 각 인스턴스가 공통 ReAct 구현을 자기 thread/문맥으로 실행하고, 상위 조정이 task graph 의존성·lease·정산으로 목적을 배정한다. 상위 N-runner 드라이버가 N개 인스턴스를 구동하고, 같은 task를 중복 점유하지 않도록 보장한다. 부분 결과·실패·중복 결과를 처리한다.
Relevant Requirements: FR-001, FR-002, FR-009, NFR-002.
Applicable AGENTS rules: 같은 역할 인스턴스가 공통 실행 계약 재사용, 판단·작업 상태 독립. 상위 조정은 무관 인스턴스 진행을 한 판단 턴에 종속시키지 않음. 중복·상충 결과가 조용히 덮이지 않게. 실행 성공/관측/완료 구분.
Dependencies: TASK-002, TASK-003, TASK-004A, TASK-005 (TASK-004는 004A가 이미 전제하므로 중복 표기하지 않음).
Ownership: root + worker, SERIAL_ONLY.
Interface Contract: 기존 native loop(nodes/react.py, runner.py, graph.py model/tools/settle) 재사용. 새 전략 루프 신설 금지. 공유 정산 원본은 attack graph·evidence store. 인스턴스별 thread/checkpoint 분리. 셸/임의 도구 결과는 TASK-004A 관측 경로로 원본 증거·graph.record에 연결한다.
Acceptance Criteria: 상위 N-runner 드라이버가 다중 인스턴스를 구동한다. 동일 역할 2개 병렬 실행, 한 작업 대기가 무관 작업을 막지 않음, 동일 task 중복 점유가 발생하지 않음, 중복·상충 결과가 명시적으로 정산됨, 부분 실패가 다른 인스턴스 결과를 덮지 않음.
Required Validation: integration/test_agent_loop.py 확장 — fan-out, 느린/빠른 결과, 승인 대기, 일부 실패. 셸 결과를 받은 인스턴스가 원본 증거 보존과 graph.record/settlement 경로까지 연결되는지 통합 검증(이 경로가 빠지면 정산 통합 테스트는 무의미).
Known Risks: A worker 미확인 → engine/runner 실제 구조로 재확인. runtime 단일 가정과 충돌.
Forbidden Scope: 새 전략 루프, 도구 이름별 분기, dev-jm 이식.

## TASK-007 — 논리적 기준점 및 전체/부분 재개, 진행 중 작업 조회·정산

Task ID: TASK-007
Goal: 상위 루프·인스턴스 루프·작업실/실행 중 작업·그래프 기록의 논리적 기준점을 저장하고, 전체/개별 복구 시 실행 여부 불명확한 외부 효과를 재실행하지 않는다. 실행 중 컨테이너가 사멸한 경우 사멸 사실을 조회하고 기존 작업 상태로 정산한 뒤 재개한다.
Relevant Requirements: FR-010, NFR-001.
Applicable AGENTS rules: 중단·재개 시 상위 상태·인스턴스 상태·실행 환경·진행 중 작업을 일관 기준점으로 연결. 이미 발생한 외부 효과 자동 반복 금지, 실행 여부 불명확하면 기존 작업 기록으로 조회·정산. 상태 중복 저장 금지.
Dependencies: TASK-003, TASK-006.
Ownership: root + worker, SERIAL_ONLY.
Interface Contract: 기존 checkpointing/work store lease·complete·TERMINAL_STATUSES reconcile(nodes/react.py tools_node) 재사용. 외부 셸 작업은 job ID로 조회·정산. MCP는 engagement-scoped 단일 세션·no-replay(app/bootstrap.py:621-629) 제약을 존중.
Acceptance Criteria: 전체/개별 복구, 외부 효과 무중복, 그래프·증거·작업 연계 복구가 동작한다. 컨테이너 사멸 조회·정산(사멸 컨테이너의 진행 중 작업을 재실행하지 않고 상태로 정산)이 동작한다. 승인 재개 일회 claim 유지.
Required Validation: crash-after-commit 재조정, 승인 재개, 개별 인스턴스 복구 테스트.
Known Risks: 셸 프로세스 존재 복원 오해. Windows/Linux 차이.
Forbidden Scope: 신규 원장 백엔드, 외부 효과 재생, 검증 완화.

## TASK-008 — 앱 조립·CLI/보고·기존 MCP 이관/호환·통합 테스트

Task ID: TASK-008
Goal: 새 실행 체계를 앱 조립·CLI·보고에 연결하고, 기존 MCP 경로를 이관 검증 전까지 유지하며 정상·거절·실패·재개·보고 회귀를 통합 검증한다.
Relevant Requirements: FR-005, FR-009, NFR-001, C-001.
Applicable AGENTS rules: 조립 지점에서 등록·설정·주입. 중앙 실행 흐름에 도구별 분기 반복 추가 금지. 생산자·소비 경로 계약 유지. 모듈 경계 준수.
Dependencies: TASK-004, TASK-005, TASK-006, TASK-007.
Ownership: root + worker, SERIAL_ONLY.
Interface Contract: app/bootstrap.py·runtime_components.py 조립 지점에서 연결. 보고는 reporting.py 기존 실행/관측/확정 의미 유지. legacy MCP 호출 경로 회귀 금지.
Acceptance Criteria: CLI smoke + 정상/거절/실패/재개/보고 회귀 통과. 기존 MCP 호출·승인 복구·task/goal settlement·증거 검증 계약 유지.
Required Validation: 저장소 필수 pytest, ruff, mypy(py3.10+3.14), CLI smoke. 최종 근거 Linux.
Known Risks: 조립 지점 분산으로 인한 누락, 보고 의미 변경.
Forbidden Scope: dev-jm 병합, main/dev 직접 위험 push, 검증 약화.

---

## Packet 간 의존/DAG 요약

- TASK-001A → TASK-002 → (TASK-003, TASK-004 병렬; TASK-005는 TASK-003 네트워크 경계 후) → TASK-004A(TASK-003, TASK-004 후) → TASK-006(TASK-004A 포함, N-runner 드라이버·중복 점유 방지) → TASK-007(컨테이너 사멸 조회·정산) → TASK-008 → TASK-009(리뷰/최종).
- 공유 계약(TASK-002)은 root 소유. 셸 provider(TASK-003)는 built-in adapter/gateway 경로(transport 미사용), egress(TASK-005)는 네트워크 경계 — 서로 다른 파일.
- program card(TASK-004)와 generic observation(TASK-004A)는 관측/context 경계로 분리.
- 모든 packet: 도구 이름별 중앙 분기·자동 등록·검증 완화 금지.

## 미확인 / 후속 확인 필요

- A(native loop)·C(Control) worker 미완: engine.py/runner.py/checkpointing.py, control/service.py admission·approval·budget·evidence 실제 경로를 구현 착수 전 root 또는 재파견 worker로 확인.
- Docker daemon·lab 컨테이너 가용성(D 미확인) → TASK-003/005 런타임 검증 전제.
- dev-vs-dev-orchestration 차이(571cef0c) 미확인 → TASK-001A.
- U-002 egress 최소 네트워크 배치.
