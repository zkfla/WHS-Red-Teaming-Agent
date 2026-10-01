# Execution Substrate 계약 제안

상태: **합의 필요(제안)**  
담당: Capability 담당자 B  
근거: `basic-computer-tools-team-plan.md` 3장, 5장, 7장, 8장

이 문서는 구현을 위한 Capability 측 제안이다. Core·Control 계약을 승인하거나
`command_run`을 운영 환경에서 허용한다는 뜻이 아니다.

## 계약 제안

| 항목 | 제안 | 근거와 제한 |
| --- | --- | --- |
| capability | 기존 `analysis` 재사용 | 새 capability 분류 없이 작업 자산을 제한적으로 가공한다. `static_analysis`로 포장하지 않는다. |
| 동기 operation | `command_run` | 제품명이나 실행 파일명과 분리된 실행 의도다. 합의 전까지 제안 상태다. |
| 비동기 operation | `command_job_start`, `command_job_status`, `command_job_cancel` | 시작·조회·취소 권한을 분리한다. 모두 합의 필요 상태다. |
| target | 기존 `AssetTarget` | `asset_id`를 MCP의 `asset_ref`에 정확히 바인딩한다. 호스트 전체를 대상으로 하지 않는다. |
| 작업공간 | operator가 승인한 `asset_ref` 하나 | 실행 컨테이너에는 이 작업공간만 `/workspace`로 마운트한다. 기본은 읽기 전용이다. |
| 실행별 폴더 | `runs/<run_id>` 아래 상대 경로 | 실행 주체가 다른 run의 폴더를 선택하지 못하게 서버의 신뢰 컨텍스트로 고정한다. 초기 구현은 작업공간 루트를 working directory로 사용하고 경로 인자를 상대 경로로 제한한다. |
| 실행 파일 | 정책의 정확한 allowlist | `grep`, `sort`, `uniq`, `wc`, `head`, `tail`, `cut`, `jq` 등 텍스트 가공 도구부터 허용한다. shell과 임의 코드 실행기는 제외한다. |
| 필수 입력 | `asset_ref`, `executable`, `args` | `asset_ref`는 operator 설정의 상수이며 adapter가 target과 일치시킨다. |
| 선택 입력 | `timeout_seconds` | 정책 상한 이내의 양수만 허용한다. |
| 신뢰 입력 | `engagement_id`, `run_id` | Agent 입력이 아니라 `ExecutionContext`에서 adapter가 주입한다. |
| 동기 출력 | `exit_code`, `stdout`, `stderr`, `timed_out`, `truncated`, `duration_ms` | 실패·시간 초과·잘림을 서로 구별한다. |
| 비동기 출력 | 시작 시 `job_id`; 조회 시 상태와 완료 결과; 취소 시 취소 상태 | job은 asset·engagement·run에 귀속되고 다른 컨텍스트에서는 조회·취소할 수 없다. |
| 위험도 | 실행·시작·취소 HIGH, 상태 조회 MEDIUM | 실행과 취소는 부작용 가능성이 있다. 배포 ROE는 명시적으로 승인해야 한다. |
| 권한 | 읽기·실행 분리, 쓰기는 정책 opt-in, 외부접속 금지 | 기본 mount는 `:ro`, 네트워크는 `none`이다. 쓰기 허용 시에도 같은 asset만 `:rw`로 마운트한다. |
| 승인 | 기본 `REQUIRE_APPROVAL` 제안 | Control 정책과 Core 계약은 변경하지 않는다. 운영자가 해당 operation/risk를 명시적으로 승인해야 한다. |
| 큰 결과 | 상한 적용 후 `truncated=true` | 공통 `artifact_ref` 계약이 없으므로 이번 범위에서 생성하지 않는다. |
| 격리 | Docker 필수 | Docker가 없으면 서버 시작을 거절하며 호스트 실행으로 대체하지 않는다. |

## 계층별 책임

- Agent: 제안 operation과 `AssetTarget`을 선택하되 컨테이너·경로·신뢰 컨텍스트를 만들지 않는다.
- Control: `analysis.<operation>`과 Registry 위험도를 평가하고 ALLOW 전에는 실행을 만들지 않는다.
- Capability adapter: `asset_id`와 `asset_ref`를 대조하고 engagement/run을 주입한다.
- Execution MCP: allowlist·경로·Docker 옵션·시간·출력·job 소유권을 fail-closed로 집행한다.
- Evidence: 성공·실패·시간 초과·취소·잘림을 구별해 보존한다. `artifact_ref`는 후속 계약 대상이다.

## 합의 요청 메시지 초안

### PM

> Execution Substrate를 기존 `analysis` capability와 `AssetTarget`으로 표현하고,
> `command_run`, `command_job_start/status/cancel`을 새 canonical operation으로 제안합니다.
> 새 capability나 Core target은 만들지 않습니다. 이 분류와 operation 명칭을 제품 계약으로
> 채택할지 검토해 주세요.

### Control 담당자

> 실행·시작·취소는 HIGH, 상태 조회는 MEDIUM으로 제안합니다. 합의 전에는 ROE에서 허용하지
> 않고, 실행 계열은 기본 REQUIRE_APPROVAL이 되도록 배포 정책에서 명시하려 합니다.
> 기존 Control 계약 변경 없이 이 위험도/승인 경계가 충분한지 검토해 주세요.

### Agent 담당자

> Agent 후보에는 `analysis.command_*`와 `AssetTarget`만 노출하고 `asset_ref`,
> engagement/run은 신뢰 컨텍스트에서 바인딩합니다. 도구 이름이나 Docker 세부사항을
> 계획에 넣지 않는 방식과 비동기 상태 조회 후보 노출 방식을 검토해 주세요.

### Capability 담당자 A

> 기존 workspace profile 패턴을 따라 execution profile, manifest, scoped adapter를
> 추가하려 합니다. `settings.py`, `bootstrap.py`, `action_naming.py`, actions 문서에는
> 기존 줄을 건드리지 않고 execution 분기와 operation만 추가합니다. workspace와 같은
> AssetTarget 바인딩 및 Registry 검증 경계를 유지하는지 검토해 주세요.

### Evidence 담당자

> ToolResult에서 정상 종료, 비정상 exit code, timeout, cancel, output truncation을
> 구조화해 구별합니다. 공통 artifact 계약이 없어 큰 출력은 상한과 `truncated`만 남깁니다.
> 후속 `artifact_ref` 계약에 필요한 필드와 보존 요건을 알려 주세요.

## 아직 합의되지 않은 항목

- canonical operation 네 이름과 위험도
- 쓰기 mount를 허용하는 별도 운영 정책의 승인 주체
- 완료 job 보존 시간과 최대 job 수의 운영 기본값
- 잘린 출력의 공통 `artifact_ref` 보관 계약
- Agent가 비동기 상태 조회를 자동 계획할 조건
