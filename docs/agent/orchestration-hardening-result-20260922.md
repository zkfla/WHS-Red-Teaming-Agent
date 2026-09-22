# ORCH-HARDENING-20260922 — 구현 결과

> 작업 ID: `ORCH-HARDENING-20260922`
> 기준 commit: `6a69445` (dev-orchestration) / 실행 protocol: `489edc7`
> 실행 worktree: `C:\\Users\\qkrgu\\.codex\\worktrees\\4261\\WHS-Red-Teaming-Agent`
> 관리 스레드: `01a0c26d-09c5-7341-93d3-d8a90d8ea3c8`
> 상태: **완료** — H1~H4 전부 구현·검증. 남은 실패는 전부 기존 환경 문제.

## 1. 커밋

| commit | 내용 |
|---|---|
| `489edc7` | 실행 protocol (코드 이전) |
| `fb4762b` | H0 — baseline 결함 2건 수리 |
| `342cbc2` | H1 — graph-aware ReconCandidateProvider |
| `897f553` | H2 — relation-aware observation / 재활성화 |
| `a84d87e` | H3 — evidence-first session comparison / posture |
| `e9b139d` | H4 — observed-fact signals + hybrid planner |
| `d745b9b` | H4 correction — unique candidate_key 옵션 identity + rerank context 복원 |
| H1 correction | final review — target_mismatch blocker 수정(§3.1). SHA는 관리 스레드에 반환 |
| H1 correction 2 | second final review — OIDC mapping dead end 수정(§3.2). SHA는 관리 스레드에 반환 |
| 본 문서 commit | 결과 문서 (HEAD) |

## 2. H0 — baseline 정렬과 고정

### 2.1 정렬 (비파괴)

worktree는 이전 task의 detached `84247a8`에 있었고 그 HEAD는 `6a69445`의
조상이 아니었다(공통 조상 `0f71f81`). 다음만 사용했다:
`git tag -f orch-feedback-backup-84247a8 84247a8` 로 이전 작업을 ref에 고정한 뒤
`git switch --detach 6a69445`. `reset --hard` / `checkout --` /
`clean` 은 쓰지 않았고, 관리 체크아웃은 읽기만 했다. 이전 두 commit은 결과 문서
자구 수정뿐이며 baseline의 같은 문서가 이미 그 내용을 담고 있다.

### 2.2 baseline 측정과 결함 수리

정렬 직후 전체 스위트: **1010 passed, 15 failed, 3 skipped**.
이 중 13건은 기존 환경 문제(심볼릭 링크 12 + cp949 1)이고, 나머지 2건은 baseline
결함이어서 수리했다(테스트 한정, production 무변경):

1. `test_agent_loop.py` — `AgentLoopRunner.run(runtime, *, initial_event_id=None)`
   로 커진 시그니처를 monkeypatch fake가 따라오지 못해 `TypeError`로 실패했다.
   fake 시그니처를 실제 계약에 맞췄다.
2. `test_result_mapper.py` — `'value' not in str(cookie_metadata)` 라는 단언이
   mapper가 스스로 넣는 `values_omitted: True` 플래그 이름에 걸렸다. 쿠키 값이
   복사되지 않는다는 사실은 `_safe_response_metadata`/`map_web_http_request`
   양쪽의 명시 allowlist로 확인했고, 단언을 의도대로 정확히 바꿨다(원문 값 문자열
   부재 + per-cookie `value` key 부재). 기존 substring 검사보다 강하다.

## 3. H1 — Graph-aware ReconCandidateProvider

`agents/recon.py`(신규) + `default_provider_registry`에서 `playbook:recon`
등록. central router에 role별 `if`는 없다.

| 항목 | 상태 | 근거 |
|---|---|---|
| SERVICE-only bootstrap | 완료 | SERVICE + no ENDPOINT + HOST 존재 + target_ref 해석 + catalog 실행 가능일 때만 `recon.http_probe` 후보. path는 도구 기본값 `"/"` |
| Web의 recon 도구 직접 호출 금지 | 완료 | 후보 소유자는 Recon provider뿐 |
| html inventory | 완료 | **관측된 HTTP 200** 을 가진 ENDPOINT에만(아래 근거) |
| openapi inventory | 완료 | 그래프에 이미 존재하는 경로 이름이 API 문서를 가리킬 때만 |
| oidc inventory | 완료 | 같은 원칙(discovery 문서) |
| tls inspect | 완료 | SERVICE의 transport fact가 HTTPS를 실제로 말할 때만 |
| 표준 경로 추측 금지 | 완료 | /openapi.json · /.well-known/openid-configuration 등을 생성하지 않음(음성 테스트) |
| Catalog 증명 시에만 후보 | 완료 | `can_execute()`, 미충족 시 `no_executable_service_discovery_tool` diagnostic |

**근거 강도 (관리 리뷰 1 반영)** — 세 신호를 의도적으로 배제했다:

- **포트 번호**(80/443/8080/8443...)는 HTTP 증거로 쓰지 않는다. 포트는 할당이지
  리스너가 실제로 HTTP를 말한다는 측정이 아니다. 그래서 bootstrap gate는 SERVICE의
  `application_protocol` 이 `http`/`https` 인 경우만 받는다.
- **HSTS 헤더 존재**는 TLS 종료를 *암시*할 뿐 transport fact가 아니므로 TLS 후보
  gate로 쓰지 않는다. `application_protocol` 또는 `scheme` 가 `https`
  인 SERVICE fact만 인정한다. 그 결과 현재 파이프라인에서 HTTPS fact를 만드는
  producer가 없으면 이 후보는 dormant다 — 근거가 없으면 만들지 않는다는 원칙에 맞다.
- **HTML inventory 의 200 제한**은 plan의 "관측된 응답"보다 좁다. 의도적이며 근거는
  `recon.html_inventory` 자체 계약이다: 정확히 HTTP 200 + `text/html` 이어야
  하고 아니면 `ToolInputError` 다. 401/403/3xx 관측에 대해 후보를 내면 실패가
  이미 알려진 호출에 action과 예산을 쓰게 된다. 즉 추론이 아니라 도구의 선행조건을
  관측값과 맞춘 것이다. openapi/oidc는 plan 문구대로 "경로가 그래프에 존재"가
  조건이라 status를 요구하지 않는다.

### H1 테스트

- `tests/unit/test_orchestration_recon_provider.py` (26): bootstrap 양성/음성
  (endpoint 존재, HOST 부재, 비HTTP 서비스, 포트만, TLS 서비스명만, target_ref 부재,
  catalog 불가, playbook probe와 중복 금지, probe retired 후 takeover), inventory
  양성/음성(200 vs 401 vs 무응답, 표준 경로 미생성), TLS 양성/음성,
  그리고 **생성된 draft를 실제 `CandidateCompiler` 에 통과시키는 회귀 테스트**
  (§3.1 참고).
- `tests/integration/test_recon_service_bootstrap.py` (2): **SERVICE-only →
  recon.http_probe → ENDPOINT → Web/API 실행** 전 구간, 그리고 복구 후 재실행 없음.

### 3.1 H1 correction — target_mismatch blocker (final review)

**발견**: `_inventory_draft` 는 `HostTarget(host=host)` 를 실행 target으로
쓰면서 `target_node_ids` 에는 ENDPOINT 노드를, `_tls_draft` 는 HostTarget을
쓰면서 SERVICE 노드를 넣었다. `node_id_for_target` 는 HostTarget을 HOST로
해석하므로 `CandidateCompiler` 의
`execution_target_id not in draft.target_node_ids` 검사에 걸려
**inventory/TLS draft가 전부 `target_mismatch` 로 버려졌다.** 단위 테스트가
provider 출력만 보고 compile 단계를 지나지 않은 것이 구멍이었다.

**수정** (compiler 불변조건도 Core/Capability 계약도 건드리지 않았다):

- **inventory** (html/openapi/oidc): 실행 target을
  `EndpointTarget(host, path, port, graph_node_id=node.id)` 로 바꿨다. 관측된
  endpoint 노드가 곧 실행 대상이고 `target_node_ids=[node.id]` 와 정확히 일치한다.
  Mapper도 이 target에서 `host`/`port` 를 읽고
  `node_id_for_target(target)` 를 발견 edge의 출발점으로 쓰므로 의미가 더
  정확해졌다(이전에는 HOST가 출발점이었다).
- **TLS**: 실행 target을 `HostTarget(host, graph_node_id=host_id)`,
  `target_node_ids=[host_id]` 로 맞췄다. SERVICE 노드는 target이 아니라
  provenance로 남긴다 — `hypothesis.metadata["source_node_id"]` 와
  `step_id` 가 후보를 정당화한 SERVICE를 가리킨다.

**ServiceTarget 안 (검토 후 채택하지 않음)**: 관리 제안대로 검증했으나
`recon.tls_inspect` 의 Mapper인 `map_recon_tls_inspect` 는
`_host_context` 에서 `isinstance(target, HostTarget)` 를 **하드 요구**한다.
ServiceTarget을 주면 `GraphMutation()` 를 반환해 결과가 **조용히 버려진다** —
compiler는 통과하지만 아무것도 매핑되지 않는 dead end다. 게다가 이 도구는
`resolve_target(policy, target_ref)` 로 origin에 handshake하고 결과를
`service:<host>/tls` 노드로 *출력*하므로, SERVICE 노드는 실행 대상이 아니라
산출물이다. 따라서 HostTarget + HOST 노드 정렬이 (a) 컴파일되고 (b) 실제로 매핑되는
유일한 조합이다. 이 판단은 `test_the_tls_mapper_only_maps_a_host_scoped_target`
로 고정했다(HostTarget → SERVICE 노드 + `tls_handshake` 관측,
ServiceTarget → 빈 mutation).

**회귀 테스트** (이 버그를 잡지 못하던 구멍을 메운다):

- `test_generated_inventory_and_tls_drafts_compile_cleanly`: 한 그래프에서
  html/openapi/oidc/TLS draft를 실제 `CandidateCompiler` 로 컴파일해
  `target_mismatch` 0건을 확인하고, 네 도구가 모두 executable이 됨을 단언한다.
  추가로 컴파일된 모든 후보에 대해
  `node_id_for_target(plan_step.resolved_target()) in draft.target_node_ids`
  불변식을 직접 검사한다.
- `test_the_bootstrap_draft_compiles_cleanly`: bootstrap draft도 동일하게
  compile 통과, rejection 0건.
- `test_an_inventory_draft_pointing_at_the_host_is_still_rejected`: HOST target을
  끼워 넣으면 **여전히** `target_mismatch` 다 — compiler 불변조건은 약화되지
  않았음을 고정한다.
- `test_an_observed_https_service_justifies_a_tls_inspection` 에 실행 target ==
  graph target, SERVICE provenance 보존 단언을 추가.

### 3.2 H1 correction 2 — OIDC mapping dead end (second final review)

**발견**: §3.1에서 inventory 실행 target을 `EndpointTarget` 으로 바꾼 것은
html/openapi에는 맞았지만 OIDC에는 dead end를 만들었다. `map_recon_oidc_inventory`
는 `_host_context` 를 호출하고 그 안에서 `isinstance(target, HostTarget)` 를
하드 요구한다. 그래서 OIDC 후보는 compiler를 통과하지만 mapper가
`GraphMutation()` 를 돌려주어 **아무것도 그래프에 남지 않았다.**
compile만 검사하고 mapper 실행까지 가지 않은 것이 §3.1과 같은 형태의 두 번째 구멍이다.

**수정** (수정 범위는 orchestration provider 하나, Mapper/Core 계약 무변경):

- **html / openapi**: `EndpointTarget` 유지. 두 Mapper는 공통으로
  `_common_context` 를 쓰며 `getattr(target, "host")`,
  `getattr(target, "port")`, `node_id_for_target(target)` 만 읽는다 —
  HostTarget 요구가 없음을 실제 소스로 확인했다. EndpointTarget이라 발견 edge의
  출발점이 그 endpoint가 되는 편이 오히려 정확하다.
- **OIDC**: 실행 target을 `HostTarget(host, graph_node_id=host_id)`,
  `target_node_ids=[host_id]` 로 두었다. 관측된 ENDPOINT 노드와 path는
  provenance로 보존한다 — `hypothesis.metadata["source_node_id"]`(관측 endpoint)
  와 `source_path`, 그리고 `step_id` 가 그 endpoint를 가리킨다.
  conflict key도 실행 target을 따라 `target:host:<host>` 가 된다.

**구조적 재발 방지**: `_inventory_draft` 가 `target_node_ids` 와
`conflict_keys` 를 손으로 짝지어 넘기지 않고
`node_id_for_target(target)` 에서 **도출**한다. 실행 target과 graph target이
어긋나는 상태 자체가 표현 불가능해졌다(compiler의 `target_mismatch` 는 그대로
유지되며, 어긋난 draft를 억지로 넣으면 여전히 거부된다).

**회귀 테스트**:

- `test_every_inventory_draft_actually_maps_a_successful_result`: html/openapi/oidc
  각각에 대해 (1) 실제 `CandidateCompiler` 컴파일, (2) **실제
  `default_result_mapper_registry()` 로 성공 ToolResult 매핑**,
  (3) non-empty mutation + 최소 하나의 ENDPOINT 노드 생성을 단언한다. openapi는
  RESOURCE template도 만들므로 "최소 하나" 기준이다.
- `test_the_tls_mapper_only_maps_a_host_scoped_target` 는 §3.1에서 추가한
  HostTarget 하드 요구 검증으로 그대로 유지한다.
- `test_an_inventory_draft_pointing_at_the_host_is_still_rejected` 로 negative
  `target_mismatch` 불변조건도 유지한다.

## 4. H2 — Relation-aware observation / role 재활성화

| 항목 | 상태 | 근거 |
|---|---|---|
| stable edge digest / count | 완료 | `GraphObservation.edge_counts`, `relation_digests` — `_relation_content()` 가 endpoint + non-volatile property만 해싱 |
| view가 edge를 함께 관측 | 완료 | `AttackGraphView.observe()` 가 `list_edges()` 를 넘김 |
| role별 edge 구독 | 완료 | `ContextProfile.include_edge_types`, `observation_fingerprint(include_edge_types=...)` |
| Web RESOURCE + relevant relation | 완료 | profile에 `RESOURCE` 와 `CAN_ACCESS`/`OWNS`/`AUTHENTICATES_TO`/`OBSERVED`/`EXPOSES` |
| volatile provenance/time 제외 | 완료 | `VOLATILE_RELATION_KEYS` denylist(provenance/step/created_at/evidence_ids 등) |
| 구독 안 한 role은 영향 없음 | 완료 | `include_edge_types` 기본값이 빈 튜플이라 다른 role fingerprint는 불변 |

### H2 테스트

`tests/unit/test_orchestration_relation_reactivation.py` (10):
relevant edge-only(CAN_ACCESS, AUTHENTICATES_TO) → fingerprint 변화,
relation property 변화 → 변화, **unrelated edge type(RELATED_TO) → 불변**,
**volatile property만 추가 → 불변**, 구독 안 한 role(recon) → 불변,
unrelated node type(ASSET) → 불변, profile 배선 검증.

기존 `test_orchestration_dynamic_candidates.py` 의 "무관한 노드" 예시는
RESOURCE가 이제 Web profile 안으로 들어왔으므로 ASSET으로 정정했다(의도 변경,
약화 아님).

## 5. H3 — Web evidence-first follow-up

| 항목 | 상태 | 근거 |
|---|---|---|
| `web.http_compare_sessions` | 완료 | 같은 path에 **서로 다른 operator-managed session의 실제 observation 2개** 일 때만 |
| 등록 세션 교차검증 | 완료(리뷰 반영) | 관측된 session_ref가 `AuthSession` 목록에 있고 그 path를 허용할 때만 eligible |
| `web.http_security_posture` | 완료 | 실제 응답이 관측된 ENDPOINT + 아직 anonymous posture 관측이 없을 때만 |
| observation-only | 완료 | 둘 다 `assessment_kind="observation"`, expectation 없음, `violation_if_confirmed=False` |
| 기존 규칙과 중복 금지 | 완료 | request/context key로 구분, 이미 비교/관측된 쌍은 재생성 안 함 |
| catalog-bound 아니면 미생성 | 완료 | `can_execute("web", "response_analysis")` |

**경계 명시**: `AuthSession` 에는 `principal_ref` 가 없다. 따라서 두 ref가
실제로 서로 다른 principal인지는 orchestration이 알 수 없고, Capability의
`http_compare_sessions` 가 "two distinct principals bound to this target are
required" 로 최종 검증한다. orchestration은 "등록된, 그 path에 허용된, 실제로 응답한
두 identity" 까지만 보증한다.

### H3 테스트

`tests/unit/test_orchestration_web_analysis_followups.py` (13): 비교 양성,
관측 1개만, **미등록 session_ref 2개(음성)**, 다른 path로 등록된 세션(음성),
이미 비교됨, catalog 불가, posture 양성/무응답/이미 관측됨,
session-scoped posture는 anonymous를 대체하지 않음, violation 미주장.

## 6. H4 — observed-fact signals + hybrid planner

| 항목 | 상태 | 근거 |
|---|---|---|
| provider 신호를 관측 사실로 | 완료 | H1/H3 후보가 `expected_information_gain`·`evidence_gap_reduction`·`downstream_unblock_count`·`expected_cost` 를 실제 관측/도구 계약(요청 수)에서 채움. `operational_risk` 는 계속 ToolDescriptor/compiler authority |
| 후보 1개 → deterministic | 완료 | `last_decision="single"`, LLM 호출 0 |
| clear margin → deterministic | 완료 | `planner_score_margin` (기본 0.15), LLM 호출 0 |
| ambiguous → bounded top-K rerank | 완료 | `planner_rerank_top_k` (기본 3), shortlist만 프롬프트에 노출 |
| 실패/invalid → deterministic top-1 | 완료 | no client / 예외 / 미제공 option 모두 top-1 |
| LLM이 tool/target/candidate 발명 불가 | 완료 | 반환 option_id가 shortlist 안에 있어야 함 |
| **target별 candidate identity** | 완료(리뷰 반영) | option identity는 `ExecutableCandidate.candidate_key` |

### H4 correction (관리 리뷰 2건 반영)

**버그**: `available_steps()` 는 하나의 playbook step을 여러 target에 binding해도
`step_id` 를 그대로 유지한다. 그래서 `candidate_scores={step_id: total}` 는
target별 점수를 덮어썼고, `_rerank` 의 `offered={step.step_id: step}` 도 같은
충돌을 냈다.

**수정**:

- `plan_node` 가 candidates와 **정렬된** `candidate_keys` 와
  `candidate_scores` 를 `candidate_key` 기준으로 넘긴다.
- `HybridPlanner` 의 정렬과 shortlist가 모두 option key 기준이고, LLM에는
  고유 `option_id` 로 제시한 뒤 반환을 원래 `PlanStep` 으로 mapping한다.
  `HYBRID_CHOICE_SCHEMA` 는 `option_id` 를 요구한다.
- `PlanningContext.candidates` 의 원본 `PlanStep` 계약과
  `PlanStep.step_id` / PM action naming은 변경하지 않았다.

**프롬프트 회귀 수정**: `_render_options()` 가 기존 `LLMPlanner._render()`
가 주던 open hypotheses · findings · attempted · evidence excerpts를 그대로 싣고,
각 option에 `target_display_name(step.resolved_target())` 기반의 실제 target
(EndpointTarget이면 path까지)을 표시한다. 프롬프트는 `PlanStep.arguments` 를
직접 dump하지 않아 request body·session 참조가 새지 않는다(음성 테스트로 고정).

### H4 테스트

`tests/unit/test_orchestration_hybrid_planner.py` (21): single/margin/rerank/
fallback 4경로, 미제공·발명 option 거부, shortlist 밖 거부, 예외 시 secret 미노출,
no client, 후보 없음, **동일 step_id 두 target → 점수대로 정확히 top-1**,
**동일 step_id ambiguous → 두 번째 unique candidate 선택 가능**,
**invalid option id → top-1 fallback**, 프롬프트에 option_id 노출,
프롬프트에 두 target 구별 표시, bounded context 보존, arguments 미유출,
`build_planner` 배선(기존 `LLMPlanner` 계약 보존).

## 7. plan §7 완료 조건 대조

| # | 조건 | 상태 | 근거 |
|---|---|---|---|
| 1 | SERVICE-only가 catalog-bound recon으로 ENDPOINT를 만들고 Web을 깨운다 | 완료 | `test_recon_service_bootstrap.py` |
| 2 | relevant edge-only는 재활성화, unrelated edge는 아님 | 완료 | H2 양·음 테스트 |
| 3 | RESOURCE가 Web/API planning context에 포함 | 완료 | profile 검증 테스트 |
| 4 | 기존 inventory/compare/posture Tool이 근거 있을 때만 Candidate | 완료 | H1/H3 양·음 테스트 |
| 5 | 새 후보도 CandidateCompiler + Control Plane 통과 | 완료 | 후보는 전부 draft→compile→POLICY 경로, 기존 통합 테스트 유지 |
| 6 | status/response 하나로 Finding을 만들지 않음 | 완료 | 신규 후보는 모두 observation-only, expectation 없음 |
| 7 | single/clear-winner는 LLM 없이, ambiguous top-K만 LLM | 완료 | H4 테스트(호출 0 검증 포함) |
| 8 | checkpoint/lease/parallel dispatch/approval resume 미파괴 | 완료 | H1~H4가 건드리지 않음, 전체 스위트 통과 |

## 8. 검증 게이트 결과

깨끗한 venv(`mcp 2.2.0`)에서 실행.

| 게이트 | 결과 |
|---|---|
| G1 H1 targeted(compile + mapper 회귀 포함) + Recon→Endpoint→Web 통합 | 26 + 2 passed |
| G2 H2 edge 양성/음성 fingerprint·재활성화 | 10 passed |
| G3 H3 targeted(근거 없음 음성 포함) | 13 passed |
| G4 H4 deterministic/margin/rerank/fallback | 21 passed |
| G1~G4 합계(H1~H4 targeted 전체 + mapper 회귀 포함) | **186 passed** |
| G5 `python -m pytest tests -q` | **1084 passed, 13 failed, 3 skipped** |
| G6 `ruff check src tests examples` | **All checks passed!** |
| G7 diff 리뷰 | §11 |

baseline `6a69445`: 1010 passed / 15 failed(환경 13 + baseline 결함 2).
최종: 1084 passed / 13 failed. **남은 13건은 전부 기존 환경 문제**이며 이번 변경
파일과 무관하다:

- 12건 `OSError [WinError 1314]` — Windows 심볼릭 링크 권한 없음
  (`test_finance_extensions.py` 10 + `test_proxy_capture_connector.py` 2).
  같은 뿌리로 `os.O_NOFOLLOW` / `os.fchmod` 부재도 확인된다.
- 1건 `test_cli_smoke.py` — `validate.py` 가 체크 문자를 cp949 콘솔에 출력해
  `UnicodeEncodeError` → exit 2. UTF-8 콘솔에서는 통과한다.

## 9. 한계 / 이번 사이클에서 하지 않은 것

1. **TLS 후보가 현재 파이프라인에서는 dormant**하다. HTTPS를 명시하는 SERVICE
   transport fact를 만드는 producer가 아직 없다(리뷰에 따라 HSTS/포트로 대체하지
   않았다). producer가 생기면 즉시 동작하며 테스트로 고정돼 있다.
2. **AuthSession에 principal 정보가 없다** — compare_sessions의 "서로 다른 principal"
   최종 검증은 Capability 도구 몫이고, orchestration은 등록·path허용·실제응답까지만
   보증한다(§5).
3. **HTML inventory는 200 관측에 한정**된다(§3 근거). session이 필요한 HTML 표면은
   `html_inventory` 도구가 session 파라미터를 갖지 않아 애초에 대상이 아니다.
4. **edge 구독은 role별 수동 선언**이다. 새 role이 relation을 계획에 쓰려면
   `ContextProfile.include_edge_types` 에 추가해야 한다(plan §6의 per-provider
   fact subscription 후속 과제와 같은 성격).
5. **`PLANNING_CONTENT_KEYS` 전면 재설계는 비목표**로 남겼다(plan §6).
6. **priority 신호는 H1/H3 신규 후보에 채웠고**, 기존 web auth 계열 후보의 신호는
   baseline 값을 유지했다(동작 변경 최소화).

## 10. 변경 파일 (22개, `6a69445` 대비 — 본 결과 문서 포함)

소스(11):
`attack_graph/observation.py`(edge count/relation digest),
`attack_graph/view.py`(observe가 edge 포함),
`agents/definition.py`(include_edge_types + Web RESOURCE/relation),
`agents/providers.py`(fingerprint에 relation + provider 등록),
`agents/recon.py`(신규 ReconCandidateProvider),
`agents/registry.py`(revision fingerprint에 edge 구독 전달),
`agents/planners.py`(HybridPlanner + option id 프롬프트),
`agents/web.py`(session comparison / posture follow-up),
`nodes/plan.py`(candidate_keys/scores 전달 + planner 설정),
`orchestration/progress.py`(include_edge_types),
`orchestration/settings.py`(planner_score_margin / planner_rerank_top_k).

문서(2): `docs/agent/orchestration-hardening-protocol-20260922.md`,
`docs/agent/orchestration-hardening-result-20260922.md`.

테스트(9): `tests/unit/test_orchestration_recon_provider.py`(신규),
`tests/unit/test_orchestration_relation_reactivation.py`(신규),
`tests/unit/test_orchestration_web_analysis_followups.py`(신규),
`tests/unit/test_orchestration_hybrid_planner.py`(신규),
`tests/integration/test_recon_service_bootstrap.py`(신규),
`tests/integration/test_agent_loop.py`(H0 수리),
`tests/unit/test_result_mapper.py`(H0 수리),
`tests/unit/test_orchestration_dynamic_candidates.py`(무관 노드 예시 정정),
`tests/unit/test_orchestration_web_followups.py`(H3 반영).

## 11. 불변조건 준수 (diff 리뷰)

| 불변조건 | 확인 |
|---|---|
| 자유-form ToolId 금지 | 모든 신규 후보가 카탈로그 도구 id를 명시, compiler가 검증 |
| 표준 경로 추측 금지 | bootstrap path만 `"/"`(도구 기본값), 나머지는 관측 경로. 음성 테스트 있음 |
| status-only Finding 금지 | 신규 후보 전부 observation-only |
| side-effect node blanket retry 금지 | retry 정책을 추가하지 않았다 |
| 중앙 tool-name if chain 금지 | ResultMapperRegistry/registry dispatch만 사용 |
| Core 금융 field 금지 | `core` 무변경 |
| Analysis/Reporting schedulable 금지 | 무변경, 기존 정합 테스트 통과 |
| Capability 경계 / MCP adapter·gateway·catalog | 무변경, `langchain-mcp-adapters` 미사용 |
| WorkLedger lease/heartbeat/conflict/idempotency | 무변경 (H1에서 prerequisite_ids를 work 의존성으로 잘못 쓰던 것을 제거) |
| LLMClient + LiteLLM | 그대로 사용, planner는 `LLMClient` port만 호출 |
| LangGraph StateGraph/Send/checkpointer | 무변경 |
| `update.py` tool 분기 금지 | 무변경 |
