# Web 운영 준비와 장애 대응

2026-10-11. 현재 후속 범위는 Web/API·Recon이다. 실제 LLM 비교와 Mobile 신규 작업은
제외한다. 이 문서는 운영 승인이나 실환경 검증 완료 증명서가 아니다.

## 1. 설정 검사와 실행은 다르다

저장소 루트의 현재 venv에서 운영자가 작성한 engagement를 검사한다.

```sh
./.venv/bin/python -m redteam.app.cli validate engagement.yaml --json
```

이 명령은 기존 실행 설정 로더를 통해 Scope/ROE/MCP 설정, shell·gate 정책,
전문 역할 프로필과 MD, 공통 요청 정책, 선택한 offline fixture 설정을 확인한다.
Docker·MCP·LLM·대상을 호출하지 않고 grant·실행 기록을 만들지 않는다.
`valid:true`는 검사한 설정이 유효하다는 의미다. `live_readiness_checked:false`,
`admission_checked:false`이므로 서버 연결·이미지 존재·커널 기능·실행 허가를 뜻하지
않는다. `(disabled)`/`(closed)`로 표시된 기능은 실제 활성 도구가 아니다.

Web 전문 역할 선택은 기존 설정을 사용한다.

```sh
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/native-specialists-web.yaml"
export REDTEAM_LLM_REQUEST_POLICY_JSON='{"mode":"bounded","max_requests":100,"max_concurrent_requests":2}'
```

이 설정만으로 실제 실행이 준비되지는 않는다. MCP 공급자·모델·engagement는 따로
필요하며, 실제 LLM 검증은 이번 작업에서 실행하지 않는다. 모델 키를 Git/보고서에
넣지 않는다. 프로필이 달라진 과거 Run을 새 설정으로 강제 재개하지 않는다.

## 2. 네트워크 없는 shell부터, 설치/대상 egress는 별도

- `REDTEAM_SHELL_POLICY_PATH`: operator-owned JSON. shell 이미지 digest/로컬
  불변 ID와 자원·출력·실행 시간 한도. 기본 실행은 network-none이다.
- `REDTEAM_GATED_NETWORK_POLICY_PATH`: 별도 operator-owned JSON. 선택하지
  않으면 gate는 닫힌다. 선택하면 shell 정책과 아래 값이 필요하다.
- `REDTEAM_CODE_FIXTURE_POLICY_PATH`: 선택적 offline 검사 정책. 코드 작성/독립
  검토 역할과 pinned 이미지가 있어야 한다. 실제 대상 코드 실행 권한이 아니다.

### Gate 정책의 필수 운영 입력

| 입력 | 확인할 사항 |
| --- | --- |
| `sidecar_image` | 운영자가 검토한 `repo@sha256:...` 또는 로컬 `sha256:...`. 태그/누락은 거절 |
| `budget` | global/target/install capacity 각각 명시. 비활성 목적은 0 capacity 유지 |
| `default_target_caps`, `default_install_caps` | 목적별 deliveries/bytes/duration을 별도로 결정. 큰 수를 자동 기본값으로 쓰지 않음 |
| `registry.targets`, `registry.mirrors` | 승인된 이름과 canonical IP pin. 두 경로는 IP가 겹치면 안 됨 |
| `install_proxy` | endpoint, pinned_hosts, mirror_upstream. credential-free HTTP(S) origin과 canonical IP pin |
| engagement Scope/ROE/approval | 대상·포트·mirror·상위 origin·실제 pin의 허용/거절과 승인 조건을 기존 Control에서 확인 |

프록시 URL은 host와 유효한 포트만 갖는 origin이다. 경로·query·fragment·userinfo·
공백/제어문자가 들어 있으면 거절한다. proxy pin과 target pin의 충돌도 정책 로딩에서
거절한다. 이것은 외부 DNS 해석이나 자동 scope 추가가 아니다. 외부 IPv6 egress는
현재도 fail-closed이다. private registry 인증을 URL에 넣는 지원은 추가하지 않았다.

기존 gate 파일에는 `sidecar_image`가 없을 수 있다. **실제 이미지 ID를 조회해 운영자가
필드를 추가해야 한다.** 임의 태그 fallback이나 자동 이미지 다운로드는 없다.
이미지 pin은 불변성이고 신뢰성 인증은 아니다. 빌드 출처·base digest·패키지 출처/
서명·보관 책임은 운영자가 별도로 확인해야 한다.

명시적으로 선택한 Docker daemon에서 로컬 이미지를 읽기만 하는 예:

```sh
docker image inspect --format '{{.Id}}' <운영자가_선택한_로컬_sidecar_이미지>
```

실행 시 provider는 pinned 이미지의 로컬 존재/응답을 **grant 예약·workroom 생성 전에**
확인한다. 실패하면 `gated_sidecar_image_required/unpinned/unavailable`로 거절한다.
컨테이너 생성에도 `--pull=never`를 유지한다. 이미지 조회를 kernel quota/NFQUEUE
검증으로 대체하지 않는다. Kernel·규칙 검사와 합성 프록시 시험은
[gate 안내](../../deploy/docker/gate-sidecar/README.md)를 따른다.

### 이번 환경의 상태

운영 mirror/proxy·이미지 신뢰·INSTALL/TARGET 한도는 아직 제공되지 않았다.
따라서 실운영 정책을 생성하거나 외부 패키지/대상 요청을 수행하지 않는다.
별도 Lima VM은 로컬 합성 경로 검증용이며 Mac CLI의 Docker daemon을 자동 전환하지
않는다. VM 안의 bind source와 venv는 VM 로컬 경로를 사용한다.

## 3. 복구와 작업 인계의 안전한 사용 범위

먼저 기존 저장소를 읽기 전용으로 조회한다. 아래 경로는 해당 Run이 실제 사용한
파일이어야 하며, 다른 checkout의 파일이나 새 빈 DB로 대체하면 안 된다.

```sh
./.venv/bin/python -m redteam.app.cli status <RUN_ID> \
  --activation-db <기존_coordination_DB> --work-db <기존_Work_DB> \
  --evidence-dir <기존_Evidence_디렉터리> --json
```

| 상태 | 가능한 처리 | 하면 안 되는 처리 |
| --- | --- | --- |
| 승인 대기 + 일치하는 checkpoint | 현재 승인 ID 확인 후 원래 Run/instance/action으로 기존 승인·재개 경로 사용 | 과거 화면 ID, 변경된 인자·대상에 승인 재사용 |
| 정확한 불변 append/원자 Graph mutation proof | 기존 reconciliation에서 저장 영수증 확인 후 처리 | 현재 값이 같다는 이유로 재적용/완료 추정 |
| 미진입 예약 + 현재 효과 부재 증명 | 기존 zero-checkpoint 검증이 허용하는 원래 작업만 복구 | TTL·초기 카운터·빈 목록만으로 미진입 추정 |
| entered + checkpoint 없음 | 명시적 `collect-originals RUN_ID engagement.yaml --json`로 지원 provider의 기존 결과만 보존 | 결과 수집 성공을 Task 완료·재실행·점유 해제 권한으로 사용 |
| 원본 없음/unknown/상충/구형 proof 없는 writer·orphan | `held` 유지. 진단과 원본을 보존하고 원래 provider/저장소를 확인 | 실패 위장, 비용 환불, DB 초기화, 자동 도구 재호출 |
| canonical 종료 Task의 잔여 claim | 기존 `InstanceDriver.release_instance_claim`의 소유권·동일 Run·quiescence 검사 후 종료 점유만 정리 | Work 자원 marker/LLM 비용 정리, 닫힌 Run 부활 |
| 미완료 Task의 다른 워커 인계 | 현재 미지원. `task_claim_handoff_unverified`로 거절 | Runtime `finished` 또는 GUIDANCE/RESUME를 인계 승인으로 해석 |

`collect-originals`는 provider의 기존 결과 조회를 할 수 있으므로 실행 없는 `status`와
다르다. 이번 검증에서는 합성 provider만 사용하며 실제 설치된 ZAP/Burp가 모두
recovery를 지원한다고 주장하지 않는다. `resume`은 실제 모델/도구를 실행할 수 있으므로
운영 준비 점검용으로 무조건 실행하지 않는다.

### 미완료 handoff를 열기 전에 확정할 계약

1. 사람의 명시적 인계 intent: Run/Task/기존 owner/수신 역할과 현재 개정에 귀속.
2. 최신 native context와 처리 상태·공유 요청 원장/미정산 Work inventory의 복구 증명.
3. 원래 actor 재개 차단과 canonical claim 변경을 하나의 원자적 채택 과정으로 연결.
4. 남은 승인·resource hold·unknown 비용 보존, 중복/취소/재시작의 거절·멱등성 검증.

이번 요청을 개별 미완료 Run의 인계 허가나 이 계약의 운영 확정으로 해석하지 않는다.
지원되지 않는 상태는 안전한 보류로 처리하며 모든 crash의 자동 복구 완료를 주장하지
않는다. 이는 PRD G8의 “복구 또는 명확한 보류”와 일치한다.

## 4. 인계 전 비용 없는 검사

```sh
umask 077
./.venv/bin/pytest -q tests/unit/test_cli_execution_preflight.py \
  tests/unit/test_operator_network_policy.py tests/unit/test_gated_image_selection.py \
  tests/integration/test_instance_claim_release_guard.py \
  tests/integration/test_original_collection_cli.py
./.venv/bin/ruff check src tests
./.venv/bin/mypy src/redteam tests
```

실제 Docker gate 시험은 승인된 로컬 이미지/지원 커널이 있는 환경에서 별도로 실행한다.
이미지 부재 skip·정적 설정 통과·합성 도구/모델 통과를 live 성공으로 세지 않는다.
이번 실제 검사 결과와 수정 이유는 [구현 기록](dev-first-specialists-implementation-20261010.md)
20절에 기록한다. 유료 LLM 호출과 GitHub push/PR은 이번 후속 작업에 포함하지 않는다.
