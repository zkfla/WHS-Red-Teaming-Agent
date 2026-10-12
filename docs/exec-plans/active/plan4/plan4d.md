# 구현 프롬프트 4d — 중앙 Run의 원본 수집 CLI와 job 조회·수집 도구

너는 기존 실행의 원본 수집 진입점을 연결하는 담당자다. 루트와 core·orchestration·capability·control·app의 `AGENTS.md`, `plan1.md`, `plan2.md`, `plan4.md`, `plan4b.md`를 읽고 4b의 실제 구현·검증을 확인한다. F05·F17과 관련 CLI/job/원본 보장만 구현한다. 코드 산출물·웹 UI·다음 단계는 시작하지 않는다.

## 1. 문제와 먼저 확인할 경로

원래 도구가 실행됐지만 SDK 체크포인트가 저장되지 않은 상황에서, 다시 실행하지 않고 기존 결과만 확인·수집할 진입점이 필요하다. 현재 `InstanceDriver.collect_checkpointless_originals`는 남아 있고 `collect-originals` CLI는 삭제됐다.

별도로 기본 등록된 `job.reconcile`은 중앙 실행에서 항상 plan2c 예약 오류를 반환한다. 원래 job을 resume에서 수집하는 기능은 남아 있으므로 결과 수집 전체를 새로 만들지 않는다. checkpoint 없는 보류 수집과, 정상 worker가 등록된 job을 조회/수집하는 동작을 구분한다.

- `instance_driver.py`: 원본 수집, Run owner, quiescent 확인, activation inventory.
- `central_recovery.collect_original`, `execution_collection.py`, `execution_journal.py`: 원래 호출·job 참조로 조회할 수 있는 결과와 무결성 검사.
- `central_state.py`: 원래 Run·Task·actor·proposal·실행 참조.
- `job_local_tools.py`, `runtime_components.py`, `long_job_adapter`, `central_recovery.reconcile_originals`: 등록된 조회 도구와 실제 중앙 job 소비자.
- `app/central_views.py`, `app/bootstrap.py`, `app/cli/commands/resume.py`, `app/cli/__init__.py`: 원래 설정·binding·저장 경로의 조회와 중앙 조립.
- 삭제 전 CLI와 원본 수집 테스트는 `git show`로 의미만 확인한다. 삭제된 legacy resume 조립 함수를 통째로 복원하지 않는다.

## 2. 구현 요구

1. `collect-originals`를 중앙 Run에 연결하는 명시적 CLI 명령으로 복원한다. 기존 `run_id`, engagement 설정 입력, `--json` 계약을 우선 재사용하고 `--runtime legacy`를 추가하지 않는다.
2. 원래 Run의 중앙 상태, 설정·component/prompt/provider binding, Evidence·Audit·Approval·체크포인트 경로와 실행 원본을 먼저 확인한다. 현재 사용자 설정을 무조건 원본보다 우선시키거나 새 Run·ID·예산을 만들어 조립하지 않는다.
3. 기존 Run owner를 확보하고 진행 중인 actor/provider 실행과 경쟁하지 않는 상태에서 inventory·binding을 다시 확인한다. 점검과 수집 사이 상태가 바뀌면 기존 정합성 오류로 처리한다.
4. 원래 invocation/action/job 또는 기존 실행 복구 참조로 결과를 조회·수집한다. 새 대상 도구 호출, job 제출, 모델 판단, 워커 시작, 승인 허가, Task/목표 완료·graph/Finding 승격을 수행하지 않는다.
5. 원본 조회에 필요한 provider 공개 포트·transport 생명주기를 기존 조립 경로로 연결한다. 조회 자체의 네트워크·Control 정책이 필요하면 기존 계약을 따른다. 조회와 대상 재실행을 같은 동작으로 취급하지 않는다.
6. 실제 실행에 진입한 actor에 체크포인트가 없으면 결과를 수집해도 actor를 자동 재개하거나 담당·자원·불명 사용량을 풀지 않는다. 수집과 재개/완료/예산 정산의 권한을 구분한다.
7. 원본이 확인되지 않으면 불명·보류 상태와 사유를 유지한다. 실제 결과의 digest·Run·action·invocation·tool·입력·job 정체성이 다르면 거절한다. 성공 응답 문구만으로 원본을 인정하지 않는다.
8. 같은 결과를 다시 수집할 때 기존 불변 원본/영수증을 재사용한다. 동일 ID의 다른 내용은 정체성 충돌로 거절한다. 결과 본문을 중앙 DB에 다시 복사하지 않는다.
9. 없는 저장소·원본·체크포인트를 조회하면서 빈 DB나 새로운 SDK 스냅샷으로 만들지 않는다. 미지원 형식은 사유와 복원 조건을 표시하고 사용자 DB를 이전·초기화하지 않는다.
10. 기존 마스킹을 적용한 구조화 결과와 CLI 종료 코드를 제공한다. 원본 본문·자격증명·provider 핸들을 오류/JSON에 노출하지 않는다. 문서의 남아 있는 명령 설명과 실제 등록을 일치시킨다.

## 3. 중앙 job 조회·수집 연결

1. 중앙 기본 등록의 `job.reconcile`을 실제 현재 Run의 실행 원장·원본 조회 포트에 연결하고 plan2c 임시 거절을 제거한다. 없는 WorkStore/Work ID를 복원하지 않는다. 기존 work_id 입력 소비자를 확인해 중앙 실행/job 참조로의 명시적 호환 또는 오류 계약을 제공한다.
2. 목록 조회는 현재 Run에 등록된 job의 마스킹된 구조화 상태·참조만 반환한다. 다른 Run·없는 job·잘못된 owner/Task 참조를 거절하고 새 job·모델 판단·워커를 제출하지 않는다. adapter가 없으면 가용성/미지원 사유를 등록·detail·결과 계약에 맞춰 표시한다.
3. 기존 `collect_original`·receipt 검증·reconciliation을 재사용한다. 원래 job status/result를 확인해 보존된 invocation/action/input/job 정체성과 결과가 맞을 때만 중앙 실행 상태·관측 처리로 연결한다. 활성/UNKNOWN은 보류하고 같은 job을 재제출하지 않는다.
4. 정상 job 수집은 기존 실제 결과 commit 계약을 따라 원본·실행/관측을 반영할 수 있지만 Task/목표 성공을 자동 확정하지 않는다. 2절의 checkpoint 없는 CLI는 별도로 원본 수집만 하며 actor 재개·graph 승격·사용량 정산을 하지 않는 경계를 유지한다.
5. 중복 수집·동시 resume/worker 수집은 원래 receipt/owner CAS로 조정한다. 동일 결과의 graph/원본/사용량을 중복 반영하지 않고 SQL 안에서 provider status/result를 기다리지 않는다. 공개 도구 설명·schema·선택/등록과 실제 지원 동작을 일치시킨다.

## 4. 최소 회귀 검증

- 실제 진입·체크포인트 없음·조회 가능한 기존 결과: 같은 실행의 원본만 수집되고 actor/LLM/job 제출/대상 호출 횟수는 증가하지 않는다. 없는 체크포인트 파일도 생성되지 않는다.
- 결과 불명·조회 미지원·원본 손상·binding 변경·다른 Run/입력/job의 결과: 사유가 표시되고 원래 점유·사용량·불명 상태가 유지된다.
- 반복 수집은 원본·영수증 중복이나 완료·사용량 정산을 만들지 않는다. 같은 ID의 다른 원본은 거절된다.
- 현재 Run 실행과 경쟁하면 owner/quiescent 검사가 수집을 막는다. 조회 중 취소·오류에도 transport와 owner의 종료가 현재 계약대로 처리된다.
- CLI 등록·인자·JSON·실패 코드·마스킹을 실제 dispatcher를 통해 검사한다. `resume`과 원본 수집이 서로 대체 실행으로 이어지지 않는다.
- 실제 기본 등록의 `job.reconcile`을 worker local-tool 경로로 호출한다. 현재 Run 목록 조회, 활성 job 보류, 완료 job 원본 수집이 작동하며 임시 plan2c 오류·새 job 제출·자동 목표 성공은 없다.
- 다른 Run/job·입력 불일치·adapter 없음·낡은 owner·중복/경쟁 수집을 검사한다. 기존 resume 수집과 같은 원본/실행 의미를 유지하고 checkpoint 없는 CLI의 보류 경계를 침범하지 않는다.

임시 중앙 DB·명시적 원본 조회 더블·임시 저장 경로로 검사한다. 라이브 MCP·실제 대상·유료 LLM은 호출하지 않는다. 관련 최소 검사와 린트·타입 검사, CLI·복구 문서를 갱신하고 `plan4.md` 5절 형식으로 결과를 보고하라.
