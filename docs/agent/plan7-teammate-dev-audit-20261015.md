# Plan7 원격 dev 감사 (2026-10-15, 2차)

상태: 읽기 전용 감사다. 원격 기준은 `git show origin/dev:...`와 `git grep origin/dev`이며, HEAD는 대조용으로만 썼다. dirty 파일은 근거로 쓰지 않았다. 소스 편집, pull·merge·commit·push는 하지 않았다. 테스트 파일은 "존재"만 확인했고 실행하지 않았다. 원격 Docker 증거는 소스 테스트가 있으나 이 감사에서 실행하지 않았고, 실행 기록을 찾지 못했다.

## 기준점

- origin/dev: 38179561. 로컬 HEAD: 7ce0daaa (감사 시작 시 ef4d557a, 이후 루트가 전진). merge-base: 376b479d.
- Plan7 원격 커밋: f994b561 (Kali attach, 85파일), 38179561 (토큰 절약, 7파일).
- 원격에는 docs/exec-plans/active/plan7-*.md가 없다.

## 교집합 (PowerShell 배열로 계산)

- 원격 변경 파일 112개(376b479d..origin/dev), 로컬 커밋 변경 80개(376b479d..HEAD), dirty·untracked 289개.
- 원격 ∩ 로컬 커밋: 20개. 이 중 Plan7 핵심 경로는 `app/cli/commands/run.py`, `capability/gated_manifest.py`, `gated_network_policy.py`, `gated_strategy.py`, `shell_preparation.py`, `shell_runtime.py`, `core/contracts/completion.py`, `orchestration/central_state.py`, `engine.py`, `hypothesis_planner.py`, `probe_atomization.py`, `specialist_planner.py`, `specialist_tasks.py`, `analysis.py`, `tests/unit/test_gated_adapter_composed.py`, `tests/unit/test_hypothesis_split.py`, `tests/unit/test_probe_atomization.py`, `tests/integration/test_v3_atomization.py`, `docs/contracts/config.md`.
- 원격 ∩ dirty: 27개. 핵심은 `config/agents/native-probes.yaml`, `prompts/hypothesis-strategy.md`, `prompts/probe-atomizer.md`, `app/central_run.py`, `app/cli/commands/run.py`, `app/cli/tool_context.py`, `app/web/engagement.py`, `app/web_policy.py`, `capability/gated_window.py`, `capability/shell_runtime.py`, `capability/shell_workroom.py`, `orchestration/central_state.py`, `completion.py`, `hypothesis_planner.py`, `probe_atomization.py`, `semantic_completion.py`, `specialist_planner.py`, `specialist_tasks.py`.
- 원격에만 있는 신규 파일(예: `capability/direct_target.py`, `capability/gated_routing.py`, `orchestration/planner_context.py`, `orchestration/investigation_context.py`, `app/kali_preparation.py`)은 로컬 교집합이 없다. 병합 시 이름 충돌은 없지만 기능 중복 검토가 필요하다.

## 요구별 원격 구현 범위

등급: 구현 = 소스 경로와 호출 연결이 확인됨. 부분 = 일부만 확인됨. 반대 = 요구와 충돌. 미확인 = 이 감사에서 원격 소스로 확인하지 못함.

### C 그룹: 워커의 program 발견·paging·원본 피드백

- C01 구현(발견·paging): `orchestration/tool_discovery.py`에서 `PROGRAM_LIST_ID` 101행, `PROGRAM_DESCRIBE_ID` 102행, `MAX_LIST_CARDS=100` 73행, `collect_program_rows` 380행, `program_cards_for_query` 433행, `_page_args` 533행, `program_list_handler` 727행, `program_describe_handler` 805행, 등록 1057–1058행. 핸들러는 `_page_args`의 offset/limit을 쓰고 `_cards_payload`로 bounded 응답을 만든다. 역할별 선택은 `tool_access.specialist_tool_ids`를 통한다. 등록 여부만이 아니라 handler가 실제 응답을 생산한다. 실행 확인은 안 했다.
- C02 부분: 원격 `capability/direct_target.py`의 `DirectTargetExecutionPolicy`(455행)와 `execute`는 proposal→실행 경로를 탄다. 다만 gate 원장과 `network_enforcement="none"`(541행)이라 C02의 gate 증거 요구는 충족하지 못한다.
- C03 미확인: `evidence_access.py`, `native_local_tools.py`가 변경됐으나 bounded 원본을 다음 워커·Planner에 전달하는 consumer 경로는 이 감사에서 추적하지 못했다. 등급은 미확인이다.
- C04 반대: 원격 `program_execution_routes()`(direct_target.py 358행)가 조건 없이 `target_ready`(361행)와 `limits={"network_enforcement":"none"}`(368행)을 낸다. ROE 강제 검증이 없다. B04 및 E05 요구와 충돌한다.

### P 그룹: planner 진행·무진행

- 원격 `settings.py:35`에 `max_no_progress_turns: 3`, `state.py:143`에 `no_progress_turns`가 있다. 같은 값이 HEAD에도 있다(HEAD settings.py:35, 137행 주석). 카운터 정의는 있다.
- 원격에서 "경로 없는 continue를 진단하고 유한 repair로 보내는" 전용 분기는 찾지 못했다. `central_state.py:1111 continue_settled_actor`와 `runner.py:643 _continue_completed_central`은 continue 상태 전이이지만 실행 가능한 후속 경로 존재 검사로 읽히지 않는다.
- 등급: P01 미확인, P02 미확인, P03 미확인. 토큰 절감(38179561)은 진행 판단과 다른 축이므로 P 충족 근거가 아니다.

### K 그룹: 모순 완료 조건·review 체인·purpose·diagnostics

- K01 부분 (HEAD와 다르게 바뀜): 원격 `core/contracts/completion.py`에 `contradictory_candidate_verdict`(68행)가 있다. 호출자는 `probe_atomization.py` 301행(코드 305행)과 `specialist_tasks.py` 102–103행 두 곳이다. 같은 후보의 confirmed/refuted AND를 거절하는 producer 경로는 있다. 반면 HEAD의 `completion_condition_conflicts`(47행)와 `CompletionConditionConflict`는 원격에서 제거됐다. HEAD의 `central_state.py` 530행 호출도 원격에서 사라졌다. canonical 저장 경로에서 같은 검사가 중복되지 않는다. 원격은 K01을 producer 두 곳에서만 막는다.
- K02 부분: 원격 `central_completion.py` 178행(`verify_task_receipt`)과 289행(`check_result_review`), `semantic_completion.py` 190행(`verify_task_receipt`), 340행(`verify_receipt_bound_origins`), 410행과 912행(`check_result_review`), `result_claims.py` 101행이 있다. 관측 원본→receipt→독립 review 체인의 이름과 호출은 있다. Planner 후속 판단과 verdict 연결의 실제 전달은 이 감사에서 추적하지 못했다.
- K03 구현(본문 동일): `completion_contract_hash`(원격 50행)의 본문은 HEAD와 diff에서 바뀌지 않았다. criteria, targets, principals, hypotheses, v2 hypothesis_revisions, probe_limits를 해시한다. purpose_hash 전역 재정의는 없다. `analysis.py` 1641행의 `candidate_purpose_hash`와 `central_state.py` 672행도 같은 체계를 쓴다.
- K04 반대(회귀): 원격은 구조화된 `CompletionConditionConflict` diagnostics와 `_diagnostic_reference`를 제거했다. 대신 `contradictory_candidate_verdict`는 hypothesis ID 하나만 반환한다. criterion ID와 bounded reference를 함께 남기던 HEAD의 진단 정보가 사라졌다. K04 요구(원인·소유자·criterion refs·repair 조건)는 충족하지 못한다.

### A·B·E 그룹 (Kali route와 physical proof)

- A01 부분: 원격 `app/bootstrap.py`는 `direct_target_shell`을 받고 486행에서 import, 493행에서 `configure_direct_target`을 호출한다. HEAD의 `gated_shell`/`configure_gated` 분기는 원격에 없다.
- A02 부분: `app/kali_preparation.py`와 `tests/unit/test_kali_preparation.py`가 있다. 원격에서 준비 provider가 direct를 가리킨다. nft readiness 실행 경로는 확인하지 못했다.
- A03 부분: `gated_window.py`의 `program_execution_routes`(244행 이후)가 reason_code를 구분한다. 이 분기는 gated 경로에만 있다.
- B01 구현: `capability/gated_routing.py` 32행 `direct_target_ips`가 IPv6, loopback, unspecified, multicast, link-local을 거부한다(17행 `TargetRouteError`). 자동 주소 치환이나 터널은 없다.
- B02 부분: `direct_target.py`의 `_validate`가 approved target, port, scope, pin을 검사한다. ledger·gate 경로는 없다.
- B03 부분: close를 `asyncio.shield`로 감싸고 실패 시 failure를 반환한다. UNKNOWN 보존과 재실행 금지는 확인하지 못했다.
- B04 반대: 위 C04와 같다.
- E01–E05 (physical proof): `tests/integration/test_gated_target_route_docker.py`가 있다. 35행에서 `REDTEAM_DOCKER_ROUTE_PROOF=1`이 없으면 skip되도록 설정된다. 테스트는 `configure_gated`를 직접 호출한다. 이 감사는 테스트를 실행하지 않았고 실행 기록을 찾지 못했다. 소스 테스트 구현은 있으나 실행 근거는 없다.
- 중복 descriptor 위험: `gated_manifest.py` 37–38행과 243행의 `shell_target_run`, `direct_target.py` 58행과 545행의 `_descriptor`가 같은 tool_id를 만든다. 원격 bootstrap은 direct 하나만 조립하므로 현재 원격 운영 경로에서 실제 충돌은 없다. 두 조립을 동시에 켜면 등록 충돌 위험이 생긴다.

## 원격 소스로 확인한 증거 위치

- 원격 테스트 존재(실행 안 함): tests/unit/test_direct_target.py, tests/unit/test_gated_target_route.py, tests/unit/test_program_execution_context.py, tests/unit/test_kali_preparation.py, tests/integration/test_program_probe_delivery.py, tests/integration/test_planner_context_lookup.py, tests/integration/test_completion_reuse_and_contradiction.py, tests/integration/test_gated_target_route_docker.py.
- planner 투영: `planner_context.py` 27행 `PlannerEvidenceQuery`, 56행 `PlannerContextRequest`, 69행 `lookup_only`, 80행 `project_planner_knowledge`, 99행 `graph_diagnostics` 보존, 106행 candidate 필드 보존. `investigation_context.py` 59행 `ContextRef`, 73행 `ContextQuery`, 136행 `InvestigationSnapshot`, 220행 `_preview`, 245행 reader.

## 병합 전 필수 판단

1. K04 회귀를 막는다. 원격 `contradictory_candidate_verdict`는 진단 정보를 잃었으므로 HEAD의 구조화 진단을 유지하고 그 위에 호출자를 붙인다.
2. C04·B04·E05는 원격 `target_ready`/`network_enforcement="none"`을 가져오지 않는다. ROE를 강제할 수 없는 route는 unavailable로 낸다.
3. A01은 HEAD의 `gated_shell` 분기를 보존한다. direct 조립은 별도 선택으로만 추가하고, 두 조립이 같은 tool_id를 만들지 않게 한다.
4. P01–P03은 원격에서 실행 가능한 후속 경로 검사를 찾지 못했다. 이 부분은 HEAD와 우리 dirty 변경을 기준으로 따로 판단한다.
5. native-probes.yaml과 프롬프트 파일은 원격 38179561과 우리 dirty 변경이 겹친다. 수동 병합하고 version과 fingerprint를 맞춘다.

## 미확인 한계

C03 consumer, P01–P03, K02 Planner 연결, K04 원격 진단 전달은 이 감사에서 추적을 끝내지 못했다. 원격 테스트 실행, Docker 실행, 실제 LLM 실행은 하지 않았다. 교집합은 파일 수준이며 의미 수준의 충돌은 아니다.

## 사용자 최신 결정에 따른 해석 변경 (2026-10-15)

이 감사 후 사용자가 팀원의 직접 프로그램 실행 방식을 선호한다고 명시했고, 계획 변경 및 오류 수정 후 도입을 요청했다. 따라서 위 B04/C04/E05의 이전 gate 강제 기준과 직접 실행 방식의 차이는 이제 거절 사유가 아니다. direct provider의 network_enforcement=none은 실제 한계를 정확히 전달해야 하며, target_ready는 직접 실행 경로의 준비 상태로 평가한다. 중복 descriptor는 두 경로를 함께 조립할 때의 조건부 위험이며, 직접 경로 하나를 채택하는 현재 통합 방향에서 실제 등록 충돌이라고 단정하지 않는다.

실제 보존·보강 대상은 구조화된 후보 모순 진단과 canonical admission 검사, 실행 가능한 continue와 중복 방지, 원본 증거·독립 판정·purpose/revision 보존, 취소·timeout·detach·UNKNOWN·저장/재개와 사용자 승인이다. completion_contract_hash 본문 일치만으로 전체 K03 재사용 경로 구현을 증명하지 못하므로 해당 항목은 '해시 본문 일치 확인, 전체 경로 미확인'으로 해석한다. 원격 Docker 테스트는 소스로 존재하지만 이 감사에서 실행하지 않았고, 검사한 자료에서 실행 기록을 확인하지 못했다는 뜻이다.
