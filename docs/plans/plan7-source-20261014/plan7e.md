# 구현 프롬프트 7e — 실제 Kali 대상 실행과 전체 소비 경로 검증

`plan7.md`, `plan7a.md`~`plan7d.md`, 변경 모듈의 `AGENTS.md`를 읽어라. 앞 단계의 구현·검증을 대조하고 누락된 통합·표시·호환만 보완하라. 관련 코드가 이미 동작하면 불필요하게 다시 설계하지 않는다.

## 검증 수준과 권한

1. unit/scripted integration은 명시적인 double과 임시 DB로 수행한다. 이는 모델의 선택·전달 계약을 검증하며 실제 모델의 취약점 발견 능력을 증명하지 않는다.
2. Docker/Kali proof는 테스트가 소유한 소규모 local mock 대상·별도 Run·제한된 network window에서 수행한다. 기존 실제 조사와 Run·정책·원장을 변경하지 않는다. 기존 Docker daemon/context와 이미지 identity를 확인하고 계획 실행만으로 글로벌 context/VM kernel을 바꾸지 않는다.
3. 요청된 단계의 로컬 검증에 필요한 Docker 준비는 기존 선택/준비 경로를 사용한다. download/build/daemon 접근이 실행 환경 승인에 막히면 요구되는 구체적 권한을 설명한다. kernel 기능이 없으면 이미지 재빌드만 반복하지 말고 지원 환경이 필요하다는 원인을 보고한다. double을 실제 Kali로 바꿔 표현하지 않는다.
4. 유료 LLM, 사용자의 실제 대상 스캔·공격·기존 Run 재개는 별도 명시 요청 범위에서만 수행한다. 외부 계정·서비스나 보안 테스트 범위를 이 계획으로 추가하지 않는다.

## 실제 통합 proof

- 기존 `test_shell_workroom_docker.py`, `test_gated_window_docker.py`, `test_gated_install_docker.py`의 책임과 선행 조건을 확인한다. 여기서 install route는 이번 target 실행 완료의 필수 권한이 아니다. 가벼운 fixture 이미지의 성공만으로 실제 Kali image/program 준비까지 통과했다고 주장하지 않는다.
- 고정 digest/ID의 Kali image와 matching manifest에서 설치된 프로그램·사용 가능한 모드를 확인한다. 소유한 mock 서비스에 제한된 HTTP 또는 TCP 검증을 수행한다. 예를 들어 curl의 bounded 응답 수집이나 nmap connect 모드의 한정된 port 확인을 사용할 수 있다. 이 선택은 시험 입력이며 런타임에 고정 행동열로 넣지 않는다.
- mock 서버 측 수신 기록과 실제 gate/window settlement, 보호된 process result, 원본 ToolResult를 함께 확인한다. exit 0/버전 출력만으로 원격 요청 성공을 인정하지 않는다. 비허용 대상·port와 window 종료 후 추가 통신이 거절되는 것도 확인한다.
- scripted model이 실제 common worker/gateway를 통해 준비된 target shell을 호출하는 통합 경로를 검증한다. 모델은 fake여도 실행 provider는 실제 Kali여야 하며 구분해 보고한다. 허용/승인 대기/승인 후 원래 invocation 한 번 실행/결과 수집을 기존 실제 코드로 통과시킨다.
- 원본 출력·참조·truncation·gate 증거가 worker 재요청, Task 관측 반환, Planner 입력까지 도달하는지 확인한다. 이후 가설 제안과 후속 작업은 기존 split 판단·원자적 등록과 criterion ID 복원을 사용한다.
- 같은 instance 자원과 네트워크 창 충돌·timeout/stop·job 취소·원본 복구에서 영향 받는 성공/실패/UNKNOWN 경로만 검사한다. 테스트 후 소유한 fixture/container만 정리한다. 기존 quarantine·unknown ledger를 지우거나 환급하지 않는다.

## 표시와 호환

1. 기존 web/CLI 상태 투영을 재사용해 선택됨·준비 완료·local-only·target-ready·승인 대기·실제 실행·실패/미준비를 구분하라. readiness 확인에는 확인한 Docker endpoint·정책/이미지 identity와 시점이 연결돼야 한다. 별도 UI 상태 DB를 만들지 않는다.
2. Kali 사용 횟수는 실제 execution 기록과 provider metadata에서 계산하고 준비 검사·프로그램 조회·native HTTP 호출을 섞지 않는다. Planner/worker/review 요청 수와 unknown token usage도 원래 ledger를 사용한다. shell/server 이름을 별도 중앙 목록에 복제해 집계하지 않는다.
3. capability·네트워크 정책·route·sidecar·program manifest·프로필/프롬프트 의미 변경은 기존 선택 fingerprint에 반영한다. 동일 조건의 checkpoint/job 복구, 변경 조건의 resume 거절, 이전 형식 read/report를 확인한다.
4. 선택한 환경에서 실제 target route가 불가능하면 명시한 prerequisite와 지원 가능한 다음 선택을 표시한다. 다른 환경·미승인 endpoint·network-none을 성공한 target route처럼 대신 쓰지 않는다.

## 최종 보고와 종료

변경 파일과 이유, 실행한 최소 테스트, 실제 Docker/Kali 프로그램·도달성 proof, 실제 LLM 사용 여부, 남은 환경/정책 제약, 저장·resume 호환을 보고하라. 환경이 미준비라 physical proof가 없으면 구현 완료 범위와 미검증 범위를 구분하라. 라이브 모델이 Kali를 자율 선택했다거나 취약점을 찾았다는 사실을 scripted proof에서 추정하지 않는다.

소스 검사는 현재 `pyproject.toml`과 CI를 기준으로 필요한 Ruff/타입 검사와 관련 회귀만 수행하라. 필요한 검증이 통과하면 종료한다. 다음 계획·커밋·push·PR merge는 요청 없이 수행하지 않는다.
