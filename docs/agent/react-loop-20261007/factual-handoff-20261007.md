# 작업 사실 핸드오프 — ReAct 루프 전환

작성일: 2026-10-07. 이 문서는 구현된 변경, 실제 확인 결과, 중단 시점의 미완성 상태를 기록한다. 작업 지시문이나 사용자 요구 원문은 포함하지 않는다.

## 1. 작업본 상태

- 저장소: `C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent`
- 브랜치: `dev-orchestration`
- HEAD: `6a2cba4cb219f0101c8e4f4f4ef8a28a8ed2dc58`
- HEAD 제목: `refactor(orchestration): simplify validation contracts and review context`
- 이번 ReAct 전환과 후속 수정은 미커밋 상태다. 이번 세션에서 커밋·푸시·dev 통합을 수행하지 않았다.
- 마지막 Git 확인에서 원격 fetch는 수행하지 않았다. 원격과의 현재 동기화 여부는 새로 확인되지 않았다.
- 작업은 일시정지 상태다. 당시 실행 중이던 targetless 구현 담당 4개를 interrupt했다. discovery 담당과 기본 루프 회귀 담당은 이미 결과를 반환했다.
- `.codex-remote-attachments/`, `reports/`, `research_notes/`, 기존 문서와 여러 scratch 파일이 미추적 상태다. 출처별 정리·삭제는 수행하지 않았다.

**현재 작업본은 완성된 실행 가능 스냅샷으로 검증되지 않았다.** 마지막 targetless 결과 처리 편집이 부분 반영된 채 중단됐다. 아래 통과 수치는 해당 검사를 실행한 시점의 소스에 대한 결과이며, 중단 당시 최신 전체 작업본의 통과 결과가 아니다.

## 2. 기본 ReAct 전환에 반영된 변경

### 그래프·모델 호출

- `orchestration/nodes/react.py`에 native 모델 호출·도구 배치·정산 처리가 들어 있다.
- 기본 그래프 왕복은 `model → tools → settle → model/종료`다.
- 기존 policy/execute/verify/update 담당 기능은 도구 처리에서 재사용한다. 이전 승인 체크포인트의 복구 경계도 남아 있다.
- 일반 native 도구 호출은 PathPlan/TurnDecision을 먼저 제출하는 경로를 사용하지 않는다.
- LLM 공개 포트에 NativeToolSpec/NativeToolCall/NativeCompletion 및 complete_native가 추가됐다. LiteLLM 어댑터는 Chat Completions와 Responses의 실제 도구 호출을 정규화하고, Fake 어댑터와 관련 테스트도 이관됐다.
- 코드가 PlanStep/WorkItem에 call ID, 현재 작업, 목적 해시, native 실행 출처 버전을 연결한다. native 실행에 가짜 PathPlan을 만들지 않는다.
- 메시지 이력은 user→assistant→tool 라운드로 보존하고 user 경계에서 자른다. 과거 컨텍스트 스냅샷은 짧은 자리표시자로 압축하며 최신 스냅샷만 온전히 전달한다.
- 텍스트만 반환한 응답은 자동 목표 달성으로 처리하지 않는다.

### 실행·출처·정산

- 기존 Control/Gateway/작업 원장/원본 증거 저장을 사용하는 native 실행 출처 분기가 들어 있다.
- 완료 출처는 저장 work/task/purpose/call과 proposal/action/invocation/tool의 실제 연결을 확인한다. 단순 native metadata marker만 인정하지 않는다.
- 종료 재증명의 저장소 연결을 맞춰 완료 미션이 최종 처리에서 BLOCKED로 뒤집히던 문제를 수정했다.
- evidence.read/task.settle/goal.settle이 등록형 runtime local 도구로 제공된다.
- 의미 정산 담당 `semantic_completion.py`가 실제 상태 commit 뒤 TASK_COMPLETED/MISSION_UPDATED를 발행한다. legacy plan의 별도 중복 발행은 제거됐다.
- LocalToolContext/LocalToolResult의 선택적 last_event_id를 native/legacy 호출자가 전달·소비하여 인과 체인을 연결한다.
- end_to_end에서 과거 삭제됐던 정산 이벤트 단언을 복원했고, event_identity에 payload·인과 확인을 보강했다.

### 진단·대상·주체

- 원본 조회/정산의 입력·운영 예외는 기존 safe diagnostic으로 반환한다. 원문 예외나 입력을 진단으로 복사하지 않는다.
- 명시적 시간 소진과 일반 실패를 구분하며, runtime 진단/이력/영수증에 원인을 보존한다.
- 거절된 호출의 불필요한 원문 인자를 제거한다. 같은 입력이 dispatch에 이미 연결된 경우 NativeCallState의 중복 인자도 제거한다.
- 대상 후보 중복 제거는 graph_node_id를 제외한 구체 실행 필드로 비교한다. 서로 다른 실제 URL 경로가 정규화 graph ID 때문에 합쳐지지 않는다.
- 명시 URL의 정확한 후보와, 이미 검증된 후보 중 명시 path에 맞는 유일한 EndpointTarget을 선택한다. 모호한 경우 typed 진단을 반환한다.
- 모델이 선택한 session_ref를 기존 등록 session alias 또는 유일한 registered owner에 연결한다. 기존 principal/session admission 검사는 유지됐다.

## 3. 인증 후속 조사 복원

주요 파일: `authentication.py`, 신규 `authentication_local_tools.py`, `knowledge_validation.py`, `runtime_components.py`, `observations/web_http.py`.

- 선택적 `authentication.assess` 도구가 두 저장 원본을 비교한다. 입력은 baseline/session evidence ID, session_ref, 선택적 expected_access다.
- 양쪽 상태는 저장 proposal과 raw ToolResult에서 재구성한다. 파생 http_observation 요약을 상태 코드의 최종 권위로 삼지 않는다.
- 같은 run/engagement의 실제 TOOL 원본과 proposal/action/invocation/tool 연결을 확인한다.
- 결과는 인증 관측과 원본 refs다. AUTHENTICATION_CHECKED를 발행하고 authentication_checks를 저장하지만, 도구 자체가 최종 finding을 만들지는 않는다.
- 최종 취약점 주장은 graph.record의 기존 원본 검토·승격 경로를 사용한다.
- 최초 실제 통합에서는 익명 요청의 host 원본과 세션 요청의 endpoint 원본 사이 source-backed 관계가 없어 finding이 거절됐다.
- HTTP 요청 mapper가 실제 HostTarget 요청에서 관측한 endpoint에 HOSTS 관계와 relation snapshot을 생성하도록 연결했다. 기존 update의 measured evidence provenance와 claim_target_is_grounded가 이를 소비한다.
- 중간 구현의 endpoint→endpoint HOSTS는 인접 검사에서 발견돼 제거됐고, 최종 조건은 HostTarget에 한정됐다.
- 인증 테스트의 리소스 주장은 session 원본의 endpoint에 연결하며 target fallback을 제거했다. 두 원본/high/verified/confirmed hypothesis 단언은 유지됐다.
- session_status라는 키가 secret-key 필터로 삭제되던 문제는 local 공개 결과에서 response_status_code로 명명해 해결했다. 기존 인증 owner/legacy 계약과 전역 마스킹 규칙은 바꾸지 않았다.

Opus 4.8 High의 인증 코드·HTTP 관계·공개 상태코드 좁은 독립 리뷰에 필수 문제가 남지 않았고, DeepSeek 4.1 Max도 인증 원본 비교 경계를 검토했다. 이는 전체 변경이나 라이브 검증의 최종 승인이 아니다.

## 4. 직접 결과 전달과 승인 배치 복구

- 이전 native external feedback은 ok/executed/evidence_ids/error만 반환했다. public evidence context에도 output이 없어 실제 값을 기록하려면 별도 evidence.read가 사실상 필요했다.
- 현재 `_result_messages`는 다음 모델 요청 직전에 마지막 연속 tool 라운드의 저장 원본을 EvidenceReader로 읽어 `original_results`에 일시적으로 붙인다.
- 레코드당 최대 4,000자, 해당 라운드 전체 텍스트 최대 8,000자다. 출처와 truncated/status를 함께 전달한다.
- 조회 non-ok 상태도 누락하지 않는다. not_found/no_raw/run_mismatch와 실제 빈 출력을 구분한다.
- 전달 본문은 요청 지역 변수에만 있으며 persistent native_messages는 참조 중심으로 유지된다. runtime.evidence_reads에 모델이 조회한 것으로 오기록하지 않는다.
- 완료된 형제 호출은 call ID/결과 refs를 남기고 arguments/dispatch를 제거한다. 아직 미실행 또는 승인 대기인 호출은 원래 봉투를 유지한다.
- 성공 local sibling+승인 pending external sibling의 실제 runner checkpoint, serializer round-trip, 승인·재개 검사에서 local은 중복 실행되지 않았고 external은 원래 compiled 입력/action으로 한 번 실행됐다.
- provider-facing 다음 턴에 실제 raw marker/provenance가 전달되고, persistent 메시지에는 marker가 없으며 별도 evidence.read 요청이 없음을 자동 검사로 확인했다.
- 원본 부재 상태 보존 및 원래 메시지 비변경은 별도 작은 실행 probe로 확인했다.

## 5. 대상 없는 실행 지원 — 부분 구현 상태

확인된 문제: query-only web_search는 공개 입력에 대상 주소가 없는데 native resolver가 material의 operator URL와 scope host 중 정확히 하나를 선택하도록 요구하여 두 통합 사례가 ambiguous_tool_target으로 거절됐다. active task의 related target은 빈 배열이었다.

기존 `target_types=[]`는 모든 대상 종류 허용이고, `binds_target_arguments=False`는 대상 인자를 자동 주입하지 않는다는 뜻이다. 두 플래그를 scope 생략 권한으로 해석하는 제안은 독립 Opus 검토에서 부적절하다고 판정됐다.

### 실제 반영된 코드

- ToolDescriptor.requires_target: bool, 기본값 True.
- `redteam.requires_target` 공통 discovery metadata 상수와 export.
- ActionProposal.target은 nullable이며 legacy_action의 None은 validator가 거절한다.
- 검색 discovery 담당 hook이 requires_target=False를 설정하고, 검색 manifest는 이를 명시적으로 확인한다.
- MCP metadata parser는 bool 값만 반영하고 미지정/잘못된 자료형은 coercion하지 않는다.
- native resolver는 requires_target=False 도구에 None을 반환한다.
- react는 None 대상의 candidate target refs/conflict refs를 비워 두고, 대상별 concurrency key를 만들지 않는다.

### 중단 당시 반영되지 않았거나 미완성이던 부분

마지막 파일 조회에서 다음 상태를 확인했다.

- `control/service.py`, `core/scope_rules.py`, candidate compiler/models에는 targetless용 새 diff가 없었다.
- PlanStep의 정확히 하나인 target/target_bind 규칙과 WorkItem의 비어 있지 않은 target refs 규칙은 그대로였다. 해당 파일의 native 계보 필드 추가는 이전 기본 ReAct 변경이다.
- engine의 targetless admission 변경도 마지막 조회에서 확인되지 않았다.
- `nodes/verify.py`에는 target_node_id를 None일 때 생략하고 mapping을 not_applicable로 처리하려는 부분 패치가 있었다.
- **verify.py 137행에는 `outcome = deps.result_mappers.map_result_with_outcome(`가 열린 채 남고 바로 138행의 `if proposal.target is None:`이 이어진다. 편집 중단으로 남은 불완전한 구문이며 현재 작업본의 실행 가능성을 보장할 수 없다.** 이 문서 작성 중 이를 수정하거나 새 컴파일/테스트를 실행하지 않았다.
- recovery/analysis/reporting/completion의 targetless 소비 통합은 완료 보고·실행 근거가 없다.
- 마지막 구현 담당은 Control, compiler/PlanStep/work, engine admission, raw/result 소비로 나뉘었고 4개 모두 interrupt됐다. discovery 담당만 구현·관련 확인을 마쳤다.

검색의 실제 native 실행·원본 저장·정산까지 통과한 결과는 아직 없다. 이 부분은 계약 선언과 일부 소비만 반영된 미완성 변경이다.

## 6. 확인된 자동 검사 결과

아래 묶음은 겹치므로 합산할 수 없다. 현재 중단 작업본 전체에 대한 결과도 아니다.

| 검사 범위 | 결과 | 확인 주체/범위 |
|---|---:|---|
| 초기 native/로컬 도구/origin/LLM 관련 4파일 | 51 passed | 재개 시 루트 실행 |
| native/local/principal/end_to_end/event_identity | 35 passed | 당시 루트 실행 |
| origin/LLM adapter/agent_loop | 54 passed | 루트 실행 |
| durable_resume | 14 passed | 루트 실행, SQLite 재개 포함 |
| recon handoff/header/access/second principal 4파일 | 26 passed | 루트 실행 |
| 인증 mapper/세션/인증 local/인증 통합/access/second principal 6파일 | 68 passed, 16.55초 | HTTP relation 수정 시점 루트 실행 |
| native/local/principal/durable/end_to_end/event_identity 6파일 | 49 passed, 25.71초 | 루트 실행 |
| 인증 local+인증 followup | 19 passed, 9.41초 | response_status_code 수정 뒤 루트 실행 |
| native/durable/approval/fanout 4파일 | 36 passed, 33.19초 | 완료 sibling 폐기 수정 뒤 루트 실행 |
| native loop+native local 2파일 | 24 passed, 10.50초 | inline 결과 전달·실제 승인 배치 회귀 포함 루트 실행 |
| requires_target metadata | 26 passed | discovery 담당 실행 보고 |
| availability metadata+web_search unit | 70 passed | discovery 담당 실행 보고 |
| web_search MCP integration | 6 passed | discovery 담당, in-process 대역 provider |
| agent_mcp_contract_binding+public_outbound_target_action | 29 passed | discovery 담당 실행 보고 |

- native loop 파일 단독7 passed 담당 보고와 위24개 묶음은 중복된다.
- CLI/fanout/approval 묶음은 당시22 passed/1 failed(NameError)였고, `_native_world` 이관 누락을 고친 뒤 해당 JSON CLI 단독1 passed를 확인했다. 이를 전체 묶음 통과로 합쳐 표현하지 않는다.
- Ruff는 각 완료 델타의 관련 파일에서 통과했다. 마지막 부분 편집을 포함한 전체 작업본 Ruff 결과는 없다.
- 중단 직전 root의 metadata+web_search MCP 검사(session 53894)는 최종 결과를 수령하지 못했다. 이후 handle은 존재하지 않았으며, 그 실행을 통과로 기록하지 않는다.

## 7. 실패·환경 관측

- 이전 Windows 전체 실행 기록: 2704 passed/201 failed/66 skipped/41 errors. 중간 소스의 결과다. 깨끗한 HEAD 전체 대조 실행은 최종 숫자가 없어 모든 실패를 기존 환경 문제로 확정할 수 없다.
- 과거13파일 migration 묶음:43 passed/43 failed/1 skipped. 이후 여러 파일의 이관·제품 결함 수정이 진행돼 현재 결과와 다르다.
- 최근 methodology/common/recon focused:15 failed/11 passed/1 skipped.
- methodology의 dirty checkout은 Windows CRLF working file와 Git autocrlf로 LF 정규화된 index blob의 차이를 byte probe로 확인했다. fixture를 LF bytes로 쓰도록 수정했다.
- 그 뒤 methodology 단독13 failed/1 skipped는 모두 setup에서 Windows에 없는 os.O_DIRECTORY에 막혔다. skip/xfail을 새로 추가하지 않았다.
- common action9개는 당시 통과했고 search2개가 실패했다. search는 현재 targetless 부분 구현 상태라 수정 후 통과 근거가 없다.
- approved_mcp_deployment/principal_mcp_transport_e2e의 MCP subprocess Connection closed는 아직 원인 확정·새 실행 확인이 없다.
- 이번 재개에서 실제 모델/MCP 라이브 실행을 수행하지 않았다. 새 최종 전체 Python3.10/3.14 suite와 전체 mypy 결과도 없다.

## 8. 입력 보존의 남은 한계

- checkpoint serializer는 타입 allowlist를 확인하지만 실행 입력의 값 기반 redaction은 하지 않는다.
- 기존 legacy 체크포인트도 원래 실행 인자를 보존했다. native가 해당 저장 성질 자체를 새로 만든 것은 아니다.
- 승인 대기 external dispatch와 top-level proposal/step, 미실행 local arguments에는 원래 입력이 남는다.
- 원장은 인자 저장소가 아니며 audit은 redacted라서, 현재 기존 저장소만으로 원래 입력을 다른 위치에서 복원하는 경로가 없다.
- 원래 입력을 임의 마스킹하면 승인 fingerprint 및 재개 실행 내용이 달라진다. 그 처리는 적용하지 않았다.
- 완료/거절 호출의 불필요한 입력은 제거했으나, 미실행 입력 저장과 이전 historical snapshots는 해결되지 않았다. 개인정보·비밀 입력 저장 전체 해결로 볼 수 없다.

## 9. 자료 위치와 이력의 정정

- 현재 변경의 정본: 작업본 소스와 `git diff HEAD`. 신규 untracked 소스는 diff 통계에 나타나지 않을 수 있다.
- 진행 이력: `docs/agent/react-loop-20261007/work-log.md`
- 독립 검토 이력: `docs/agent/react-loop-20261007/review.md`
- 구현 설계 기록: `docs/agent/react-loop-20261007/implementation-plan.md`
- 팀 PRD 접점 기록: `docs/agent/react-loop-20261007/prd-coordination-points.md`
- 실행 분류: `data/react-loop-20261007/remaining-focused-current.txt`, `remaining-focused-classification.txt`, `methodology-lf-current.txt`, `migration-state-check.txt`
- 측정: `data/react-loop-20261007/measure-after.txt`, `measure_result.json`
- 환경 조사: `data/react-loop-20261007/environment-notes.md`는 조사 당시 상태다. Docker·인증 사용 가능성의 현재 재확인 자료는 아니다.
- data는 로컬 무시 자료라 Git만으로 전달되지 않는다.

이전 핸드오프의 “end_to_end가 통과하므로 정산 이벤트가 일부 발행된다”는 설명은 당시 이벤트 단언이 삭제돼 있어 잘못된 추정이었다. 이후 실제 publisher 연결과 단언 복원으로 별도로 확인했다. 이전 문서의 인증 도구 구현 중/미커밋 등의 과거 표현은 이 문서의 상태와 다를 수 있다. scratch 삭제 완료라는 과거 표현도 현재 미추적 목록과 일치하지 않는다.

컨텍스트 압축 측정은 기존 오프라인 스크립트에서 최대 요청863,878→70,298바이트 및890,167→72,354바이트를 보고했다. 이후 inline 원본 전달을 추가한 최신 소스에서 재측정한 값은 아니며, 실제 모델 성능 개선율이나 다른 에이전트 대비 우월성의 근거가 아니다.

## 10. 전체 판정

기본 native ReAct 루프, 출처·정산 이벤트, 인증 비교·취약점 소비, 직접 결과 전달, 승인 배치 복구는 관련 자동 검사와 좁은 독립 소스 리뷰 근거가 있다. 대상 없는 실행의 전체 소비 통합, 최신 전체 작업본 검사, 실제 모델/MCP 종단 실행은 완료되지 않았다. 현재 작업본에는 중단된 부분 패치가 있으며 최종 완료 판정은 없다.
