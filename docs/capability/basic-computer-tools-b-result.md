# 담당자 B 결과: 명령 실행·Execution Substrate

작성일: 2026-10-02
브랜치: `capability`
상태: 로컬 구현·검증 완료, push하지 않음, operation 계약 합의 대기

## 단계별 결과

| 단계 | 상태 | 결과 |
| --- | --- | --- |
| P1 계약 제안 | 완료 | `analysis`, `AssetTarget`, `command_*`, HIGH/default approval 제안과 팀별 질문 작성 |
| P2 동기 실행기 | 완료 | Docker 전용 `run_command`, exact allowlist, digest image, 격리 argv 구현 |
| P3 제한·취소 | 완료 | bounded output, timeout, task cancellation, container/child cleanup 구현 |
| P4 연결 | 완료 | manifest, scoped adapter, settings/bootstrap profile, deploy 예시, Control/Gateway 통합 |
| P5 비동기 | 완료 | `job_start/status/cancel`, 소유권, 상태, TTL·용량 정리 구현 |
| P6 문서·검증 | 완료 | protocol/result, Windows 기준선 비교, WSL2/Docker 실환경 검증 |

`command_run`, `command_job_start`, `command_job_status`, `command_job_cancel`은 구현됐지만
팀 합의 전까지 canonical contract의 **합의 필요(제안)** 상태다. Agent 자율 선택 완료로
표시하지 않는다.

## 변경 파일

신규:

- `src/redteam/capability/servers/execution.py`
- `src/redteam/capability/execution_manifest.py`
- `src/redteam/capability/adapters/mcp/execution.py`
- `tests/unit/test_execution_mcp.py`
- `tests/unit/test_execution_manifest.py`
- `tests/integration/test_execution_mcp.py`
- `deploy/mcp/execution/README.md`
- `deploy/mcp/execution/policy.example.json`
- `deploy/mcp/execution/read.env.example`
- `docs/capability/basic-computer-tools-b-contract-proposal.md`
- `docs/capability/basic-computer-tools-b-protocol.md`
- `docs/capability/basic-computer-tools-b-result.md`

승인받은 공통 파일 추가:

| 파일 | 추가 내용 | 이유 |
| --- | --- | --- |
| `src/redteam/capability/settings.py` | execution profile 필드·정책 digest·exact binding 분기 | operator policy에서 stdio profile을 안전하게 구성 |
| `src/redteam/capability/bootstrap.py` | execution manifest 검증·adapter 선택 분기 | Registry discovery를 실제 schema와 대조 |
| `src/redteam/orchestration/action_naming.py` | `command_run`, `command_job_start/status/cancel` | CandidateCompiler가 제안 operation을 검증하도록 함 |
| `docs/contracts/actions.md` | execution operation의 합의 필요 상태와 경계 | 공통 계약 독자에게 임시 상태를 명시 |

공통 파일의 기존 줄 삭제는 0이다. Core contracts와 Control 정책은 수정하지 않았다.
팀 계획서 본문과 사용자 미추적 `docs/audit/`도 수정하지 않았다.

## A 담당자에게 보낼 변경 요약 초안

> Execution Substrate를 기존 workspace 패턴에 맞춰 추가했습니다. `settings.py`에는
> `execution` stdio profile과 policy digest/exact binding을 추가했고, `bootstrap.py`에는
> execution manifest 검증 및 `ExecutionMCPAdapter` 선택 분기만 추가했습니다.
> `action_naming.py`와 `docs/contracts/actions.md`에는 `analysis.command_run` 및
> `command_job_start/status/cancel`을 “합의 필요(제안)” 상태로 추가했습니다. 네 공통
> 파일은 기존 줄 삭제 없이 추가만 했고 Core·Control 계약은 변경하지 않았습니다.
> Registry discovery, CandidateCompiler, Control 승인 대기/거절 미실행, Gateway 실제
> MCP 호출을 테스트했습니다. operation 명칭과 운영 ROE는 팀 합의가 필요합니다.

## 테스트 결과

P0 Windows 기준선:

```text
1962 passed, 42 skipped, 100 failed, 41 errors
```

최종 Windows 전체 pytest:

```text
1985 passed, 43 skipped, 100 failed, 41 errors
```

증감: passed +23, skipped +1, failed +0, errors +0. 기존 실패·오류는 POSIX 전용
workspace/VDB/methodology와 Windows symlink·permission 조건이며 담당자 B 변경 전과 수가 같다.

담당자 B 선택 테스트:

```text
82 passed, 1 skipped
```

WSL2 + Docker 실제 opt-in 테스트:

```text
1 passed
```

실제 테스트는 로컬 digest
`python@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e`
와 합성 파일만 사용했다. 실제 stdio discovery/Gateway 호출, A의 workspace 목록·읽기,
container `sort` 결과 저장, 재읽기, timeout, 실행 중 job cancel, 잔여 execution container
0을 확인했다.

전체 Ruff는 기존 `deploy/docker/apktool-mcp/entrypoint.py` import 정렬 2건 때문에 실패한다.
담당자 B가 변경한 Python 파일은 Ruff를 통과했다. 기존 파일은 수정하지 않았다.

## 계획서 5장 상태

| 항목 | 구현 | 단위/통합 검증 | 실환경 검증 |
| --- | --- | --- | --- |
| `run_command` | 완료 | 완료 | 완료 |
| timeout 상한 | 완료 | 완료 | 완료 |
| stdout/stderr 상한·`truncated` | 완료 | 완료 | 미검증 |
| task/연결 취소 cleanup | 완료 | 완료 | job cancel 완료, stdio 연결 강제 종료는 미검증 |
| 상태 조회와 `job_id` | 완료 | 완료 | start/cancel 완료 |
| Docker 격리 옵션 | 완료 | argv 검증 | 실제 container 실행 완료 |
| `--network none` | 완료 | argv 검증 | outbound probe는 미검증 |
| workspace 하나·기본 ro | 완료 | 완료 | rw 8.1 scenario 완료 |
| shell 금지·allowlist | 완료 | 완료 | allowlisted `sort`/`tail` 완료 |
| 환경변수 비누출 | 완료 | process/Docker argv 검증 | 컨테이너 내부 dump는 미검증 |
| CPU·memory·PID 제한 | 완료 | argv 검증 | 부하 한계 측정은 미검증 |
| timeout/cancel 정리 | 완료 | 잔여 mock 0 | 잔여 Docker container 0 |
| structured 반환 | 완료 | schema·결과 검증 | 정상·timeout 완료 |
| `artifact_ref` | 보류 | 해당 없음 | 공통 계약 부재를 문서화 |

## 계획서 12장 체크리스트

- [ ] 공통 계약 및 권한 기준 확정 — operation/ROE 팀 합의 대기
- [x] 명령 실행 환경 구현 및 등록
- [x] discovery와 manifest 일치 검증
- [x] 작업공간 및 실행 격리 검증
- [x] 정책 거절과 실패·취소·시간 초과 검증
- [x] 파일·명령 통합 시나리오 통과
- [ ] Agent 팀과 도구 선택·실행 검증 — 후보 컴파일은 확인, 자율 선택은 미검증
- [x] 연결 설정, 실행 방법, 제한사항 문서화
- [x] 기존 사용자 변경 보존 및 결과 commit 기록

담당자 A/C의 파일·웹검색 범위처럼 이번 작업에서 구현하지 않은 항목은 체크하지 않는다.

## 남은 합의와 후속 작업

- PM·Control·Agent·A·Evidence 팀이 네 canonical operation과 승인 정책을 확정해야 한다.
- 상태 조회 위험도를 MEDIUM으로 분리하려면 server/profile별 risk 표현 방식을 합의해야 한다.
- Agent의 자율 후보 선택→승인 요청→job poll/cancel 전략은 Agent 팀 검증이 필요하다.
- 공통 artifact 계약 후 잘린 전체 출력의 `artifact_ref` 저장을 추가해야 한다.
- stdio 연결 자체를 강제로 끊었을 때 cleanup되는 실환경 fault-injection을 추가할 수 있다.
- 허용된 네트워크 진단 도구 없이 outbound probe를 하지 않았으므로 실제 egress 차단은
  Docker `--network none` 구성과 argv 검증까지만 근거가 있다.

## 재현 명령

Windows 선택 테스트:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest `
  tests\unit\test_execution_mcp.py `
  tests\unit\test_execution_manifest.py `
  tests\integration\test_execution_mcp.py `
  tests\unit\test_capability_settings.py `
  tests\unit\test_orchestration_candidates.py -q
```

WSL2/Docker 실제 테스트:

```bash
cd /mnt/c/WHS-Red-Teaming-Agent
export REDTEAM_TEST_EXECUTION_DOCKER=1
export REDTEAM_TEST_EXECUTION_IMAGE='python@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e'
python -m pytest \
  tests/integration/test_execution_mcp.py::test_real_stdio_docker_execution -q -s
```

실제 배포에서는 위 테스트 image를 그대로 채택하지 말고 내부 검토한 최소 text-tools
image를 자체 build한 뒤 digest를 고정해야 한다.
