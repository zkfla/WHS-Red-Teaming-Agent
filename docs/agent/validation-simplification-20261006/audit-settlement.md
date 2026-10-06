# 정산·영수증 계약 감사 (settlement audit)

기준: HEAD `2f64f636`, 읽기 전용 조사. 수정·테스트 실행 없음.
아래 인벤토리와 미확정/리뷰중 표시는 최초 조사 당시 상태다. 이후 구현·독립 승인·최신 실행 결과는 plan.md, review.md, final-validation.md를 기준으로 읽는다. 작업 트리 변경 없음이라는 아래 문장도 당시 조사 시점에만 적용된다.
상위 판단: `docs/agent/validation-contract-20261005/review-proposal-synthesis-20261006.md` 8장.
목표: 검증 부담을 중간에 가까운 중상으로 완화하되, 원본·권한·관측/추론 구분·근거 없는 확정 차단은 보존.

## 0. 줄수 정정 (측정 아티팩트 주의)

- `src/redteam/orchestration/completion.py` 정본은 **2050줄**이다.
  `Measure-Object -Line` 파이프로 재면 1957이 나오지만, `ReadAllLines().Length`와
  `git show HEAD:...` blob 카운트가 2050으로 세 번 일치하고 CRLF도 2050개다.
  1957은 기준으로 쓰지 않는다.
- `src/redteam/orchestration/semantic_completion.py` 1062줄,
  `src/redteam/orchestration/engine.py` 3178줄,
  `src/redteam/orchestration/replanning.py` 1091줄 (이상 raw 기준).
- 작업트리 tracked 변경 없음. 아래 줄번호는 작업트리=HEAD 번호 그대로다.

## 1. 인벤토리 (파일별 조건 위치)

| 파일 | 확인한 조건과 위치 |
|---|---|
| `src/redteam/orchestration/semantic_completion.py` | `verify_task_receipt` 146행대: revision 변경 무효화 아님, purpose_hash·대상/가설 변경·origin 없음은 무효화. `verify_single_origin` 190행: 원본 1건 증명. `verify_receipt_bound_origins` 241행: 영수증 자체 검사 + 인용 origin 전건 재증명 + `check_result_review`. `select_task_evidence_ids` 321행: 명시 evidence 우선, step evidence 폴백. `_verify_task_origin` 353행: 작업 완료 제안 시점 검사. `_pending_execution_errors` 463행. `complete_task` 525행. `settle_unresolved` 665행: `_ask` 호출 없음. `verify_legacy_task_origins` 734행: 영수증 없는 완료의 읽기 전용 path 증명. `_goal_task_coverage` 839행. `complete_goal` 912행. |
| `src/redteam/orchestration/completion.py` | `check_record_linkage` 430행: action/invocation/tool/proposal 연결, 첫 실패 중단. `verify_stored_origin` 585행: path/version/step 저장 존재 + mission 바인딩. `verify_origin_step` 638행: step 모드 원본 인정. `validate_completion` 1116행: step 코드 검사. `validate_result_claims` 1253~2041행: 영수증 해소~원본·계정·기준 평가~outcome 정합성. `_NO_CANDIDATE_PHRASES` 33행대. `__all__` 2044~2050행. |
| `src/redteam/orchestration/engine.py` | `_ask` 532행, `_propose` 621행대(`permits()` 거짓이면 `_budget_failure`), `record_completion_review` 723행(SYSTEM 영수증 append, 모델 호출 없음), `_review_token` 1316행, `_check_review_path` 1330행, `_commit_review` 1335행, dispatch 루프 `while budget.permits()` 1692행, `complete_step` precheck 1904행·본체 2580행대, `complete_goal` precheck 1925행·위임 2723행, `complete_task` precheck 1953행·위임 2728행. |
| `src/redteam/orchestration/result_claims.py` | `check_result_review`: claims 비어 있으면 거절, claim별 evidence 집합·target 범위 확인 후 `validate_result_claims` 위임. `mission_result_purpose` 제공. |
| `src/redteam/orchestration/completion_proposal.py` | `CompletionProposal.unresolved` = inconclusive/blocked. `validate_unresolved`: reason 필수 + 인용 evidence 범위 확인, 성공 증명 요구 없음. `record_unresolved`: `claim_supported=False` SYSTEM 노트, 완료 영수증과 분리. |
| `src/redteam/core/contracts/mission.py` | `MissionTask`: BLOCKED는 `blocking_reason` 필수, `completion_evidence_id`는 원자 완료와 함께 커밋. `MissionCriterionResult.purpose_hash`: 목표 의미 바인딩, legacy는 None 허용. |
| `src/redteam/orchestration/replanning.py` | `_TerminalCriterion` 382행, `_TerminalReview` 392행, `_terminal_task_coverage` 411행(목표 리뷰와 같은 공유 증명 재사용 명시), `_terminal_criterion_results` 483행(저장 `criterion_results`가 criteria 집합·`met`·`purpose_hash`·인용 합집합·`check_result_review`를 모두 통과하면 새 리뷰 없이 재사용). |
| `src/redteam/orchestration/task_graph/rules.py` | `TRANSITIONS`: COMPLETED/FAILED/CANCELLED 종단. 의존성 사이클·타 mission 의존 금지. |
| `src/redteam/orchestration/coordination.py` | `change` 콜백이 유일한 원자 경계, 외부 I/O는 경계 밖. 영수증 append + task 전이는 같은 커밋. |
| `src/redteam/orchestration/checkpointing.py` + `app/cli/commands/resume.py` | resume은 체크포인트 원본 proposal·승인 연결 복원, 재계획·재구성 금지, 실패 시 run 그대로. |
| `src/redteam/app/report_store.py` + `orchestration/reporting.py` | 터미널 보고 1파일 저장, 미도달 run은 unknown. `investigation_unresolved`만 미해결 상세로 투영, 실패 실행과 동시 표시. |
| `src/redteam/orchestration/cognition.py` | `DecisionBudget.permits` 172행: 남은 예산 0 또는 정상 한도 소진이면 거짓. 모델 호출 여부와 무관한 카운터. |
| `docs/contracts/evidence.md` | append-only·digest·교체 규격까지만 정의. `task_completion_receipt`·`investigation_unresolved`·`completion_review` record_kind는 코드 소유. 문서 신설은 이번 범위 밖. |

## 2. 조건별 판정 (근거·소비·발생 상황)

| 조건 | 판정 | 실제 근거 | 소비 경로 | 발생 상황 |
|---|---|---|---|---|
| run·engagement 바인딩 (원본·영수증·미해결 인용 전부) | 유지 | cross-run 증거 차용 차단. `verify_single_origin`·`_verify_task_origin`·`validate_result_claims`·영수증 검사·`validate_unresolved`에 분산 존재하나 검사 대상이 각기 다름 | task·goal·terminal·step 전 경로 | 다른 run·engagement·mission 증거 인용 시 거절 |
| 원본 raw 보존 + action/invocation/tool/proposal 연결 | 유지 | 식별자만으로 증명 불가. `check_record_linkage` 첫 실패 중단 | task 제안·goal 커버리지·terminal·step | raw 미보존·연결 불일치 시 미증명으로 처리 |
| path/version/step 저장원본 존재 | 유지 | lineage 신선도는 호출자 책임, 존재·바인딩만 확인 (`verify_stored_origin`) | 동상 | 인용 path 미저장·타 mission 소속 시 거절 |
| task target·principal 적합성 | 유지 | 작업 위임 범위를 벗어난 증거 차용 차단 (`_verify_task_origin` target·principal 분기) | `complete_task` | 범위 밖 대상·계정 증거로 완료 제안 시 거절 |
| claim별 계정·가설·finding·최종영수증 증명 (poc·hypothesis) | 유지 | 근거 없는 취약점 확정 차단의 핵심층 | `check_result_review`→`validate_result_claims`, goal 기준별·terminal 기준별 | poc 최종영수증·verified finding 없으면 거절 |
| purpose_hash·대상/가설 목록 바인딩, revision 미무효화 | 유지 | 목적 변경은 무효화, bare revision은 무효화 아님 (이미 구현됨, 문서화만) | 영수증 검증·goal/terminal 재사용 | 목적 변경 후 구 영수증 재사용 시 거절 |
| legacy path 증명 폴백 (영수증 없는 완료) | 유지 | 읽기 전용, 새 영수증 발행 없음. 활성 path·목적 문자열 일치·필수 step `validate_completion` 재수행 | goal·terminal 커버리지 | 구 완료 task 조회 시 |
| goal 커버리지 내 2회차 `verify_single_origin` 루프 | 통합 후보 (좁은 후보 1) | `verify_receipt_bound_origins` 내부 증명과 같은 저장 조회 반복. 판정 자체는 유지하고 조회만 재사용하는 조건으로만 추진 | `_goal_task_coverage` | 필수 task마다 발생, task 수 비례 비용 |
| task 제안→claims 재검증 겹침 | 조회 재사용 후보 (좁은 후보 2) | `_verify_task_origin` 통과 기록을 `validate_result_claims`의 동일 존재·linkage 검사에 전달. target/principal·claim 증명은 유지 | `complete_task` | 완료 제안마다 발생 |
| 저장 `criterion_results` 재사용 (terminal 구현됨) | 유지, goal 확대는 미확정 | 동일 criteria 집합·`met`·`purpose_hash`·인용 합집합·`check_result_review` 통과 시에만 재사용 | terminal (구현됨), goal (검토중 아님) | 터미널 정산 시 |
| 후보없음 문구 조건 | 역사 조사 시점 유지 → 현재 계획 R4 삭제로 override | 감사 시점에는 성공 제안 + 유효 증거 없음 경우로 제한된다고 봤다. 이후 상위 계획이 R4 삭제를 확정했다. 상세는 `plan.md` R4절과 `review.md` R4 승인행을 따른다. 증거 없는 성공 금지는 기존 원본·기준 검사가 그대로 막는다 | `validate_result_claims` 2017행대 | 증거 0 성공 제안 시 |
| step·task·goal 3중 모델 리뷰 | 유지, 단순 삭제 기각 | purpose 상이: step=고정기준+증거, task=위임목적+원본, goal/terminal=원본 criteria+필수 커버리지. 삭제는 근거 부족 | 각 `complete_*` | 각 정산 제안 시 |
| B1 의미기반 중복차단·B5 새 지문·일괄 strict·동적 행동표·고정 턴 예산 | 보류·제외 | 8장 결론. 실제 계약 결함 재현 항목만 별도 판단 | — | 이번 감사 후보 아님 |

## 3. 중복 검증 정적 호출 구조 (실행 없음, 기계적 중복 vs 보존 필요)

- 작업 경로 2회: `complete_task`의 `_verify_task_origin` 1회 + `check_result_review`→`validate_result_claims`의 동일 evidence 존재·run·path linkage·principal 검사 1회.
- 목표 경로 최대 3회: `_goal_task_coverage` 내 `verify_receipt_bound_origins`(내부 `verify_single_origin` 포함) + 같은 루프의 2회차 `verify_single_origin` + 기준별 `check_result_review` 3회차. terminal도 같은 공유 증명 재호출.
- 중복 성격: 존재·run 바인딩·path linkage 저장 조회 반복 (성능·호출 비용).
- 보존 성격: task target/principal 적합성, claim별 계정·판정·finding 증명, 영수증-원본 연결과 리뷰-주장 연결은 책임이 다르다. 한쪽 삭제는 근거 없는 확정 차단층을 걷어낸다.
- 결론: 판정층은 유지하고 저장 조회 반복만 줄이는 좁은 후보 1·2만 추진 검토. 동일 판정 유지 + 조회 감소가 증명 안 되면 유지로 종결.

## 4. observation 종류 확인 (이번에 원본 확정)

- `completion.py`에서 `observation`은 기준 종류별 특별 분기가 없다.
  `kind == "observation"` 분기 없음. `observation` 언급은 `http_observation` 메타 판독(233·954행)과 refuted 메시지 문구(1661행대)뿐이다.
- 일반 pool 경로: `valid_records` 수집 후 기준별 `min_evidence` 개수 검사(1810행대)를 통과하면, semantic처럼 코드가 admitting하는 별도 관문이 없고 tail의 outcome 정합성(1991행대)·후보없음 금지(2017행대)·proposal 주장 대조(2025행대)만 거친 뒤 `issues.deduplicated()`로 반환된다.
- 즉 observation은 일반 pool 성공 + 개수 조건으로 코드 admit된 뒤 의미 판단은 호출자 리뷰에 위임된다. 별도 observation 검증층 신설은 불필요하며, 신설 제안이 나오면 기각 근거로 쓴다.

## 5. R2·R3 계획 상태 (미확정, 리뷰중)

- R3 확정 (계획 리뷰중): dispatch `complete_*`의 `budget.permits()` precheck(`engine.py` 1904·1925·1953행)가 미해결 정산까지 막는다.
  `settle_unresolved`·`complete_step` 미해결 분기는 `_ask`·`_propose` 호출이 zero라 모델 검수가 필요 없는데도, 마지막 예산을 쓴 뒤의 미해결 제안이 `continue_planning`으로 돌려보내지고 task는 RUNNING으로 남는다.
  최소 완화안: 세 분기에서 completion proposal을 먼저 파싱해 unresolved면 precheck를 우회하고 `complete_*`로 진입. 성공 경로는 기존 검사 그대로. 새 예산·지문·캐시 도입 없음. 코드 경로상 확정, 자동 재현은 미작성.
- R2 계획 (계획 리뷰중): goal 판정 시 raw ID·task ownership 맵을 모델에 명시한다.
  발췌 8cap 안에서는 검증된 모든 원본 ID를 표시하고, requested window 관련이면 해당 원본을 보존한다. cap은 리뷰 페이로드만 자르고 커버리지 검증을 자르지 않는原则 유지.
  새 저장소·지문 도입 없이 기존 영수증·`criterion_results`·원본 조회 재사용 범위 안에서만 다룬다.
- R2 구현 메모: task·goal 리뷰 전송은 `_task_review_payload`·`_goal_review_payload`로 조립한다
  (`semantic_completion.py`, subject + 소유맵 + 발췌 + 전체 원본 ID + `review_note`).
  앞으로 모델 요청 이벤트의 path 참조는 전송 안의 `active_path` reference를 쓴다.
  `engine._ask`가 refs(`path_id`·`version`)를 읽는 위치가 이 reference이므로,
  전체 path 덤프 제거 후에도 이벤트 refs 유지 조건이 깨지지 않는다.

## 6. 미확인 잔여

- 모델 호출 횟수·비용 실측(A18 수치화)은 실행 관측 없이 단정하지 않는다. `engine._ask` 단일 진입·`permits()` 게이트 구조까지만 확정.
- R2·R3 계획 리뷰 결과는 본 감사 이후 확정분만 반영한다.
- `graph_recording.py` 사실 기록 경로·`diagnostics.py` JSON 오류 분류는 이번 분담(정산·영수증·저장/보고/재개) 밖이라 대조하지 않았다.
- 피드백 채택 항목(A1~A18·B1~B12)은 전부 구현 대상이 아니다. 위 좁은 후보 1·2와 R2·R3만 계획 검토로 넘긴다.

## 7. 제약 준수

- 본 감사는 읽기 전용. 파일 수정·테스트 실행·서브에이전트 생성 없음.
- dev 통합 금지. 본 문서는 `dev-orchestration` 흐름용이며 현재 브랜치 외 푸시 판단에 쓰지 않는다.
- R2 대역 정렬 기록: `test_engine_live_run_flow.py` 목표 대역이 옛 `payload["path"]["steps"]`를 읽어
  R2 전송에서 KeyError가 난다. 승인된 공개 필드 `verified_source_ids`(검증된 전체 원본, 발췌 상한 밖 포함)로
  대역만 정렬했고, 실제 실행·목표 달성·저장/보고·원본 근거 단언은 그대로다. 기대치 완화 없음.
  최초에는 `test_cli_smoke.py`·`test_end_to_end.py`의 직접 소비가 없어 무관하다고 보고했으나,
  두 파일도 같은 `LiveScriptedLLM`을 가져오는 실행 환경에 의존한다. 독립 리뷰에서 이 간접 소비를 확인했고
  루트의 최신 CLI/E2E 포함 94개 관련 검사도 통과했다. 따라서 rc2의 다섯 실패는 이 공유 대역 입력 불일치로 분류한다.
  대역 실행은 루트 정본으로 넘긴다 (자식 환경 통합 pytest hung).
