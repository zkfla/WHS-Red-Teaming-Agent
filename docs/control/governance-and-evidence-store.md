# Track B: Approval, Audit, and Evidence Store

이 문서는 Track B의 승인 처리, 감사 기록, 파일 기반 Evidence 검사 규칙을
정리한다. 차단 Action의 `EvidenceRecord` 필드 규격과 Track A 연결 지점은
[`blocked-action-evidence.md`](./blocked-action-evidence.md)를 따른다.
Core Contract와 다른 팀의 구현은 변경하지 않는다.

## Approval과 Audit

- `ApprovalService.request(action_id)`는 `PENDING` 승인을 저장한다.
- `resolve(approval_id, status, resolved_by)`는 현재 상태가 `PENDING`이고
  요청 상태가 `APPROVED` 또는 `REJECTED`일 때만 저장한다. 처리된 승인을
  다시 처리하려 하면 `ValueError`를 발생시킨다.
- `python -m redteam.app.cli approvals resolve <id> approved --by <name>`은
  먼저 영속 Audit에서 해당 `action_id`와 `approval_id`가 모두 일치하는
  `REQUIRE_APPROVAL` 정책 결정을 찾는다. 찾지 못하면 승인 상태를 바꾸지
  않는다. 승인 저장 후 원래 Run·Engagement·Action ID를 사용해
  `APPROVAL_RESOLVED` Audit 항목을 추가한다.
- 승인 파일과 Audit 파일은 별개다. 승인 저장 이후 Audit 쓰기에 실패하면
  상태 변경과 감사 기록이 불일치할 수 있다. 여러 프로세스가 같은 승인을
  동시에 처리하는 경우도 현재 저장소는 원자적 상태 전이를 보장하지 않는다.

기본 경로는 `data/approvals`와 `data/audit`이다. 별도 경로를 사용할 때는
`approvals resolve --dir <approval-dir> --audit-dir <audit-dir>`로
두 경로를 모두 지정한다.

## Approval 만료

`ApprovalService.sweep_expired(ttl_seconds)`는 요청 시각부터 TTL 이상 지난
`PENDING` 승인만 `EXPIRED`로 저장한다. TTL은 유한한 0 이상의 숫자여야
한다. 이미 승인·거절·만료된 항목은 변경하지 않는다.

```bash
python -m redteam.app.cli approvals expire --ttl-seconds 3600
```

이 명령은 호출할 때 한 번 실행된다. TTL 기본값이나 자동 스케줄러는 없다.
운영자는 필요한 주기에 맞춰 명령을 실행해야 한다. 현재 만료 CLI는
`APPROVAL_RESOLVED` Audit 항목을 추가하지 않는다.

## Evidence 파일 검사

`FilesystemEvidenceStore`는 Run별 JSONL 파일과 `_index.jsonl`을 사용한다.
`verify_run_integrity(run_id)`는 인덱스의 Evidence ID·줄 해시와 Run
파일을 양방향으로 대조해 다음 ID를 반환한다.

- 내용이 변경되거나 중복된 기록
- 인덱스에는 있지만 Run 파일에서 빠진 기록(파일 전체 삭제 포함)
- Run 파일에는 있지만 인덱스에 없는 기록

문제가 없으면 빈 목록을 반환한다. `evidence verify --run <run-id>`는
문제가 있으면 ID를 출력하고 종료 코드 1을 반환한다.

이 검사는 로컬 인덱스가 기준이며 독립적으로 서명된 감사 증명은 아니다.
파일과 인덱스가 함께 삭제·수정되면 검출할 수 없다. 또한
`EvidenceRecord.digest`의 원본 payload는 저장하지 않으므로 그 digest를
재계산하지 않는다. 이 검사는 저장된 JSONL 줄의 해시를 확인한다.

`evidence purge --days <n>`은 파일 수정 시각을 기준으로 오래된 Run 파일과
해당 인덱스 항목을 삭제한다. 자동으로 실행되지 않으며 삭제된 Run은
이후 검사 대상이 아니다.

## 팀 간 연결

Track A는 `DefaultControlPlane`과 Bootstrap에서 차단 Evidence를 연결한다.
Track B의 Approval·Audit·Evidence 파일 저장소는 이 연결 전에 독립적으로
동작한다. 승인 후 Run 재개는 Agent 팀의 실행 흐름에 속한다.
