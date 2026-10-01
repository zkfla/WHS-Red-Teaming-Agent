# 담당자 B 프로토콜: 명령 실행·Execution Substrate

상태: 구현 및 Capability 연결 완료, canonical operation 팀 합의 필요
적용 환경: WSL2/Linux + Docker
대상: operator가 등록한 engagement/run 전용 `AssetTarget` workspace

## 계약 경계

Execution Substrate는 기존 `analysis` capability와 `AssetTarget`을 재사용한다.
아래 operation은 모두 **합의 필요(제안)** 상태다.

| MCP tool | canonical operation | 위험도 | 입력 | 출력 |
| --- | --- | --- | --- | --- |
| `run_command` | `command_run` | HIGH | `asset_ref`, `executable`, `args`, `timeout_seconds?` | `exit_code`, `stdout`, `stderr`, `timed_out`, `truncated`, `duration_ms` |
| `job_start` | `command_job_start` | HIGH | `asset_ref`, `executable`, `args`, `timeout_seconds?` | `job_id`, `status` |
| `job_status` | `command_job_status` | HIGH | `asset_ref`, `job_id` | 상태, 완료 result 또는 고정 error |
| `job_cancel` | `command_job_cancel` | HIGH | `asset_ref`, `job_id` | 상태, `cancelled` |

profile 단위 위험도만 지원하는 현재 Registry 구조 때문에 상태 조회도 보수적으로 HIGH다.
Control 계약은 바꾸지 않았으며, 운영 ROE와 사람 승인은 별도로 설정해야 한다.

## 호출 흐름

```text
Agent proposal (analysis.command_*, AssetTarget)
  -> CandidateCompiler (asset_id -> asset_ref)
  -> Control (scope, ROE, risk, approval)
  -> ToolInvocation
  -> MCPToolGateway
  -> ExecutionMCPAdapter (trusted engagement_id/run_id 주입)
  -> stdio Execution MCP
  -> Docker argv 실행
```

DENY 또는 REQUIRE_APPROVAL 상태에서는 `ToolInvocation`을 만들 수 없으므로 Docker를
시작하지 않는다. Registry 발견, 후보 노출, Control 승인, 실제 호출 성공은 각각 별도
검증 상태로 기록한다.

## Operator policy

정책은 한 `asset_ref`, `engagement_id`, `run_id`, `workspace_root`를 고정한다. 이미지에는
`repository@sha256:<64 hex>` 형식만 허용한다. `allowed_executables`는 tool name과
`/usr/bin/<name>`의 정확한 mapping이며 shell과 임의 코드 실행기는 거절한다.

주요 제한:

- `timeout_seconds <= max_timeout_seconds`
- stdout+stderr 보존량 `<= max_output_bytes`, 초과 시 `truncated=true`
- `max_jobs`, `completed_job_ttl_seconds`
- `--pids-limit`, `--memory`, `--cpus`
- 기본 workspace mount `readonly`; 쓰기는 operator policy opt-in
- `container_user`는 root가 아닌 workspace 소유자의 UID:GID

절대 경로, Windows drive 경로, 역슬래시, `..`, NUL/newline과 shell metacharacter가 든
인자는 Docker 시작 전에 거절한다. 실행 파일과 인자는 항상 분리된 argv로 전달한다.

## Docker 격리

실행마다 임의의 `redteam-exec-<uuid>` 이름을 사용한다.

```text
docker run --rm --name <name>
  --network none --read-only
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=<limit>
  --cap-drop ALL --security-opt no-new-privileges
  --user <non-root uid:gid>
  --pids-limit <limit> --memory <limit> --cpus <limit>
  --workdir /workspace
  --mount type=bind,src=<one workspace>,dst=/workspace,readonly
  --env PATH=/usr/local/bin:/usr/bin:/bin --env LANG=C.UTF-8
  <digest-pinned image> <allowlisted executable> <args...>
```

docker.sock, host home, credential 파일과 전체 host 환경은 전달하지 않는다. Docker 또는
POSIX 실행 환경을 사용할 수 없으면 서버가 시작을 거절하며 host fallback은 없다.

## Timeout, cancellation, output

stdout/stderr는 동시에 drain하지만 메모리에는 정책 상한까지만 보관한다. 정상 exit와
비정상 exit code는 `timed_out=false` 결과로 반환한다. timeout은 알려진 container name에
`docker rm --force`를 실행하고 Docker CLI child도 종료한 뒤 `timed_out=true`로 반환한다.
MCP task/연결 취소도 같은 cleanup 경로를 수행한 후 cancellation을 전파한다.

cleanup command가 성공하지 않으면 `execution_container_cleanup_failed`로 fail closed한다.

## Async jobs

`job_start`는 검증을 먼저 수행하고 UUID 기반 `job_id`를 반환한다. job은
asset/engagement/run 소유권과 함께 서버 메모리에 보관된다. 다른 context는 존재 여부를
노출하지 않고 거절한다.

상태는 다음과 같다.

- `running`: task 실행 중
- `completed`: Docker 실행이 끝남. nonzero exit와 timeout 정보는 result에서 구별
- `failed`: Docker spawn 또는 cleanup 등 substrate 실패
- `cancelled`: cancel 요청 후 컨테이너와 child cleanup 완료

완료 job은 `completed_job_ttl_seconds` 후 제거한다. `max_jobs`에 도달하면 가장 오래된
완료 job부터 제거하며, 실행 중 job만 남았으면 새 job을 거절한다. 서버 재시작 후 job은
복구되지 않는다.

## 설정과 실행

예시는 `deploy/mcp/execution/`에 있다. policy의 placeholder digest를 실제 검토한 이미지
digest로 교체하고, `container_user`를 WSL workspace 소유자 UID:GID로 바꿔야 한다.

```bash
export REDTEAM_MCP_SERVERS=execution
export REDTEAM_MCP_EXECUTION_KIND=analysis
export REDTEAM_MCP_EXECUTION_TRANSPORT=stdio
export REDTEAM_MCP_EXECUTION_COMMAND=python
export REDTEAM_MCP_EXECUTION_PROFILE=execution
export REDTEAM_MCP_EXECUTION_RISK_LEVEL=high
export REDTEAM_MCP_EXECUTION_EXECUTION_POLICY=/absolute/path/execution-policy.json
```

settings가 policy byte digest, exact tool allowlist와 operation binding을 구성한다. stdio
서버는 전달받은 digest를 다시 검사하고 다르면 시작하지 않는다.

## Artifact 보류

공통 `artifact_ref` 생성·보존 계약이 없으므로 이번 구현은 큰 출력을 저장하지 않는다.
상한까지만 반환하고 `truncated=true`를 남긴다. Evidence 팀과 공통 계약을 합의한 뒤
별도 확장해야 한다.
