# 구현 프롬프트 7d — Kali 관측을 막는 완료 조건과 원본 재사용 수정

`plan7.md`, `plan7a.md`~`plan7c.md`, 루트·Core·Orchestration·App의 `AGENTS.md`를 읽어라. Kali 실행 경로를 먼저 확보한 뒤 이번 단계의 실제 관측→반환→후속 판단 장애를 수정하라. 별도의 후보 엔진이나 모든 완료 조건의 OR 전환을 만들지 않는다.

## 근거와 소유 계약

기준 Run의 `Root Security Header Posture`와 `Root Missing Headers Review Path`는 같은 후보에 대한 확인·반증을 함께 AND 조건으로 등록했다. `task_completion_contract.check_task_conditions`는 모든 저장 조건을 검사한다. `semantic_completion`에서는 조건을 통과해야 독립 작업 완료 검토를 요청하는 경로도 있다. 원본을 쓰는 후속 작업에는 `purpose changed since the evidence`와 `no preserved raw result` 거절이 있었다.

`core/contracts/completion.py`, `core/contracts/graph.py`, `specialist_tasks.py`, `probe_atomization.py`, `hypothesis_planner.py`, `completion.py`, `task_completion_contract.py`, `semantic_completion.py`, `evidence_validity.py`, `central_results.py`와 후보 verdict admission·독립 review 생산자를 읽어라. raw 원본, task completion receipt, candidate verdict receipt와 reuse binding을 서로 구분하라.

## 구현 요구

1. 새 Task 조건은 이번 검증 목적의 관측/반환으로 표현한다. 일반 조사 워커에게 아직 열린 후보의 최종 확인/반증·Finding을 먼저 완료하라고 요구하지 않는다. 자연어 `If`로 결과 분기를 암시해도 AND semantics는 바뀌지 않는다고 실제 Planner/atomizer schema·system·프롬프트에 전달한다.
2. 같은 hypothesis ID에 `hypothesis_confirmed`와 `hypothesis_refuted`를 함께 요구하는 모순은 구조화된 조건으로 판정하고 계획 검증에서 거절·유한 repair를 제공하라. 두 상태를 가진 서로 다른 후보나 이미 확정된 사실을 검사하는 정상 작업까지 일괄 금지하지 않는다. 설명 문구·제목·취약점 이름을 parsing해 실행 정책을 결정하지 않는다.
3. 후보 verdict 조건을 모두 삭제하거나 전역 OR로 바꾸지 않는다. 일반 관측 작업과 이미 확정된 결과를 확인하는 작업의 의미를 구분한다. contradiction이 원자화로 넘어가기 전에 Planner의 조건 생산 지점에서 진단하고, 같은 validator를 canonical 등록 boundary에서도 사용해 다른 공개 호출이 우회하지 못하게 한다.
4. 워커의 원본 관측→작업 관측 반환/검토→Planner의 후보 후속 판단→정확한 revision/purpose의 독립 후보 판정이 실제 호출 가능한 경로인지 확인하라. 없다면 기존 reviewer/provider/stage에서 필요한 입력·출력 연결만 보완한다. 후보가 확인돼야 관측 Task가 끝나고 그 Task가 끝나야 후보가 확인되는 의존을 만들지 않는다. atomic candidate/task 등록은 유지한다.
5. 원본 evidence의 실행 당시 task purpose·target·principal·action/invocation은 변경하지 않는다. 새 후속 Task가 이를 읽는 것과 자기 목적의 성공 증거로 채택하는 것은 다르다. 기존 `source_task_id/source_result_id`, 공개 원본 참조·유효성/reuse 검증·독립 review를 통해 합법적인 후속 사용을 연결하라.
6. 정확히 어떤 binding/validity가 후속 사용을 거절하는지 producer부터 확인하라. 정당한 다른 Task의 관측 재사용이 단순 purpose 문자열 불일치로 막히는 재현을 만들고, 기존 reuse 계약의 소유 모듈에서 해결하라. task purpose hash 검사를 전역 제거하거나 다른 task/target/session의 증거를 자동으로 관련 있다고 취급하지 않는다. 현재 계약이 표현하지 못하는 경우 최소 binding 의미와 저장·보고·resume 영향을 명시한다.
7. 이미 읽은 raw 원본을 반복 조회하거나 raw 없는 완료 receipt를 물리 원본 대신 제출하는 흐름을 정확한 진단으로 반환하라. 코드가 scope·참조·상태를 검증하고, 보안적 의미나 새 가설의 유용성은 모델/독립 review가 판단한다. 인과가 없는 재시도나 모든 UNKNOWN을 내부 코드 결함으로 분류하지 않는다.
8. 후보 확인 실패·목적 재사용 거절·필요한 대상 관측 부족을 기존 `DecisionDiagnostic`/`CompletionIssues`에 원인·소유자·관련 criterion/source refs·수정 또는 재개 조건으로 남겨라. 근거 없는 실패 분류는 unknown으로 유지하되 이미 아는 구조화된 binding 원인을 일반 안내로 잃지 않는다.
9. 과거 Task의 모순 조건·결과를 조용히 고치지 않는다. 역사적 조회·보고를 유지하고 새 목적/Task는 기존 후속 등록 계약으로 생성한다. 이번 수정 전에 구현된 condition ID 선택·원본 복원을 되돌리지 않는다.

## 최소 검증과 완료 조건

- 같은 후보의 확인+반증 AND는 생산/등록에서 거절되고 구체적 criterion/hypothesis 참조가 repair에 전달된다. 관측 목적·서로 다른 후보의 정상 조건은 유지된다.
- 실제 bound original을 보존한 관측 작업이 후보의 사전 확정 없이 반환될 수 있고, 이후 독립 판정으로 후보 revision을 갱신할 수 있다. fake가 graph 상태를 직접 확정한 것만으로 전체 경로 완료를 주장하지 않는다.
- 명시한 source pair와 valid original을 가진 후속 재사용은 필요한 review/binding을 거쳐 진행한다. wrong target/principal/source, stale purpose/revision, raw 없는 receipt, forged/missing evidence는 계속 거절된다.
- CLI/web 보고에는 Task 결과·후보·Finding의 차이가 유지되고 해결된 승인 대기가 현재 장애로 남지 않는다.

이 단계 완료는 결과 피드백의 정합성이다. 실제 취약점 발견이나 모델의 조사 능력 향상으로 표현하지 말고 다음 단계는 자동 실행하지 마라.
