> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# I07 수정 노트 — 공개 단계 계약만 쓰는 재개·운영자 지시

브랜치: dev / 검증 방식: 소스 정독만 (루트 금지령으로 테스트·컴파일·린트 미실행)

## 1. 변경 전 / 변경 후

### 1-1. 재개 검사 (runner.py:296)

- 전: change.tasks._missions.get(MissionId(...)) — 단계 저장소의 비공개 사전을 직접 조회
- 후: change.tasks.peek_mission(MissionId(...)) — 공개 단계 조회로 교체
- 동작 동일: 없으면 None, 있으면 단계 계획 반환. 잠금·복사 없이 Backend 경계 안에서 읽는다.

### 1-2. 재활성화 (runner.py:318)

- 전: 같은 비공개 사전 직접 조회
- 후: change.tasks.peek_mission(MissionId(...)) 으로 교체
- 주: 같은 함수 안에서 바인딩 조회·대기 지시 확인은 원래부터 공개 계약(binding/pending)을 쓰고 있었고, 이번에 손댄 건 계획 조회 한 줄뿐이다.

### 1-3. 지시 봉투 검증 (service.py:603) / 지시 등록 (service.py:655)

- 전: change.tasks._missions.get(record.intent.mission_id) 두 군데
- 후: change.tasks.peek_mission(record.intent.mission_id) 두 군데
- 인자 형 변환 없음: record.intent.mission_id 는 원래부터 MissionId 라 그대로 넘긴다.

### 1-4. 지시 적용의 부분 실패 복구 (service.py:767 / :776)

- 전: 적용 직전 비공개 사전 두 개를 항목별 깊은 복사로 떠 두고, 변이 실패 시 비공개 사전에 도로 끼워 넣음
- 후: staged_snapshot = change.tasks.snapshot_tasks() 로 떠 두고, 실패 시 change.tasks.restore_tasks(staged_snapshot) 으로 복구
- 새로 짠 게 아니라 기존 복사 방식을 저장소 안으로 옮긴 것이다. 복사 단위(계획·과제 항목별 깊은 복사)와 복구 시점(거절 판정 직전)은 전과 같다.

## 2. 롤백 유지 판단 근거 (제거가 아니라 공개 계약으로 이관)

### 백엔드는 콜백 예외 시 staged 전체를 버린다

- 메모리 제공자 change (state_backend.py:73-94): staged 복사본을 만들어 콜백을 돌리고, 콜백이 끝나야 정식 영역으로 갈아끼운다. 콜백이 예외를 던지면 갈아끼우기 줄을 못 타서 정식 영역은 그대로다.
- SQLite 제공자 _transaction (state_backend.py:133-183): 예외 나면 db.rollback() 하고 다시 던진다. staged 는 버려지고 커밋 안 된다.
- 즉 콜백이 예외를 밖으로 던지는 한, 수동 복구는 필요 없다.

### 그런데 지시 적용부는 예외를 삼키고 계속 커밋한다

- _apply_record 안쪽 apply (service.py:746-854): 변이 단계(_mutate 호출, service.py:779)를 TaskGraphError/ValueError 로 잡아서 거절로 기록하고, 예외를 다시 던지지 않은 채 같은 change 안에서 봉투 발행·레코드 저장까지 이어간다. 바깥 coordinator.change 는 정상 반환이라 백엔드가 staged 를 통째로 커밋한다.
- 변이는 중간에 터질 수 있다: 중단 처리는 과제 목록을 돌면서 하나씩 취소하고(service.py:1404-1416), 우선순위 변경도 과제마다 하나씩 반영한다(service.py:1431-1435, 1473-1475). 세 번째 과제에서 규칙 위반이 나면 앞의 두 개는 이미 staged 에 적혀 있는 상태다. 숫자 변환 실패(service.py:1466-1468)도 먼저 반영된 우선순위 뒤에 터질 수 있다.
- 그래서 삼킨 실패에 대해서는 staged 를 적용 전으로 되돌리는 복구가 있어야 한다. 롤백 경로를 지우면(안 b) 부분 적용이 거절 건과 함께 커밋돼서, 거절됐다면서 과제는 바뀐 괴상한 상태가 된다.
- 검증-후-적용(안 b)은 변이 종류마다 사전 검사를 복제해야 해서 손이 크고, 규칙 검사가 저장소 안에 있어서 밖에서 다 따라 하기도 어렵다. 최소 정합은 복사/복원을 공개 계약으로 올리는 쪽(안 a)이다.

## 3. 새 공개 계약 (서명)

- task_graph/memory.py:55 — def snapshot_tasks(self) -> tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]]
- task_graph/memory.py:70 — def restore_tasks(self, snapshot: tuple[dict[MissionId, MissionPlan], dict[TaskId, MissionTask]]) -> None
- coordination.py:82 — StagedTaskGraph.snapshot_tasks (같은 서명, Protocol 선언)
- coordination.py:85 — StagedTaskGraph.restore_tasks (같은 서명, Protocol 선언)
- paths 쪽은 손대지 않았다: 지시 변이가 경로 저장소를 건드리지 않아서, 기존에 떠 두던 것도 계획·과제 두 사전뿐이었다.
- SQLite 경로도 덤으로 커버된다: SQLite 제공자의 staged 는 같은 메모리 저장소 클래스를 불러와서 쓰기(_load_memory) 때문에 새 메서드가 그대로 붙는다.

## 4. 남은 한계

- 스냅샷은 1회용이다. restore_tasks 는 받은 사전을 그대로 정식 staged 로 올리므로, 같은 스냅샷으로 두 번 복구하면 두 번째부터는 살아있는 상태를 가리킨다. 가져다 쓰는 쪽은 스냅샷 하나당 복구 한 번만 해야 한다 (현재 호출부는 그렇게 쓴다).
- 적용 콜백 안의 staged_service._tasks = change.tasks (service.py:764)는 그대로다. 이건 저장소 비공개 영역을 파는 게 아니라, 지시 처리 담당이 원래 들고 있던 슬롯에 staged 뷰를 꽂는 내부 배선이라 이번 건이 아니다.
- 상용 단계 뷰를 직접 구현한 제공자가 있다면 snapshot_tasks/restore_tasks 쌍도 갖춰야 지시 적용이 돈다. 없으면 적용 시점에 속성 오류가 난다. 기본 메모리/SQLite 제공자는 둘 다 갖췄다.
- coordination.py 에는 비공개 저장소 접근이 원래부터 없었다 (공개 Protocol 정의랑, 이미 공개 조회로 바뀐 ChangeSet 토큰/입장 확인만 있음). 캐시·수명 로직은 손대지 않았다.
- 실행 금지에 따라 컴파일·테스트·린트를 돌리지 않았다. 줄번호는 수정 직후 원본을 눈으로 읽고 적었다.
