# 구현 프롬프트 7 — Kali를 실제 대상 탐색에 연결하고 결과를 후보 피드백으로 전달

너는 이 프로젝트의 구현 담당자다. 루트와 변경 모듈의 `AGENTS.md`, 이 문서와 사용자가 지정한 하위 단계, 관련 현재 코드·설정·테스트를 읽어라. 지정된 단계의 구현·필요한 검증·계약 문서 갱신까지 완료하라. 다음 단계는 자동으로 시작하지 마라. 전체 구현을 요청받았을 때만 7a부터 7e까지 순서대로 진행하라.

이 문서 작성은 구현·정책 확대·실제 대상 실행 권한이 아니다. 기존 미커밋 변경, 환경 설정, Run·승인·Evidence·checkpoint를 보존하라. 계획과 구현 사실, 오프라인 검증과 실제 실행을 구분하라.

## 목표와 우선순위

최우선 목표는 공통 워커가 허용된 실제 대상에 대해 Kali의 프로그램을 실행하고, 원본 결과가 Planner의 후속 가설·짧은 검증 판단에 전달되는 것이다. 도구 등록, 이미지 준비, 모델에 목록 노출만으로 완료를 선언하지 마라. Kali를 모든 작업에 강제로 호출하는 것도 목표가 아니다. 정보와 목적에 맞는 탐색 수단으로 실제 선택·실행할 수 있어야 한다.

설정·네트워크·대상 연결을 먼저 해결한다. 이후 결과 전달과 완료 조건의 실제 장애를 해결하고 전체 경로를 검증한다. 기존 HTTP MCP를 삭제하거나 모든 프로그램을 별도 MCP 도구로 포장하지 않는다.

## 기준 사례와 현재 구현

기준 사례는 2026-10-14의 `run_a3947f4a3758`이다. Run ID·대상·파일명·도구명은 재현 근거이며 일반 런타임의 판단 조건이 아니다.

- 성공한 대상 도구 실행은 12회다. HTML inventory 4회, HTTP posture 4회, HTTP request 3회, HTTP probe 1회이며 shell 실행은 0회다. 도구 실행 횟수와 내부 HTTP 요청·네트워크 flow 수를 혼동하지 않는다.
- 모델 요청은 59회다. Planner 17회, 원자화 4회, 워커 36회, 작업 검토 2회다. 사용량은 unknown이며 토큰 0을 뜻하지 않는다.
- 작업 10개 중 관측 완료 1개, 미확정 8개, blocked 1개다. 후보 1개는 held이고 Finding은 0개다. 종료 사유는 `operator_stopped`다.
- 워커 문맥에 네트워크가 닫힌 `analysis.shell_run`과 local job 도구가 있었다. 대상 접근용 `analysis.shell_target_run`·`analysis.shell_target_job_start`는 제공되지 않았다. 당시 실제 Kali 컨테이너 대상 실행은 확인되지 않았다.
- Kali 자동 준비는 `shell_preparation.py`·`shell_runtime.py`에 이미 있다. 이미지·program manifest와 Docker endpoint를 묶는 실행 환경 fingerprint도 구현돼 있다.
- 별도 대상 네트워크 정책, gated descriptor, adapter/namespace 실행, sidecar, network ledger가 이미 있다. `REDTEAM_GATED_NETWORK_POLICY_PATH`와 `configure_gated` 조립을 재사용한다. 기존 local shell은 계속 network-none이다.
- 웹 조사 설정에는 이미 Kali의 closed/inherit/policy 선택과 정책 파일 검증·snapshot이 있다. 새 설정 화면을 중복 만들지 말고 실제 생산자→조립→소비자와 준비 과정의 누락을 고쳐라.
- `deploy/docker/gate-sidecar/README.md`에는 특정 Docker Desktop 커널에서 nft quota가 지원되지 않았던 측정 기록이 있다. 다른 Linux VM에서 synthetic proof가 통과한 기록도 있다. 과거 기록만으로 현재 환경의 가능·불가능을 단정하지 않는다.
- 두 후보 작업에 같은 후보의 `hypothesis_confirmed`와 `hypothesis_refuted`가 AND 완료 조건으로 들어갔다. 조건 설명의 자연어 `If`는 코드의 조건 분기가 아니다. 후속 원본 재사용에서는 purpose binding 거절이 반복됐다.
- JS 응답 본문이 생략되고 Swagger inline script가 제거된 관측도 있다. 이 계획의 우선 해결은 Kali의 허용된 요청·프로그램 출력 전달이며, 모든 native HTTP 응답 정책을 무제한으로 바꾸는 일이 아니다.
- 현재 완료 조건 ID 선택·원본 복원, 상세 원자화 진단 수정도 확인하라. 기본 `native-probes`는 작성 시점에 version 9, atomizer prompt는 version 3이다. 이미 해결된 원자화 본문 재작성 문제를 다시 구현하지 않는다.

## 단계

| 단계 | 파일 | 책임 |
|---|---|---|
| 7a | `plan7a.md` | 기존 Kali·gate 준비와 웹/CLI 선택을 실제 조립에 연결 |
| 7b | `plan7b.md` | Docker 환경별 대상 도달성·scope binding·네트워크 gate 검증 |
| 7c | `plan7c.md` | 공통 워커의 프로그램 선택·실행·원본 출력과 Planner 전달 |
| 7d | `plan7d.md` | 모순된 완료 조건·관측/후보 판정 경계·정당한 원본 재사용 수정 |
| 7e | `plan7e.md` | 실제 Kali와 소유한 로컬 대상의 통합 검증·상태 표시·호환 확인 |

각 단계는 선행 단계의 실제 동작을 확인하고 부족한 구현만 보완한다. 이는 구현 순서이며 에이전트가 조사할 순서를 고정하는 DAG가 아니다.

## 유지할 경계

1. 기존 registry/descriptor, 선택·readiness, ToolGateway, Control admission, network ledger, 실행 supervisor, Evidence Store, 중앙 transaction/outbox와 request ledger를 재사용한다. 새 Kali 실행 엔진·작업 큐·계획 DB·비용 원장을 만들지 않는다.
2. Capability는 실행 환경·프로그램·대상 route와 원본 실행 증명을, Control은 허용 범위·승인·예산을, Orchestration은 관측·작업·후보 의미를, App은 조립·입력·표시를 소유한다. 중앙 루프에 Kali/프로그램/랩 이름별 분기를 추가하지 않는다.
3. 사용자에게 허용 대상과 한도를 보여주고 명시한 선택만 준비·실행에 사용한다. HTTP 요청의 `full`은 Kali 네트워크 전체 허가가 아니다. 조사 시작 문구만으로 별도 네트워크 권한을 추가하지 않는다.
4. arbitrary bash의 TCP/UDP gate가 HTTP 메서드·path·header·session 제약까지 강제한다고 주장하지 않는다. 제공자별 정책과 공통 ROE를 구분한다. 공통 실행 제한을 강제할 수 없는 route는 실행 가능으로 표시하지 않는다.
5. 이미지·sidecar·Docker context·정책·대상 binding의 현재 identity를 검증한다. host network/privileged 실행, 모델 프로세스의 NET_ADMIN 부여, quota 삭제, scope 확대, 오래된 승인 재사용으로 연결 문제를 우회하지 않는다.
6. 실행 성공·프로그램 출력·대상 관측·후보·Finding·Mission 성공을 분리한다. shell exit 0이나 프로그램 버전 확인을 취약점 발견으로 처리하지 않는다.
7. 기존 Run 조회·보고는 유지한다. 새 계약/정책/fingerprint가 기존 Run과 다르면 기존 resume 검사로 명확히 거절한다. 과거 데이터를 수정하거나 실행 불명 작업을 새 ID로 반복하지 않는다.

## 최종 완료 기준

- 명시적으로 선택한 대상 네트워크 설정이 웹/CLI에서 같은 준비·조립 경로로 들어가고 현재 선택 상태를 정직하게 보여준다.
- 지원되는 Docker 환경에서 실제 Kali 프로그램이 허용된 소유 대상과 통신하고, 비허용 목적지·port와 취소/예산 경계가 실제 gate로 통제된다.
- 공통 워커의 원래 action/invocation으로 실행·원본 결과·gate 증거가 저장되며 Planner가 bounded 출력·참조·한계를 받는다.
- 관측 작업은 관측 조건으로 반환하고, 후보 확인/반증은 독립 검토와 정확한 revision/purpose를 거친다. 모순된 AND 조건을 등록하지 않는다.
- 준비/미준비/승인 대기/실제 실행/실행 불명/종료를 기존 상태에서 투영하고 Kali 사용 횟수는 실제 실행 원장으로 계산한다.
- 테스트·실제 Docker/Kali 실행·실제 LLM 판단·취약점 발견을 각각 구분해 보고한다. 필요한 문제가 해결되면 종료하며 커밋·push·다음 단계는 요청 없이 수행하지 않는다.
