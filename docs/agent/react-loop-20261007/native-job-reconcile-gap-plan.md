# native 루프 job 회수 공백 — 최소 계획 (구현 전)

작성일: 2026-10-07. 담당: /root/coordination. 상태: 조사·계획만. 코드 수정 없음. root 승인 후 소유 모듈 확장.

## 1. 확인한 공백 (파일:행 근거)

1. native 루프가 모델에 노출하는 도구는 원격 도구 + `deps.local_tools.descriptors()`뿐이다(`src/redteam/orchestration/nodes/react.py` `model_node`). 등록된 runtime local 도구는 `evidence.read`, `task.settle`, `goal.settle` 3개다(`src/redteam/orchestration/native_local_tools.py`의 `native_local_tool_contributions`).
2. `InvestigationEngine.reconcile_job`(`src/redteam/orchestration/engine.py:2565-2652`)의 유일한 호출자는 `engine.py:1943-1944`이며, 조건은 `decision.mode == "reconcile_job"`인 legacy `TurnDecision` 경로다. `TurnDecision.mode` Literal에 `"reconcile_job"`이 포함된다(`engine.py:143-156`).
3. native 경로는 `TurnDecision`을 만들지 않는다. `react.py`는 `engine.client.ask_native`와 도구 호출을 쓴다. 따라서 native에서는 회수 트리거 자체가 없다.
4. long job 제출·폴링은 `src/redteam/orchestration/nodes/execute.py:173-215`에 있다. 마감 초과 시 `TimeoutError("registered job is still running; collect its result later")`, 상태 `UNKNOWN`이면 `RuntimeError("... do not resubmit")`를 던진다. 제출 직후 `work_store.bind_job`이 `work.job_id`를 보존한다(`execute.py:187-197`).
5. `execute_node`는 그 예외를 실패한 `ToolResult`로 정규화하고 `metadata={"orchestration_exception": True}`를 붙인다(`execute.py:428-465`). 이는 `recovery.is_execution_ambiguous`가 True로 판정하는 형태다(`src/redteam/orchestration/recovery.py:57-68`).
6. `update_node`가 WorkItem을 완료 처리한다(`src/redteam/orchestration/nodes/update.py:503-528`). 성공도 차단도 아니면 `WorkStatus.FAILED`, `reason=error_code`다. `job_id`는 남는다.
7. 원장 계층은 회수를 받을 준비가 되어 있다. `decide_reconcile`(`src/redteam/orchestration/work/rules.py:199-230`)은 `job_id`가 일치하면 `FAILED`/`STALE`에서도 `RECONCILABLE_STATUSES`로 전이를 허용한다.
8. native `tools_node`는 terminal work row를 만나면 저장 상태만 보고 `ok=False`로 복원할 뿐 회수하지 않는다(`react.py`의 `TERMINAL_STATUSES` 분기).

결론: 원장과 엔진 회수 로직은 존재하지만, native 루프가 `engine.reconcile_job`에 도달할 진입점이 없다. 이것이 이번 공백의 정확한 범위다.

## 2. 제안: 최소 등록형 runtime local 도구 하나

새 큐·원장·프레임워크·어댑터를 만들지 않고, 기존 `reconcile_job`을 호출하는 local 도구 하나만 추가한다.

- 위치(소유 모듈): `src/redteam/orchestration/native_local_tools.py`에 네 번째 도구 추가. 중앙 분기 없음.
- 등록: `native_local_tool_contributions()` 튜플에 contribution을 추가한다. `src/redteam/app/runtime_components.py`의 기존 `builtin.native-local-tools` 선언이 그대로 소비하므로 조립 지점 변경이 필요 없다.
- 노출·라우팅: `react.model_node`가 local descriptor를 native tools로 자동 노출하고, `react.tools_node`가 `deps.local_tools.get(tool_id)`로 `_local_call`에 자동 라우팅한다. 그래프·중앙 실행 흐름 변경이 필요 없다.
- 도구 id: `job.reconcile` (evidence/settlement와 같은 runtime-local 네임스페이스).
- 입력 schema: 모델이 런타임 ID를 만들지 않도록 선택형 최소 입력으로 둔다. `{"type": "object", "properties": {"work_id": {"type": "string"}}, "additionalProperties": false}`. `work_id`가 주어지면 현재 run 소유인지 검증하고, 없으면 원장에서 후보를 결정적으로 고른다.
- 후보 선택(새 원장 없음): `deps.work_store.list_items(run_id=runtime.run_id)`에서 `job_id`가 있는 항목. 미지정 시 그 항목들을 순서대로 `engine.reconcile_job`에 넘긴다.
- 위임 본문: `native_local_tools._settle`와 같은 패턴을 재사용한다.
  - `engine = InvestigationEngine(deps)`; `engine._event_runtime = runtime`; `engine.last_event_id = context.last_event_id`.
  - `result = await engine.reconcile_job({"work_id": wid}, runtime, budget=context.budget)`.
  - 반환: `LocalToolResult(ok=..., payload=result, refs=(work_id, job_id, evidence_id), last_event_id=engine.last_event_id)`.
  - `runtime_outcome`은 설정하지 않는다. 회수는 정산이 아니라 수집·상태 읽기이므로 `local_tools` 계약대로 None을 유지한다.
- 예외 매핑:
  - `deps.long_job_adapter is None`이면 `LongJobManager`가 `LongJobNotSupportedError`를 던진다(`src/redteam/orchestration/long_jobs.py:49-51`). handler는 이를 잡아 `ok=False`, `error="job reconciliation is not supported"`로 반환한다. 성공이나 빈 결과로 숨기지 않는다.
  - 기타 저장소 실패는 기존 `operational_exception_diagnostic(stage="native_local_tool", code="job_reconcile_failed")`로 반환한다.
  - 상태가 아직 terminal이 아니면 `engine.reconcile_job`이 `{"status": <state>}`를 반환하므로 그대로 전달해 모델이 다음 판단에 쓴다.
- 예산·인과: reconcile_job은 `verify_node`/`update_node`를 재생하고 reviewed_runtime의 usage를 runtime에 반영한다(`engine.py:2625-2637`). handler는 `_settle`처럼 `context.budget`을 넘기고 runtime 카운터를 직접 더하지 않는다. `react._local_call`의 `before_usage + budget.used` 합산과 중복되는지는 구현 시 확인 항목으로 남긴다.
- 미채택 대안: 새 큐·원장·검수 제약·범용 어댑터·도구 이름별 중앙 분기. 요구에서 금지됐고 기존 구성요소로 해결된다.

## 3. 남은 검증 항목 (구현 시)

- `engine.reconcile_job`의 원본 검색 조건은 `metadata.candidate_key == work.candidate_key`이고 `proposal`/`plan_step`이 함께 있어야 한다(`engine.py:2584-2597`). execute가 남기는 `execution_submission` receipt가 native 경로의 candidate_key와 일치하는지 실제 실행으로 확인해야 한다.
- reconcile_job은 verify_node를 재생하므로 verifier(모델 검증 호출)와 예산이 필요하다. 계정1 라이브 경로와 예산 조건에서 동작을 확인해야 한다.
- 후보가 여러 개일 때 순서와 중복 호출의 멱등성을 확인해야 한다(`decide_reconcile`은 재적용 시 True, None을 반환한다).
- `long_job_adapter=None`인 현재 기본 조립에서는 이 도구가 "실제 백엔드 없음"만 정직하게 보고한다. 실 백엔드 어댑터 도입은 이 계획 범위가 아니다.
