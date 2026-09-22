# ORCH-HARDENING-20260922 — 실행 protocol

> 작업 ID: `ORCH-HARDENING-20260922`
> 실행 worktree: `C:\\Users\\qkrgu\\.codex\\worktrees\\4261\\WHS-Red-Teaming-Agent`
> 기준 commit: `6a69445` (dev-orchestration)
> authoritative plan: `docs/agent/orchestration-hardening-plan-20260922.md`
> 결과 문서: `docs/agent/orchestration-hardening-result-20260922.md`
> 관리 스레드: `01a0c26d-09c5-7341-93d3-d8a90d8ea3c8`

이 문서는 코드를 건드리기 전에 작성·커밋한다. plan의 H1~H4를 어떤 순서로,
어떤 불변조건 아래, 어떤 게이트로 검증하는지를 고정한다.

## 1. H0 — baseline 정렬과 고정

### 1.1 worktree 정렬 (비파괴)

실행 worktree는 이전 task의 detached `84247a8`에 있었고, 그 HEAD는
`6a69445`의 조상이 아니다(공통 조상은 `0f71f81`). 정렬 방법:

1. `git tag -f orch-feedback-backup-84247a8 84247a8` — 이전 detached 작업을
   ref로 고정해 어떤 경우에도 유실되지 않게 한다.
2. `git switch --detach 6a69445` — worktree가 clean이므로 이동만 하고
   버려지는 작업이 없다.

사용하지 않은 것: `reset --hard`, `checkout --`, `clean`,
강제 branch 이동. 관리 체크아웃은 계속 읽기 전용이다.

이전 두 commit(`e7e47f5`, `84247a8`)은 직전 task 결과 문서의 자구 수정이며
baseline의 `docs/agent/orchestration-implementation-result.md`가 이미 그 내용을
포함한다. 즉 이관할 잔여 변경은 없다(백업 tag로도 보존).

### 1.2 베이스라인 측정

깨끗한 venv(`mcp 2.2.0`)에서 `python -m pytest tests -q`:
**1010 passed, 15 failed, 3 skipped**.

| 실패 | 건수 | 성격 |
|---|---|---|
| `test_finance_extensions.py` + `test_proxy_capture_connector.py` | 12 | `OSError [WinError 1314]` — Windows 심볼릭 링크 권한 없음(환경) |
| `test_cli_smoke.py::test_full_lifecycle_...` | 1 | `validate.py`가 체크 문자를 cp949 콘솔에 출력 → `UnicodeEncodeError`(환경) |
| `test_agent_loop.py::test_unexpected_run_failure_still_publishes_a_partial_report` | 1 | baseline 결함 — §1.3 |
| `test_result_mapper.py::test_web_mapper_preserves_only_normalized_response_metadata` | 1 | baseline 결함 — §1.3 |

### 1.3 baseline 결함 2건 수리 (H0, 테스트 한정)

두 건 모두 baseline commit에 이미 존재하던 실패이며, 원인이 명확해 수리한다.
수리 내용은 "테스트를 약화"가 아니라 "테스트를 사실에 맞춤"이다.

1. `test_agent_loop.py` — production
   `AgentLoopRunner.run(runtime, *, initial_event_id=None)`는 커졌는데
   monkeypatch fake `fail_run(self, runtime)`가 따라오지 않아
   `TypeError: unexpected keyword argument`가 난다. fake 시그니처를 실제
   계약에 맞춘다. production 코드는 건드리지 않는다.
2. `test_result_mapper.py` — 단언이
   `value not in str(cookie_metadata)`인데, mapper가 스스로 넣는
   `values_omitted: True` 플래그 이름에 걸린다. mapper가 쿠키 값을 복사하지
   않는다는 사실은 확인했다(`_safe_response_metadata`도
   `map_web_http_request`도 명시 allowlist만 복사한다). 단언을 의도에 맞게
   정확히 바꾼다: 원문 값 문자열이 없고, cookie dict에 `value` key가 없다.
   기존 substring 검사보다 강한 단언이다.

두 수리는 별도 commit으로 남기고 결과 문서에 명시한다. production 동작을 바꾸지
않으므로 H1~H4와 독립적으로 revert 가능하다.

## 2. H1 — Graph-aware ReconCandidateProvider

대상: `orchestration/agents/recon.py`(신규), `agents/providers.py`,
`agents/definition.py`, `agents/registry.py`, 필요 시 `nodes/plan.py`.

### 2.1 SERVICE-only bootstrap

현재 `WEB_API_AGENT.inspect-http-service`는 `ServiceTarget`에
`path`가 없어 항상 `graph_prerequisite_missing(path)`로 drop된다.
그래서 HOST + HTTP SERVICE만 있고 ENDPOINT가 없는 그래프에서 진행이 멈춘다.

- 소유자는 **Recon**이다(plan §3.2, §5 H1). Web이 recon 도구를 직접 호출하지 않는다.
- 후보: `capability=recon` + `operation=service_discovery`,
  `tool_id=recon.http_probe`, `target=HostTarget(host)`,
  arguments는 `target_ref`와 `path="/"`.
  이 `path="/"`는 새 추측이 아니라 **이미 존재하는 도구 기본값이자 기존 recon
  playbook 계약**이다. 다른 path는 절대 만들지 않는다.
- 조건: SERVICE fact에 `scheme`가 HTTP(S)임이 **관측**되어 있고,
  `host`와 `target_ref`가 SERVICE 또는 대응 HOST fact에서 해석되며,
  대응 HOST node가 Graph에 실제로 존재하고, 같은 HOST에 ENDPOINT가 아직 없고,
  그 HOST에 `recon.service_discovery`가 시도된 적이 없어야 한다.
- Catalog가 `("recon", "service_discovery")`를 실행 불가로 증명하면 후보를 내지
  않는다(`can_execute()`).

### 2.2 evidence-first inventory / TLS follow-up

- `recon.html_inventory`: **실제 응답이 관측된** ENDPOINT에만. path는 그
  ENDPOINT의 실제 path.
- `recon.openapi_inventory`: Graph에 OpenAPI/Swagger 문서로 볼 근거가 있는
  path가 실제로 존재할 때만(예: 경로명이 관측된 경우). 표준 경로를 만들지 않는다.
- `recon.oidc_inventory`: 같은 원칙 — discovery 문서가 실제로 관측된 path만.
- `recon.tls_inspect`: SERVICE fact가 HTTPS/TLS를 실제로 나타낼 때만.
- 조건이 아니면 후보를 만들지 않고, 이유가 있으면 기존 diagnostic 어휘
  (`CandidateDiagnostic`)로 남긴다.

### 2.3 확장 방식

provider 등록(`default_provider_registry`)만으로 동작하고, central router에
role별 `if`를 추가하지 않는다. 기존 `PlaybookCandidateProvider` 결과는
보존하고 graph follow-up을 **추가**한다.

## 3. H2 — Relation-aware observation / role reactivation

대상: `attack_graph/observation.py`, `attack_graph/view.py`,
`orchestration/progress.py`, `agents/definition.py`,
`agents/providers.py`.

현재 fingerprint는 node identity/content만 담고 edge는 담지 않는다. 그래서 이미
있던 Principal과 ENDPOINT 사이에 `CAN_ACCESS` 하나만 새로 생기면 Web이
그 delta를 보지 못한다.

- `GraphObservation`에 edge type별 **stable relation digest/count**를 추가한다.
- `AttackGraphView.observe()`가 node와 edge를 함께 observation으로 만든다.
- `ContextProfile.include_edge_types`를 추가하고, role fingerprint는 그 role이
  구독하는 edge type만 포함한다.
- Web/API profile에 `RESOURCE` node type과 최소
  `CAN_ACCESS`, `OWNS`, `AUTHENTICATES_TO`, `OBSERVED`, `EXPOSES`
  relation을 반영한다.
- edge digest에서 volatile provenance(`step`, timestamp, run id, 누적
  evidence id)를 제외한다. 포함하면 매 turn 모든 role이 재활성화된다.

완료 기준: relevant edge-only mutation은 Web fingerprint를 바꾸고, unrelated/
volatile edge 변화는 바꾸지 않는다(양·음 테스트 둘 다).

## 4. H3 — Web evidence-first follow-up

대상: `orchestration/agents/web.py`, `agents/providers.py`.

- `web.http_compare_sessions`: **같은 ENDPOINT에 대해 서로 다른
  operator-managed session의 실제 observation이 둘 이상** 있을 때만. 어떤 두
  session인지가 관측에서 나오지 않으면 후보를 만들지 않는다.
- `web.http_security_posture`: 실제 응답이 관측된 ENDPOINT이고 아직 posture
  observation이 없을 때만.
- 두 후보 모두 `assessment_kind="observation"`이며 Finding을 직접 만들지
  않는다. status/response 하나만으로 Finding을 만들지 않는다는 plan §7-6을 지킨다.
- 기존 authentication/authorization/redirect 규칙과 중복되지 않도록 request/context
  key로 구분한다. Catalog-bound operation이 없으면 조용히 후보를 만들지 않는다.

## 5. H4 — Priority signal producer + Hybrid planner

대상: `agents/planners.py`, `nodes/plan.py`, `settings.py`,
provider modules.

- provider가 **관측 가능한 사실만으로** `PrioritySignals`를 채운다:
  `expected_information_gain`(아직 inventory/비교되지 않은 surface),
  `evidence_gap_reduction`(open hypothesis가 요구하는 비교를 직접 채우는 경우),
  `downstream_unblock_count`(후속 ENDPOINT/identity/resource를 여는 것이
  계약상 명확한 inventory), `expected_cost`(요청 수가 도구 계약에 고정된 경우).
  `operational_risk`는 계속 ToolDescriptor/compiler가 authority다.
  LLM이 score를 쓰지 않는다.
- Hybrid 선택: 후보 1개 → deterministic. top1−top2 margin ≥ 설정값 → deterministic.
  그 외에만 bounded top-K rerank를 LLM에 요청. LLM 실패/invalid id → deterministic top-1.
- LLM 반환은 **제공한 `step_id` 집합 안에서만** 유효하다(기존 membership 검사 유지).
  Tool/target/candidate 발명은 불가.

## 6. 이번 사이클 불변조건 (위반 시 중단·보고)

1. 자유-form `ToolId` 생성 금지 — 모든 후보는 Catalog-bound binding을 가진다.
2. 표준 경로 추측 금지(`/openapi.json`, `/.well-known/openid-configuration`,
   `/login` 등을 "보통 있으니까" 만들지 않는다).
3. status code 하나로 Finding 생성 금지.
4. side-effect node에 blanket `RetryPolicy` 금지(중복 event/ledger 위험).
5. 중앙 tool-name `if` 체인 금지(registry dispatch 유지).
6. Core 계약에 금융 도메인 field 추가 금지.
7. Analysis/Reporting을 schedulable Agent로 변경 금지.
8. Capability 경계 보존: `MCPAdapter`/`Gateway`/`ToolCatalog` 그대로,
   `langchain-mcp-adapters`로 우회 금지.
9. WorkLedger lease/heartbeat/conflict/idempotency 의미 보존.
10. `LLMClient` + LiteLLM 추상화 보존.
11. LangGraph `StateGraph`/`Send`/checkpointer 보존(자체 workflow 엔진 금지).
12. `PLANNING_CONTENT_KEYS` 전면 재설계 금지(plan §6) — 필요한 RESOURCE/
    observation key만 국소 보강한다.
13. `update.py`에 tool별 분기 추가 금지(ResultMapperRegistry 유지).

## 7. 검증 게이트

| 게이트 | 내용 |
|---|---|
| G1 | H1 targeted unit + Recon → Endpoint → Web 통합 시나리오 |
| G2 | H2 edge-only relevant(양) / unrelated·volatile(음) fingerprint·reactivation |
| G3 | H3 targeted unit — 관측 근거 없는 경우 후보 미생성 포함 |
| G4 | H4 deterministic(1개) / margin / rerank / invalid·실패 fallback |
| G5 | `python -m pytest tests -q` 전체 |
| G6 | `ruff check src tests examples` |
| G7 | diff 리뷰: scope creep, role-specific router `if`, guessed path/target 여부 |

기존 테스트를 삭제·약화·xfail 우회하지 않는다. 계약이 실제로 바뀌어 기대값이
낡아진 경우에는 새 계약에 맞게 정정하고 결과 문서에 사유를 적는다.

## 8. Rollback 의미

- 되돌리기 단위는 **commit**이다. H0/H1/H2/H3/H4를 각각 독립 commit으로 남기므로
  문제가 생긴 축만 `git revert <sha>`로 걷어낼 수 있다.
- git 밖의 상태를 바꾸지 않는다: DB migration 없음, checkpoint schema 변경 없음,
  engagement/app 설정 파일 형식 변경 없음. 이전 task의 SQLite checkpoint가 있어도
  그대로 재생된다.
- provider 등록은 가법적이다. H1/H3 rollback은
  `default_provider_registry`에서 해당 등록을 제거하는 것으로 이전 동작
  (playbook only)으로 되돌아간다.
- baseline으로 완전히 돌아가려면 `git switch --detach 6a69445`면 충분하고,
  이전 task의 detached 작업은 `orch-feedback-backup-84247a8` tag에 남아 있다.
- 부작용이 큰 실패(예: lease/idempotency 의미 훼손)를 발견하면 해당 축 구현을
  중단하고 관리 스레드에 보고한다(plan의 stop-and-report 조건).

## 9. 비목표

plan §6을 그대로 따른다: 별도 blackboard/memory DB, Core 금융 field, Control Plane
재작성, WorkLedger → checkpoint 대체, Analysis/Reporting의 schedulable화, 자유-form
ToolId, 표준 경로 추측, `PLANNING_CONTENT_KEYS` 전면 재설계.
