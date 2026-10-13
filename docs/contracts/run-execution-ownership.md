# 전문 Run 실행 소유권 계약

이 계약은 같은 Run을 두 실행 조립자가 동시에 변경하는 것을 막는다.
Task claim, 도구 자원 lease, Control 승인, 실행 영수증 검증을 대체하지 않는다.
기존 Coordinator의 저장소 제공자가 소유권을 제공하고 기존 InstanceDriver가 보유한다.
새 작업 큐·실행기·분산 lock 서비스를 만들지 않는다.

## 1. 왜 기존 Task claim만으로 충분하지 않은가

재개는 원래 instance와 Task occupant를 유지한다. 따라서 두 프로세스가 같은
체크포인트를 읽으면 둘 다 같은 occupant로 인정될 수 있다. 드라이버 객체 내부의
`_active` 검사도 다른 드라이버의 실행 상태는 알지 못한다. 계획·검토·checkpoint
쓰기까지 중복될 수 있으므로, 도구 진입 직전이 아니라 전체 전문 조립 진입에서 막는다.

## 2. 소유 모듈과 적용 순서

`RunExecutionBackend.exclusive_run(run_id)`는 선택적 저장소 기능이다.
Coordinator는 이 공개 port만 사용하며 `atomic_scope()` 문자열을 해석하거나
제공자의 DB 경로를 읽지 않는다. 전문 모드에서 기능이 없으면
`run_execution_backend_unsupported`로 거절한다. 기존 일반 모드는 이 기능을 요구하지 않는다.

```text
기존 InstanceDriver.open
  → own_run: 배타적 소유권 확보 / 이미 소유 중이면 즉시 거절
  → 새 실행: 전문 계획·분해 → Ready Task 기동
  → 재개: 전체 cohort 영수증 복구 → Control 사용량 복원 → 원래 instance 재개
  → 실행/후속 판단/정산
  → 드라이버가 소유한 actor 종료·checkpointer close
  → Run 실행 소유권 반환
```

- 같은 드라이버의 같은 Run 재사용은 허용한다. 다른 Run으로 소유권을 바꾸지 않는다.
- 경쟁 프로세스는 기다리며 LLM을 재호출하거나 새 Task/instance를 만들지 않는다.
  `run_execution_already_owned`를 반환한다.
- 승인 watch도 같은 소유권을 먼저 확보한다. 사용자 pause/stop은 기존 의미를 유지한다.
- iterator 구독을 끝내도 실행 중 actor가 남아 있으면 소유권은 드라이버에 남는다.
  다른 프로세스가 actor drain 이전에 같은 Run을 재개할 수 없다.
- `close()`를 기다리는 호출자가 취소돼도 드라이버가 소유한 정리는 취소하지 않는다.
  actor drain·SDK 정리·소유권 반환 뒤 호출자의 취소를 전달한다. 정리 중 재개방은 거절한다.
- 소유권 경쟁이 실패하면 원래 Run의 Task, lifecycle, 요청 원장, checkpoint와 Control
  집계를 변경하지 않는다. 다른 Run은 독립적으로 소유권을 획득할 수 있다.

## 3. 저장소별 범위

| 저장소 | 구현과 범위 |
| --- | --- |
| Memory | 같은 backend의 실행 Run 집합을 기존 lock 아래 변경. 실행 중에는 상태 lock을 계속 잡지 않음. 프로세스 간 보장은 없음 |
| SQLite / POSIX | canonical DB 경로 옆의 Run hash companion 파일에 `flock(LOCK_EX\|LOCK_NB)`. 같은 로컬 DB를 공유하는 독립 연결·프로세스에 적용 |
| SQLite / Windows | 같은 companion 파일을 `CreateFileW`의 공유 없는 비상속 handle로 보유. 경쟁 open은 즉시 거절하며 보유 중 파일 삭제·교체도 차단. scope 종료·프로세스 종료 때 handle 해제 |
| Custom | 동일한 배타성/정리 계약을 제공하는 선택적 port가 필요. 미지원 기능을 Memory로 조용히 대체하지 않음 |

POSIX SQLite 소유권은 로컬 POSIX 파일 시스템을 전제로 한다. 파일 descriptor는 exec로
상속되지 않으며 파일은 private regular inode이어야 한다. symlink, hardlink,
디렉터리, FIFO 또는 타인 읽기/쓰기 권한을 가진 companion은 거절한다.
저장소/플랫폼 장애도 명확히 거절한다. 획득 과정에 await/thread 취소 창을 만들지 않는다.

Windows는 운영자가 관리하는 로컬 파일 시스템과 저장 디렉터리의 Windows ACL을
전제로 한다. POSIX mode bits를 Windows 접근 권한으로 해석하지 않는다. handle로
disk 파일·단일 hardlink를 확인하고 reparse point·디렉터리를 거절한다. 기존 파일을
truncate하거나 잠금 파일을 삭제하지 않는다. Linux 컨테이너와 Windows 호스트가
같은 DB를 동시에 조립하는 등 서로 다른 OS의 잠금 방식 간 배타성은 보장하지 않는다.
Windows CLI에서 판단/조정하고 Docker에서 도구를 실행하는 구성은 조립자가 하나이므로
이 교차 OS 조립에 해당하지 않는다. 이 변경은 다른 파일 저장소의 Windows 호환성이나
Docker 도구 실행 준비까지 보장하지 않는다.

중앙 SQLite 저장소와 레거시 이관 검사도 같은 `run_storage_ownership` 함수를
사용한다. 읽기 전용 검사(`create=False`)는 없는 companion을 생성하지 않는다.
Windows에서 POSIX private-inode 계약의 이관 마커가 발견되면 저장소 실행을
거절한다. 마커가 없는 일반 중앙 저장소의 소유권은 Windows handle 경로를 사용한다.

파일은 해제 후에도 남겨 둔다. 실행 중 파일을 삭제·교체하면 새 inode가 생겨 소유권이
둘로 갈라질 수 있기 때문이다. 유지보수 삭제는 모든 관련 프로세스가 종료된 별도
정비 시점의 작업이며 자동 TTL 정리로 구현하지 않았다. DB 파일을 hardlink로 복제하여
서로 다른 저장소 경로로 사용하는 배포나 NFS/여러 호스트의 분산 소유권은 보장하지 않는다.

장시간 모델/도구 I/O 동안 SQLite 쓰기 트랜잭션을 열어 두지 않는다.
기존 `change()`의 짧은 원자적 상태 변경은 계속 가능하므로 사용자 중단·범위 변경과
다른 워커의 상태 기록을 막지 않는다.

## 4. 프로세스 종료는 안전한 재실행의 증거가 아니다

OS가 소유권을 해제했다는 것은 조립 프로세스가 끝났다는 뜻이다.
외부 도구·분리된 자식 프로세스가 끝났거나 이전 요청이 실패했다고 단정하지 않는다.
다시 소유권을 확보해도 다음은 그대로 유지한다.

- 원래 Task occupant, instance ID, 완료 조건과 계약 hash.
- dispatched/unknown LLM 요청의 사용량. 자동 환불·전체 한도 초기화 없음.
- `Work.resource_execution_pending`과 미확정 자원 충돌. TTL/프로세스 종료로 해제 없음.
- 원본 checkpoint와 execution receipt의 기존 reconcile 검사.
  unsafe/unknown 결과이면 실행 보류이며 원래 요청을 다시 보내지 않는다.

활성 Task 예약은 있지만 checkpoint가 아직 없는 crash를 임의로 새 instance로
대체하는 복구 기능을 이 계약이 추가한 것은 아니다. 전체 crash 복구와 실제
Mobile/브라우저·분산 실행의 검증은 별도 PRD 항목으로 남는다.

## 5. 검증한 범위

`test_run_execution_ownership.py`는 Memory/SQLite 배타성·독립 Run·정상 종료·취소,
canonical 경로, unsafe inode와 미지원 backend를 검증한다. 별도 PID의 정상 종료,
terminate/kill 뒤 소유권 확보가 가능하되 미확정 Work와 요청 계량이 그대로인지 확인한다.
Windows에서는 live companion 삭제/교체 차단·경로 대소문자 별칭·unsafe handle 검증도
확인한다. POSIX inode/mode/FIFO 검사는 POSIX에서, Windows sharing 검사는 Windows에서
수행하며 필요한 symlink 생성 권한이 없으면 해당 사례만 skip한다.

`test_specialist_run_ownership.py`는 public 전문 start와 CLI cohort 재개를 사용해
경쟁 시 계획/claim/reconcile/Control 복원보다 먼저 거절되는지 검증한다.
원래 승인된 action을 한 번 실행한 후 후속 Task가 진행되는 것도 확인한다.
가짜 actor는 iterator 종료·driver drain 수명 검사에만 사용한다.

종료 정리 취소 시험은 수정 전 Memory/SQLite에서 소유권 반환 누락을 재현했다.
수정 후 같은 시험에서 정리 중 경쟁 거절, actor 종료 뒤 소유권 반환과 호출자의
`CancelledError` 전달을 확인했다. lease TTL을 줄이거나 강제 claim 해제로 해결하지 않았다.

실제 유료 LLM·금융 사이트·외부 인증·Mobile 기기·브라우저 프로필을 실행한 검증은 아니다.
