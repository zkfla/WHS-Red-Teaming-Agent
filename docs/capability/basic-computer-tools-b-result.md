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

## G1~G10 보완 결과

| 항목 | 결과 | 근거 |
| --- | --- | --- |
| G1 네트워크 차단 | 완료 | 실행 중 container의 `NetworkMode=none` inspect와 test-only Python image의 `1.1.1.1:53` 연결 실패 확인 |
| G2 연결 종료 정리 | 완료 | stdio MCP 서버 SIGKILL fault injection 후 새 서버 시작 시 동일 context label의 고아 container 제거, 최종 0개 |
| G3 인자 범위 | 완료 | executable별 `allowed_options`, 위험 옵션 전용 오류와 일반 미허용 옵션 오류 분리, 기존 정책 기본값 호환 |
| G4 출력 제한 | 완료 | 실제 Docker stdout/stderr 각각 초과, `truncated=true`, 합산 retained buffer 1,024 bytes, 명령 종료 확인 |
| G5 자원 제한 | 완료 | inspect로 memory 64 MiB, 0.5 CPU, PID 64 확인; 16 MiB에서 `jq -s` OOM을 nonzero/non-timeout으로 구분 |
| G6 Gateway timeout | 완료 | 서버 timeout보다 먼저 발생한 Gateway timeout과 caller task cancel 모두 container 0개 확인 |
| G7 통합 거절 | 완료 | Registry→CandidateCompiler→Control→Gateway→실제 stdio에서 executable/shell/path/위험 옵션 거절; 다른 asset/run job 접근 거절 |
| G8 실행 이미지 | 완료 | digest-pinned Debian base, 고정 package version, non-root image 추가; Windows와 WSL에서 build 성공 |
| G9 WSL2 회귀 | 완료 | 최신 전체 suite `2162 passed, 42 skipped, 1 failed, 0 errors`; 기준선 대비 실패 증가 0 |
| G10 `artifact_ref` | 보류 | 공통 계약을 만들지 않고 A `write_file` 연계 가능 범위와 Evidence 협의 사항만 기록 |

## Agent 최소 연결 및 3A~3F 추가 보강

| 항목 | 상태 | 검증 근거 |
| --- | --- | --- |
| Agent 기본 비활성 | 완료 | 변경 전·후 동일 기존 묶음 각각 `146 passed, 1 deselected`; lease 단독 전·후 각각 `1 passed` |
| opt-in 후보 노출·스케줄 | 완료 | `execution_agent.enabled=true`와 graph-bound Asset recipe가 있을 때만 `command_run` 노출 |
| 고정 인자 바인딩 | 완료 | 모델 입력을 버리고 graph 사실에서 `wc -l <relative path>`와 context identity 재생성 |
| Agent→Control→Gateway→MCP→Evidence | 완료 | Windows fake Docker 경로와 WSL2 실제 Docker 경로 모두 통과; 실제 경로 `1 passed` |
| ROE·서버 거절 | 완료 | ROE 제외 시 Control 거절·Docker 미실행, 미허용 executable은 안정 오류 코드로 서버 거절 |
| 3A 큰 출력 파일 | 완료 | host가 `.command_outputs/<id>.txt`를 독점 생성하고 `output_path` 반환; WSL2 `6 passed` |
| 3B 취소·timeout 후손 정리 | 완료 | outer/child/grandchild를 `docker top`으로 확인한 뒤 두 경로 모두 label container 0 |
| 3C workdir/workspace 격리 | 완료 | `/workspace` 고정, 두 workspace 내용 분리, `../` 접근 거절 |
| 3D 환경 비누출 | 완료 | 전달 PATH/LANG 고정, Docker 생성 HOME/HOSTNAME 외 키 없음, host secret·민감 키 없음 |
| 3E 다른 run/asset 격리 | 완료 | 실제 Gateway의 job status/cancel 거절과 원 job 유지·정상 cleanup 확인 |
| 3F manifest 필드 | 완료 | Pydantic `output_schema` 고정 검증 및 discovery health check의 `health_status` 확인 |

이 연결은 정해진 Agent 응답으로 후보 노출, 스케줄, 정책 경유 실행과 Evidence 기록까지
증명한다. 실제 LLM이 자유 판단으로 후보를 선택하는지는 Agent 팀의 별도 종단 검증이
필요하다. 현재 bridge는 팀의 Agent workflow 재설계 때 교체할 수 있는 임시 최소 연결이다.

## 변경 파일

신규:

- `src/redteam/capability/servers/execution.py`
- `src/redteam/capability/execution_manifest.py`
- `src/redteam/capability/adapters/mcp/execution.py`
- `tests/unit/test_execution_mcp.py`
- `tests/unit/test_execution_manifest.py`
- `tests/integration/test_execution_mcp.py`
- `tests/unit/test_execution_agent_bridge.py`
- `tests/integration/test_execution_agent_e2e.py`
- `tests/integration/test_execution_output_files.py`
- `tests/integration/test_execution_runtime_boundaries.py`
- `tests/fakes/execution_fault_client.py`
- `deploy/mcp/execution/README.md`
- `deploy/mcp/execution/policy.example.json`
- `deploy/mcp/execution/agent-opt-in.example.json`
- `deploy/mcp/execution/read.env.example`
- `deploy/docker/execution-tools/Dockerfile`
- `deploy/docker/execution-tools/README.md`
- `docs/capability/basic-computer-tools-b-contract-proposal.md`
- `docs/capability/basic-computer-tools-b-protocol.md`
- `docs/capability/basic-computer-tools-b-result.md`
- `docs/capability/basic-computer-tools-b-gap-notes.md`
- `docs/capability/basic-computer-tools-b-orchestration-change-note.md`

Agent 최소 연결을 위해 수정·추가한 orchestration 파일:

| 파일 | 누적 diff | 내용 |
| --- | ---: | --- |
| `src/redteam/orchestration/execution_bridge.py` | `+209/-0` | opt-in schema, 도구 필터, graph-bound recipe·인자 |
| `src/redteam/orchestration/seed.py` | `+3/-0` | 검증한 Asset execution 사실 seed |
| `src/redteam/orchestration/engine.py` | `+50/-6` | context 필터와 execution 전용 조기 바인딩; 비-execution 검사 순서 보존 |

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

G 보완 후 Windows 담당자 B 선택 테스트:

```text
37 passed, 2 skipped
```

두 skip은 `REDTEAM_TEST_EXECUTION_DOCKER=1`이 필요한 WSL2 전용 테스트다. 변경한 B Python
파일 전체는 Ruff를 통과했다.

WSL2 + Docker 실제 opt-in 통합 테스트 전체:

```text
3 passed in 29.35s
```

G7 연속 경로 보강 후 실제 hardening test도 다시 `1 passed in 20.56s`로 통과했다.
WSL daemon의 로컬 image evidence digest는 다음과 같다. 이 값은 운영 policy나 저장소에
하드코딩하지 않았다.

```text
whs-redteam/execution-tools@sha256:638bb00b5e38cf55700b73e2ffc2edc62ef75e865eb8e297c3ccb631a0918ccb
python@sha256:9f6ef439f51f4b36dc5c6bb265c2d3dd810bde94c5bbec77ae86b6650c488cf9  # test-only outbound probe
```

최신 WSL2 전체 pytest:

```text
2162 passed, 42 skipped, 1 failed, 0 errors in 224.28s
```

직전 WSL2 기준선 `2146 passed, 37 skipped, 1 failed`와 비교하면 passed +16,
skipped +5, failed +0이다. 유일 실패는 기존
`test_agent_loads_reference_via_mcp_then_passes_data_to_web_llm[False]`이며 단독 재현됐다.
이 테스트는 `analysis-agent.schedulable is False`를 기대하지만 Track A 등록 상태는 이미
`True`다. 이번 B 변경은 Agent Registry를 수정하지 않았고 execution 관련 실패는 0이다.

전체 Ruff는 기존 `deploy/docker/apktool-mcp/entrypoint.py` import 정렬 2건 때문에 실패한다.
담당자 B가 변경한 Python 파일은 Ruff를 통과했다. 기존 파일은 수정하지 않았다.

## 계획서 5장 상태

| 항목 | 구현 | 단위/통합 검증 | 실환경 검증 | 미검증/제한 |
| --- | --- | --- | --- | --- |
| `run_command` | 완료 | 완료 | 완료 | 없음 |
| timeout 상한 | 완료 | 완료 | server/Gateway timeout 완료 | 없음 |
| stdout/stderr 상한·`truncated` | 완료 | 합산 buffer·파일 상한 완료 | stdout/stderr 초과와 host output file 완료 | 전체 MCP process RSS는 측정하지 않음 |
| task/연결 취소 cleanup | 완료 | mock 잔여 0 | caller cancel, stdio SIGKILL+restart 완료 | 죽은 SDK session 자체의 재사용은 지원하지 않음 |
| 상태 조회와 `job_id` | 완료 | ownership/TTL 완료 | start/status/cancel 완료 | 재시작 후 job 복구는 설계상 미지원 |
| Docker 격리 옵션 | 완료 | argv 검증 | 실제 container inspect 완료 | 없음 |
| `--network none` | 완료 | argv 검증 | inspect와 outbound 실패 완료 | 운영 allowlist에는 probe 도구 없음 |
| workspace 하나·기본 ro | 완료 | mount 검증 | ro execution + A write 흐름 완료 | 없음 |
| shell 금지·executable/option allowlist | 완료 | 오류 코드별 완료 | 전체 stdio 거절 완료 | 없음 |
| 환경변수 비누출 | 완료 | host env/Docker argv 검증 | container 내부 env dump 완료 | Docker 자체 HOME/HOSTNAME은 존재 |
| CPU·memory·PID 제한 | 완료 | argv 검증 | inspect, memory OOM 완료 | CPU throttling 부하 측정은 없음 |
| timeout/cancel 정리 | 완료 | 잔여 mock 0 | 매 fault 후 label container 0 | 없음 |
| structured 반환/오류 | 완료 | manifest와 `error_code` 완료 | 정상/timeout/거절 완료 | 없음 |
| 다른 run/asset job 접근 | 완료 | 완료 | 실제 Gateway 거절 완료 | 없음 |
| `artifact_ref` | 보류 | `output_path` 경계 완료 | Workspace `read_file` 재읽기 완료 | canonical Evidence artifact 계약 부재 |

## G10 보류 항목: `artifact_ref`

Execution 서버는 반환 buffer와 별도로 큰 출력을 승인된 workspace의 B 전용
`.command_outputs/<job-or-command-id>.txt`에 상한까지 저장하고 상대 `output_path`를
반환한다. 경로 이탈, symlink, 덮어쓰기, context 혼합을 차단하고 mode 0700/0600을
적용했다. A의 기존 `read_file`로 이 경로를 다시 읽는 흐름도 검증했다. 다만
`output_path`는 workspace 상대 경로일 뿐 canonical Evidence `artifact_ref`는 아니다.

Evidence 담당과 다음 계약을 합의하기 전에는 Execution MCP가 artifact를 직접 만들지 않는다.

- artifact의 engagement/run/asset 소유권과 다른 context 접근 차단
- 원본 byte 수, 저장 byte 수, SHA-256, media type, truncation/completeness 표현
- Execution MCP 직접 저장과 Agent→`write_file` 2단계 저장 중 신뢰 경계
- write action의 별도 Control 승인, 원자적 생성과 실패 시 정리
- Evidence/Attack Graph가 참조할 canonical ID와 보존·삭제 정책

## 계획서 12장 체크리스트

- [ ] 공통 계약 및 권한 기준 확정 — operation/ROE 팀 합의 대기
- [x] 명령 실행 환경 구현 및 등록
- [x] discovery와 manifest 일치 검증
- [x] 작업공간 및 실행 격리 검증
- [x] 정책 거절과 실패·취소·시간 초과 검증
- [x] 파일·명령 통합 시나리오 통과
- [ ] Agent 팀과 실제 LLM 자유 선택 검증 — 고정 응답 기반 후보·스케줄·실행은 완료
- [x] 연결 설정, 실행 방법, 제한사항 문서화
- [x] 기존 사용자 변경 보존 및 결과 commit 기록

담당자 A/C의 파일·웹검색 범위처럼 이번 작업에서 구현하지 않은 항목은 체크하지 않는다.

## 남은 합의와 후속 작업

- PM·Control·Agent·A·Evidence 팀이 네 canonical operation과 승인 정책을 확정해야 한다.
- 상태 조회 위험도를 MEDIUM으로 분리하려면 server/profile별 risk 표현 방식을 합의해야 한다.
- Agent의 실제 LLM 자유 선택과 job poll/cancel 전략은 Agent 팀 검증이 필요하다.
- 공통 artifact 계약 후 잘린 전체 출력의 `artifact_ref` 저장을 추가해야 한다.
- 전체 MCP process RSS와 CPU throttling 부하는 이번 라운드에서 측정하지 않았다. 출력
  retained buffer, CPU 설정값, host/Docker argv 및 container 내부 환경 경계는 검증했다.
- 운영 image는 내부 registry에 push한 뒤 registry가 발급한 RepoDigest를 operator policy에
  넣어야 한다. 로컬 build digest는 테스트 증거일 뿐 배포 식별자가 아니다.
- WSL 전체 회귀의 기존 Analysis Agent schedulable 기대 불일치는 해당 소유 팀 정리가 필요하다.

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
cd /path/to/WHS-Red-Teaming-Agent
docker build -t whs-redteam/execution-tools:local deploy/docker/execution-tools
export REDTEAM_TEST_EXECUTION_DOCKER=1
export REDTEAM_TEST_EXECUTION_IMAGE='whs-redteam/execution-tools@sha256:<local-repodigest>'
export REDTEAM_TEST_EXECUTION_PROCESS_IMAGE='python@sha256:<test-only-repodigest>'
python -m pytest tests/integration/test_execution_mcp.py -q
python -m pytest tests/integration/test_execution_agent_e2e.py -q
python -m pytest tests/integration/test_execution_output_files.py -q
python -m pytest tests/integration/test_execution_runtime_boundaries.py -q
python -m pytest -q --tb=no
```

실제 배포에서는 test-only Python image를 채택하지 않는다. text-tools image를 내부
registry에 게시·검토한 뒤 registry RepoDigest를 고정해야 한다.
