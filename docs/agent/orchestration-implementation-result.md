# ORCH-AGENT-FEEDBACK-20260921 — 구현 결과

> 작업 ID: `ORCH-AGENT-FEEDBACK-20260921`
> 실행 worktree: `C:\\Users\\qkrgu\\.codex\\worktrees\\4261\\WHS-Red-Teaming-Agent`
> 실행 protocol: `docs/agent/orchestration-implementation-protocol.md`
> 상태: **완료** (요구사항 전부 구현·검증, 남은 실패는 전부 기존 환경 문제)

## 1. 기준 커밋과 최종 커밋

| 구분 | commit | 설명 |
|---|---|---|
| 관리 체크아웃 기준 | `73da08f` + 미커밋 3파일 | protocol 규칙 + 인계된 dirty fix |
| 실행 worktree 시작점 | `4933698` | worktree 생성 시점 (protocol 커밋 이전) |
| protocol 이관 | `e213fff` | `git cherry-pick 73da08f...`, 관리 체크아웃 비파괴 |
| 회귀 baseline 확정 | `7e49b1e` | 관리 체크아웃의 3개 dirty fix 이관 |
| 실행 protocol 기록 | `bd2a3be` | 구현 전 protocol 커밋 (프로젝트 규칙) |
| 코드 변경 1 | `058c9aa` | graph-driven 후보 생성 + Web multi-step + evidence-gated Finding 승격 |
| 최종 결과 commit | `833b051` | 본 문서를 포함한 최종 커밋 (부모 `bd2a3be` protocol) |

이관은 blob 해시 일치로 검증했다: `compiler.py` `86d5df4`,
`plan.py` `14b85f1`, `test_orchestration_candidates.py` `3dcf8f6`
— 관리 체크아웃과 바이트 단위로 동일하다. 이관 전 `plan.py`(4933698)는
`SyntaxError: '{' was never closed`(line 205)로 import 자체가 불가능했으므로,
이관 후 상태가 유일하게 실행 가능한 baseline이다. 관리 체크아웃은 읽기만 했다.

## 2. 아키텍처 변경 요약

기존 `OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN` 경계,
`ActionProposal -> ControlPlane -> ToolInvocation` 게이트, PM action naming,
Event Bus 계약은 그대로 유지했다. 바뀐 것은 "무엇을 계획할 수 있는가"와
"무엇을 주장할 수 있는가"의 두 지점이다.

### 2.1 재활성화가 노드 '내용'을 본다 (`attack_graph/observation.py`, `orchestration/progress.py`)

기존 role-scoped revision fingerprint는 노드 **정체성**(id/count/label)만 해싱했다.
그래서 endpoint가 익명 401에서 같은 세션 403으로 바뀌어도 — id도 context key도
count도 그대로이므로 — Web 역할이 깨어나지 않았다.
`GraphObservation.content_digests`를 추가해 각 노드 타입별로
`PLANNING_CONTENT_KEYS` 화이트리스트의 planning-relevant 내용을 해싱하고,
`observation_fingerprint`가 이를 포함하도록 했다. 화이트리스트인 이유는
last observed step·누적 evidence id·attempt log·provenance 같은 휘발성
bookkeeping이 들어가면 매 turn 모든 role이 "새 일이 있다"고 보이기 때문이다.
truncation은 prompt 크기 결정이므로 digest 계산은 **전체** 노드에 대해 한다.

### 2.2 provider가 실제 catalog 실행 가능성을 본다 (`agents/providers.py`, `nodes/plan.py`)

`GraphCandidateContext`에 `available_operations`·`operations_verified`
·`planning_fingerprint`를 명시하고 `can_execute()` 판정을 제공한다.
`plan_node`가 provider가 선언한 `declared_operations` + playbook step을
live Tool Catalog에 실제로 조회해 "지금 실행 가능한 (capability, operation)"만
넘긴다. **catalog가 없으면** `operations_verified=False` = "unknown(없음이 아님)"
→ 기존 declared-binding 신뢰 동작을 유지한다 (compiler가 이미 문서화한 규칙과 동일).

### 2.3 Web이 그래프 관측값만으로 multi-step이 된다 (`agents/web.py`)

`WebCandidateProvider`가 `propose_graph`를 구현해 4가지 bounded 후속을
도출한다: (1) 익명 401 + 등록 세션 → 세션 probe(기존 유지), (2) 세션 요청이
401/403인데 operator가 allow로 선언 → 없는 **익명 baseline** 요청, (3) 세션 바인딩
403 + 같은 path를 커버하는 다른 등록 세션 → 다른 identity 비교, (4) catalog가
`auth_attempt` action에 `web.http_compare_anonymous`를 바인딩한 경우 →
한 번의 실제 도구 호출 비교. 익명 403은 후속을 만들지 않는다 — 자격증명 부재로
설명되므로 권한 주장을 읽어내면 관측에 없는 의미가 된다.

### 2.4 Finding 승격을 검증과 분리 (`orchestration/findings.py`, `nodes/verify.py`)

verifier는 "이 결과가 무엇을 뜻하는가", promoter는 "이 주장을 근거 있게 말할 수
있는가"를 답한다. `FindingPromoter`는 VIOLATION만, evidence id 1개 이상,
hypothesis가 실제 confirmed일 때만 승격하며 claim key로 중복을 막는다.
거부되면 verdict를 UNKNOWN으로 내리고 기존 `ObservationRecorded`에
`finding_promotion_refused`를 실어 관측 가능하게 남긴다 (새 이벤트 없음).

### 2.5 생명주기 이벤트 정합 (`nodes/verify.py`, `nodes/replan.py`)

`HypothesisResolved`를 실제 해소 시점에 발행한다. hypothesis는 후속 검증이
실제로 가능할 때만 open으로 남고(반복 가능 step), 아니면
`no_follow_up_verification_available` 사유와 함께 inconclusive로 닫는다
(Work Ledger가 terminal non-repeatable 후보를 영구 dedup하므로 후속이 존재하지 않음).
`ReplanTriggered`는 **결정일 때만** 발행한다: terminal outcome, 또는 role별
planning-input fingerprint가 바뀌어 재활성화된 경우. turn을 한 번 더 도는 것은
결정이 아니므로 조용히 넘어간다.

### 2.6 관측 가능한 rejection (`candidates/compiler.py`, `agents/base.py`)

reason 어휘를 정본화했다: `capability_unavailable`(namespace 자체가 비어 있음)
/ `operation_unsupported`(namespace는 있으나 이 intent에 바인딩 없음)
/ `tool_unhealthy`(namespace 전체가 unhealthy) / `schema_mismatch`.
draft가 **아예 만들어지지 않던** 경우(`$path`를 채울 수 없는 target)는
`DraftDrop` → `CandidateRejected`로 발행해 조용히 사라지지 않게 했다.
malformed/non-JSON 인자는 `cand_invalid_` 결정론적 키로 안전하게 보고된다.

### 2.7 Result Mapper 확장 (`observations/web_http.py`, `observations/mapper.py`)

`web.http_compare_anonymous` mapper를 registry에 등록했다. 도구가 실제로
돌려준 값(양측 status, `same_status`, `same_body`, 도구 자신의
`verdict="observation_only"`)만 기록하고 취약점으로 승격하지 않는다.
`is_http_status`는 `TypeGuard[int]`로 공유해 두 mapper의 중복 검증을 제거했다.
`update.py`에는 tool별 분기를 추가하지 않았다.

## 3. 요구사항별 완료 매트릭스

### 3.1 핸드오프 필수 동작

| # | 요구사항 | 상태 | 근거 |
|---|---|---|---|
| 1 | 동적 graph-driven 후보 생성 (관측→가설→새 후보→선택 반복) | 완료 | `GraphCandidateContext` 확장, `plan_node`가 실제 catalog 조회, Web `propose_graph` 4규칙 |
| 1a | LLM은 컴파일된 실행 후보 중 선택만, 도구명 발명 불가 | 완료(기존 유지) | `CandidateCompiler`가 도구/schema/health 검증, planner는 step_id만 선택. 신규 후보도 동일 경로 통과 |
| 1b | 재활성화/dedup이 의미 있는 planning-input fingerprint 기반 | 완료 | `content_digests` 기반 role-scoped fingerprint. revision counter 미포함 |
| 1c | 무관한 graph 변화로 재발화하지 않음 | 완료 | 테스트: `test_an_unrelated_node_type_does_not_change_the_role_fingerprint` |
| 2 | Web Agent가 진짜 multi-step | 완료 | 401/403 세션 비교 4규칙 + `web.http_compare_anonymous` 재사용 |
| 2a | 새 후보 생성 전 실제 MCP/tool 바인딩·schema 확인 | 완료 | `_available_operations`가 live catalog 조회, `can_execute` 게이트 |
| 2b | 실제 web-http capability 재사용 | 완료 | `http_request`, `http_compare_anonymous` 사용. 나머지 web 도구는 §6-1 참고 |
| 2c | ToolResult에 없는 사실 날조 금지 | 완료 | `NEVER_OBSERVED_KEYS` 음성 테스트. Location/WWW-Authenticate/Set-Cookie/raw header/body 미기록 |
| 2d | 관측이 뒷받침할 때만 Endpoint/Parameter/Auth state/Session/Token/Object/Permission 물질화 | 완료 | status code·session_ref·body_sha256·redirect·path_parameters만 기록. token/object/permission은 관측 근거가 없어 미생성 |
| 2e | 보안 헤더 부재 자체는 취약점이 아님 | 완료 | mapper는 `security_headers_present`만 기록, 후보·Finding 도출 없음 |
| 3 | Finding 파이프라인이 evidence-driven | 완료 | `FindingPromoter` evidence/hypothesis 게이트 |
| 3a | Finding이 실제 Evidence·target·hypothesis와 연결 | 완료 | `evidence_ids`, `metadata.target`, `metadata.hypothesis_id`, `claim_key` |
| 3b | observation-only는 overconfident verifier로도 Finding 불가 | 완료 | 테스트: `test_an_observation_only_step_never_becomes_a_finding` |
| 3c | 기존 auth 비교 경로를 evidence 있을 때만 보존·일반화 | 완료 | 기존 `AuthenticationCheck` 유지, 승격은 공통 promoter 경유 |
| 3d | blanket `violation_if_met=True`로 해결하지 않음 | 완료 | 신규 step 어디에도 blanket 설정 없음 |
| 4 | Analysis/Reporting 아키텍처 정합 | 완료 | 둘 다 `schedulable=False` seam, `default_registry`가 spec 미등록, 모든 definition의 provider_id가 resolve됨 |
| 4a | verifier/projector 경로가 실제 도달·테스트됨 | 완료 | `deps.verifier` 매 action 호출, terminal projection은 기존 테스트 + 신규 정합 테스트 |
| 5 | rejection 관측성 (안정적 reason + safe fields) | 완료 | §2.6, reason 10종, 진단 생성이 절대 예외를 던지지 않음 |
| 5a | 기존 EventType/Event Bus로 노출, 중복 의미 미발명 | 완료 | 신규 이벤트 0개 |
| 6 | Tool Result → mapper → GraphMutation, `update.py`는 generic 유지 | 완료 | `ResultMapperRegistry` 등록만 추가. `update.py` 변경 없음 |
| 7 | lifecycle/replanning 이벤트 | 완료 | `HypothesisResolved` 실제 해소 시, `ReplanTriggered`는 의미 있는 재계획에서만 |
| 8 | 확장성 (Recon/WebApi/Mobile/Analysis/Reporting 호환) | 완료 | provider/definition 등록만으로 역할 추가. 오케스트레이션 코어 수정 없음 |

### 3.2 팀원 피드백 P0/P1/P2

| 항목 | 상태 | 비고 |
|---|---|---|
| P0-1 plan.py 문법 오류·기본 실행 복구 | 완료 | 인계받아 이관, compileall/import/test 전부 통과 |
| P0-2 Dynamic Candidate regeneration | 완료 | §2.1, §2.2 + 테스트 |
| P0-3 Web multi-step | 완료 | §2.3 + 테스트 10종 |
| P0-4 Hypothesis → Evidence → Finding | 완료 | §2.4 + 테스트 9종 |
| P0-5 Candidate/Event lifecycle 연결 | 완료 | correlation/causation 체인 테스트 갱신·추가 |
| P1-1 rejection diagnostics | 완료 | §2.6. CLI는 기존 renderer가 이벤트를 사람이 읽게 표시(신규 렌더러 추가 없음) |
| P1-2 Result Mapper 확장·실패 처리 | 완료 | §2.7. mapper 예외는 `ResultMapperFailed`로 남기고 run을 중단시키지 않음(기존 동작 유지·검증) |
| P1-3 Replan 관측성 | 완료 | §2.5. reason/outcome 어휘 도입 |
| P2 Analysis/Reporting 역할 정리 | 완료 | §3.1-4. 서비스(seam) 유지가 의도된 아키텍처이며 문서·registry·테스트가 일치 |

## 4. 실행한 검증과 결과

깨끗한 venv(신규 생성, `mcp 2.2.0` / `rich 14.3.4`)에서 실행했다.
pyproject의 `mcp>=2,<3`을 만족한다.

| 게이트 | 명령 | 결과 |
|---|---|---|
| C | `python -m compileall -q src/redteam/orchestration` | exit 0 |
| C | `python -m compileall -q src` | exit 0 |
| D | 오케스트레이션 단위/회귀 스위트 | 117 passed, 2 xfailed |
| E | `ruff check src tests examples` | **All checks passed!** (baseline은 F401 2건으로 실패 상태였음) |
| E | `ruff format --check src tests examples` | 121 files would be reformatted — **이 저장소의 포매터가 아님**(§5.3) |
| F | `python -m pytest tests -q` | **967 passed, 13 failed, 3 skipped, 2 xfailed** |
| — | `mypy src/redteam` | 2 errors in 2 files — 둘 다 이번에 수정하지 않은 Capability 파일(§5.4) |

### 4.1 baseline 대비 변화

| 시점 | 결과 |
|---|---|
| 이관 직후 (baseline) | 919 passed, 16 failed, 3 skipped, 2 xfailed |
| 최종 | 967 passed, 13 failed, 3 skipped, 2 xfailed |

passed +48, failed −3. 남은 13건은 성격이 전부 환경 문제로 확정됐고(§5),
오케스트레이션 동작 실패는 **0건**이다.

### 4.2 신규/보강 테스트

| 파일 | 내용 |
|---|---|
| `tests/unit/test_orchestration_web_followups.py` | Web 4규칙 양성 + 익명 403/302/헤더·쿠키 비날조 음성 + identity 분리 + catalog 게이트 (10) |
| `tests/unit/test_orchestration_findings.py` | promoter 양성/거부 5사유 + claim 중복 + evidence 정규화 (9) |
| `tests/unit/test_orchestration_dynamic_candidates.py` | context/can_execute, content-digest, role-scoped 재활성화(양·음), fingerprint, DraftDrop, compare mapper(양·음), seam 정합 (16) |
| `tests/unit/test_orchestration_nodes.py` | 관측-only Finding 금지, hypothesis 없는 VIOLATION 거부 관측, drop 이벤트, replan 이벤트 3종, 반복가능 hypothesis open 유지 (신규 6 + 갱신 2) |
| `tests/integration/test_event_identity.py` | action chain에 `HypothesisResolved` 포함, `ReplanTriggered` run identity·causation 전용 테스트 |
| `tests/integration/test_end_to_end.py` | 이벤트 순서에 `CandidateGenerated`·`HypothesisResolved`·`ReplanTriggered` 반영 |
| `tests/unit/test_orchestration_candidates.py` | `capability_unavailable` / `operation_unsupported` / `tool_unhealthy` 구분 |

### 4.3 기존 테스트 중 의도적으로 계약을 갱신한 3건

아래 3건은 핸드오프가 **명시적으로 요구한 동작 변경** 때문에 기대값이 낡아진 것으로,
삭제·약화·xfail 우회가 아니라 새 계약에 맞춘 정정이며 단언은 오히려 구체화됐다.

1. `test_verify_records_evidence_then_reaches_a_verdict` — 이벤트 목록에
   `HypothesisResolved`를 추가하고, 그 payload의 status까지 단언하도록 강화.
2. `test_a_failed_execution_leaves_the_hypothesis_open` →
   `test_a_failed_execution_without_a_follow_up_closes_the_hypothesis` —
   R7이 요구한 "후속 검증이 없으면 inconclusive로 닫고 사유를 남긴다"에 맞춤.
   동시에 반대 방향(`test_a_failed_execution_keeps_a_repeatable_hypothesis_open`)을
   신규 추가해 양쪽을 모두 고정했다.
3. exact 이벤트 목록을 단언하던 `test_a_violation_produces_a_verified_finding_backed_by_evidence`,
   `test_plan_states_a_hypothesis_before_it_acts`,
   `test_update_discovers_service_and_endpoint_from_a_successful_http_probe` —
   새로 발행되는 `HypothesisResolved`·`GraphMutationApplied`·`CandidateGenerated`를
   기대 목록에 포함. (앞의 둘은 이번 변경 이전 baseline에서도 이미 실패하던 항목이다.)

## 5. 검증된 기존 실패/블로커 (내 변경과 구분)

13건 전부 이번 작업 이전부터 존재하던 환경 문제이며, 이번 변경 파일과 무관함을
증거로 확인했다.

### 5.1 Windows 심볼릭 링크 권한 (12건)

`tests/unit/test_finance_extensions.py` 10건 + `tests/unit/test_proxy_capture_connector.py` 2건.
정확한 증거: `OSError: [WinError 1314]` at `pathlib.py:1253 self._accessor.symlink`.
테스트가 `Path.symlink_to`를 쓰는데 Windows에서 SeCreateSymbolicLinkPrivilege
(관리자 또는 개발자 모드)가 없다. 같은 뿌리의 다른 증상으로
`os.O_NOFOLLOW`(session_state.py:19), `os.fchmod`(proxy_capture.py:262)가
Windows에 없는 것도 확인된다. → 로컬 권한 문제이며 코드 결함이 아니다.

### 5.2 콘솔 인코딩 cp949 (1건)

`tests/integration/test_cli_smoke.py::test_full_lifecycle_init_validate_run_status_findings_report`.
재현 시 `ERROR: 'cp949' codec can't encode character` → exit 2. 원인은
`src/redteam/app/cli/commands/validate.py:77`가 체크 표시 문자를 stdout에 그대로
출력하는데, 테스트가 stdout을 파이프로 잡으면 Python이 로케일 인코딩(cp949)을
쓰기 때문이다. UTF-8 콘솔에서는 통과한다. 오케스트레이션 범위 밖이라 고치지 않고
기록만 한다.

### 5.3 `ruff format --check`는 이 저장소의 기준이 아님

121개 파일이 재포맷 대상인데 이는 이번 변경 이전부터 그렇다(변경 파일뿐 아니라
미변경 파일이 대부분). 저장소가 `ruff format`으로 관리되지 않는다는 뜻이다.
작업 범위는 저장소가 실제로 쓰는 `ruff check`이며 이는 통과한다.
기존 코드를 대량 재포맷하는 것은 "기존 결과 보존" 규칙에 어긋나므로 하지 않았다.

### 5.4 mypy 잔여 2건

`src/redteam/capability/connectors/proxy_capture.py:262` `os.fchmod`,
`src/redteam/capability/servers/session_state.py:19` `os.O_NOFOLLOW`.
둘 다 이번에 수정하지 않은 파일(§8 변경 목록에 없음)이고 Windows 비호환 API다.
내가 추가한 코드에서 나온 mypy 오류는 수정 과정에서 37건 → 0건이 됐다.

## 6. 남은 한계

1. **`session_list` / `http_compare_sessions` / `http_security_posture` 미연결.**
   세 도구는 실제로 존재하고 schema도 확인했지만 현 Web provider가 도출하는
   bounded 후속(§2.3)에는 이들을 쓸 근거가 없다. `http_compare_sessions`는
   두 principal 비교라 "왜 그 두 개인가"를 관측에서 도출할 수 없고,
   `http_security_posture`는 헤더/쿠키 플래그 관측인데 이는 취약점 판정이
   아니라 별도 분석 단계가 필요하다. 억지 후보를 만들지 않기 위해 도입하지 않았다.
2. **응답 헤더 기반 후속(Location·WWW-Authenticate·Set-Cookie)은 구조적으로 불가.**
   `http_request`가 이들을 반환하지 않는다. 도구 계약(Capability 소유)이
   바뀌면 mapper+provider 규칙 추가로 확장된다. 현재는 302를 "3xx 관측"으로만
   기록하고 Location 기반 후보를 만들지 않는다(테스트로 고정).
3. **token/object/permission 노드는 물질화하지 않는다.**
   현 도구 출력에 근거가 없다(raw token은 정책상 미노출, object id는 path
   template 파라미터로만 추정). 관측 근거가 생기면 mapper에서 추가한다.
4. **비교 이력은 request key별 최신값 위주로 소비된다.**
   endpoint `observations` 목록은 누적되지만 provider는 최신값을 쓴다.
   이력 전체 분석이 필요하면 Analysis seam 쪽 작업이 필요하다.
5. **재활성화 fingerprint의 화이트리스트는 수동이다.**
   `PLANNING_CONTENT_KEYS`에 새 fact key가 생기면 추가해야 한다.
   누락 시 "내용 변화를 못 보는" 방향의 실패(재활성화 안 됨)로 나타나며,
   잘못된 재발화보다 안전한 쪽이다.
6. **승인 재개는 이번 범위 밖**이며 기존 동작(단발 실행, 렛저 lease 미재사용)을 유지했다.

## 7. 비날조 확인

Graph/Event에 관측되지 않은 사실을 기록하지 않았음을 다음으로 확인했다.

1. **도구 계약 우선 확인**: 새 후보를 만들기 전에
   `redteam/capability/servers/web_http.py`,
   `redteam/capability/servers/network.py`의
   `CapturedResponse.summary()`가 실제로 반환하는 필드를 먼저 읽었다.
   `http_request`는 `status_code`·`body_bytes`·`body_sha256`·
   `redirect`·`security_headers_present`·`body_omitted`·
   `target_ref`·`session_ref`만 노출한다.
2. **금지 키 음성 테스트**: Location/WWW-Authenticate/Set-Cookie/cookie/headers/body가
   어떤 후보 arguments에도 나타나지 않음을 두 테스트로 고정했다.
3. **상태 코드 의미는 프로토콜 정의로만**: 401/403에서 인증 필요/권한 거부를
   유도하되, 그 메커니즘을 증명할 원문 헤더는 반환되지 않으므로 **어떤
   mechanism(scheme/realm/쿠키명)도 이름 붙이지 않았다**.
4. **비교 결과를 취약점으로 승격하지 않음**: `http_compare_anonymous` mapper는
   도구 자신의 `verdict="observation_only"`를 그대로 저장하고
   `violation`·`vulnerable` 같은 키를 만들지 않는다(테스트로 고정).
5. **권한 주장은 신원이 있을 때만**: 익명 403은 후속을 만들지 않는다.
   자격증명 부재로 완전히 설명되는 상태에서 permission 결론을 읽지 않는다.
6. **승격 게이트**: evidence 0건, hypothesis 미확정, hypothesis 없는 VIOLATION은
   Finding이 되지 못하며 거부 사유가 이벤트로 남는다.
7. **보안 헤더 부재는 관찰일 뿐**: 어떤 후보·Finding도 여기서 파생되지 않는다.

## 8. 변경 파일 목록

소스(13):

| 파일 | 변경 |
|---|---|
| `attack_graph/observation.py` | `PLANNING_CONTENT_KEYS`, `content_digests` 추가 |
| `orchestration/progress.py` | fingerprint에 content digest 포함 |
| `orchestration/agents/base.py` | `unresolved_placeholder()`, `available_steps(drops=)` |
| `orchestration/agents/providers.py` | context 확장, `DraftDrop`, `planning_fingerprint()`, reporting provider 등록 |
| `orchestration/agents/web.py` | 그래프 기반 multi-step provider 전면 확장 |
| `orchestration/candidates/compiler.py` | rejection reason 정본화 (인계 수정 위에) |
| `orchestration/findings.py` | **신규** — evidence-gated 승격 seam |
| `orchestration/nodes/plan.py` | 실제 catalog 기반 context, drop 발행 |
| `orchestration/nodes/verify.py` | promoter 사용, `HypothesisResolved`, open 정책 |
| `orchestration/nodes/replan.py` | `ReplanTriggered` (결정 시에만) |
| `orchestration/authentication.py` | 관측 레코드에 `redirect`·`body_sha256` |
| `orchestration/observations/mapper.py` | `is_http_status()` + compare mapper 등록 |
| `orchestration/observations/web_http.py` | `map_web_compare_anonymous` 추가·중복 검증 제거 |

문서(3): `docs/agent/orchestration-implementation-protocol.md`(신규),
`docs/agent/orchestration-implementation-result.md`(본 문서),
`docs/agent/event-bus.md`(신규 이벤트·reason 어휘 반영).

테스트(8): `tests/unit/test_orchestration_web_followups.py`(신규),
`tests/unit/test_orchestration_findings.py`(신규),
`tests/unit/test_orchestration_dynamic_candidates.py`(신규),
`tests/unit/test_orchestration_nodes.py`,
`tests/unit/test_orchestration_candidates.py`,
`tests/integration/test_event_identity.py`,
`tests/integration/test_end_to_end.py`,
`tests/unit/test_orchestration_web_followups.py`.

## 9. Core / Control / Capability 변경 여부

변경 없음. `src/redteam/core`, `src/redteam/control`,
`src/redteam/capability`는 읽기만 했다. `ActionProposal` 계약,
PM action naming(`capability` + canonical `operation` = intent,
`ToolId` = 실행 수단)도 그대로다.
`attack_graph/observation.py`는 "Owned by Agent Team"으로 명시된 Agent 소유
모델이라 수정 범위에 포함되며, 기존 필드는 전부 하위 호환(기본값)으로 유지했다.
