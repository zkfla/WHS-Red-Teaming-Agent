# Execution Substrate MCP

상태: `command_run` 계약은 **합의 필요(제안)** 이다. 이 profile을 설정하는 것만으로
운영 ROE나 승인이 부여되지는 않는다.

서버는 WSL2/Linux에서 Docker daemon을 사용해 allowlist 명령을 실행한다. Docker를
사용할 수 없거나 POSIX 환경이 아니면 시작을 거절하고 호스트 실행으로 대체하지 않는다.

## 준비

1. `policy.example.json`을 `policy.json`으로 복사한다.
2. `workspace_root`를 engagement/run 전용 절대 경로로 바꾸고 권한을 제한한다.
3. 텍스트 도구가 들어 있는 내부 이미지를 빌드·검토한 뒤 `image`를 실제
   `repository@sha256:<64 hex>` digest로 교체한다. 예시의 0 digest는 배포용이 아니다.
4. 기본 `writable: false`를 유지한다. 계획서 8.1의 결과 저장을 검증할 때만 별도의
   operator 승인 후 `true`로 바꾼다.
5. `container_user`는 root가 아닌 workspace 소유자의 WSL UID:GID와 맞춘다
   (`id -u`, `id -g`). 그래야 mode 0700 workspace를 다른 host 사용자에게 열지 않고
   공유할 수 있다.
6. `read.env.example`을 참고해 WSL2 shell에 환경변수를 설정한다. secret이나 Docker
   socket 경로를 passthrough하지 않는다.

## 직접 서버 확인

```bash
POLICY=deploy/mcp/execution/policy.json
SHA256=$(sha256sum "$POLICY" | cut -d' ' -f1)
python -m redteam.capability.servers.execution \
  --policy "$POLICY" --sha256 "$SHA256"
```

Docker invocation은 shell을 사용하지 않으며 다음 경계를 고정한다.

- `--network none`, `--read-only`
- `--tmpfs /tmp:rw,noexec,nosuid,nodev,...`
- `--cap-drop ALL`, `--security-opt no-new-privileges`
- 비root `--user`, `--pids-limit`, `--memory`, `--cpus`
- 승인된 workspace 하나만 `/workspace`에 bind mount, 기본 `readonly`
- 컨테이너 내부 환경은 `PATH`, `LANG`만 지정

`docker.sock`, 호스트 홈, 인증정보, 전체 환경은 mount하거나 전달하지 않는다.

## 실환경 테스트

실제 digest와 합성 workspace를 준비한 뒤 다음 환경변수로 opt-in 통합 테스트를 실행한다.

```bash
export REDTEAM_TEST_EXECUTION_DOCKER=1
export REDTEAM_TEST_EXECUTION_IMAGE='repository@sha256:<digest>'
python -m pytest tests/integration/test_execution_mcp.py -q -s
```

큰 출력은 정책 상한까지만 반환하고 `truncated=true`로 표시한다. 공통 artifact 계약이
없으므로 `artifact_ref`는 생성하지 않는다.

## Agent 임시 연결 opt-in

`agent-opt-in.example.json`은 기존 Analysis Agent가 `command_run`을 후보로 볼 수 있게
하는 최소 설정 조각이다. 기본값은 꺼짐이며 `engagement.metadata.execution_agent`가
없거나 `enabled`가 `false`이면 execution 도구는 Agent 문맥에서 보이지 않는다.

활성화하려면 다음 조건을 모두 충족해야 한다.

- `metadata.execution_agent.enabled`를 `true`로 지정한다.
- `assets` 키는 engagement scope에 이미 선언된 Asset과 정확히 같아야 한다.
- 각 Asset에는 코드가 지원하는 `line_count` recipe와 정규화된 workspace 상대 경로를
  지정한다. 절대 경로, 역슬래시, `.` 또는 `..` 경로 요소는 거절된다.
- ROE에는 `analysis` capability와 제안 operation `analysis.command_run`을 명시한다.
- MCP 서버 및 Gateway allowlist에도 `run_command`/`command_run`을 명시한다.

Agent가 제시한 executable이나 args는 실행에 사용하지 않는다. 현재 bridge는 그래프에
seed된 Asset 사실에서 `wc -l <relative-path>` 인자를 다시 만들며, execution 서버의
executable·option allowlist가 마지막 방어선이다. 비동기 command job operation은 이
임시 Agent 연결에서 노출하지 않는다.

이 연결은 후보 노출, 스케줄, Control 정책 경유, Gateway/MCP 실행 및 Evidence 기록까지
검증한다. 실제 LLM이 자유 판단으로 이 후보를 고르는지에 대한 검증은 Agent 팀의 별도
종단 테스트가 필요하다. 팀의 Agent workflow 재설계 때 이 임시 bridge는 교체할 수 있다.
