# 구현 프롬프트 7b — Docker에서 허용 대상으로 도달하는 경로와 gate

`plan7.md`, `plan7a.md`, 루트·Core·Capability·Control·App의 `AGENTS.md`를 읽어라. 7a의 실제 준비 결과를 확인하고 이 단계의 대상 route·identity·gate 연결을 구현·검증하라.

## 먼저 확인할 코드와 계약

`gated_strategy.py`, `gated_window.py`, `gate_observer.py`, `gated_activation.py`, `gated_manifest.py`, `shell_workroom.py`, `core/contracts/target.py`, `core/contracts/network.py`, scope 규칙과 `control/network_ledger.py`를 읽어라. 기존 operator network policy 검증, target binding, Docker context 선택·adoption과 관련 테스트도 확인하라.

## 구현 요구

1. 기존 local `analysis.shell_run`은 network-none으로 유지한다. 대상 통신은 기존 `shell_target_run`·`shell_target_job_start`와 승인된 network window 경로로 연결한다. 기존 catalog/adapter/extension 등록을 재사용하고 tool 이름별 중앙 우회를 만들지 않는다.
2. 대상 위치를 명확히 하라. 호스트 loopback, 같은 Docker daemon의 대상 컨테이너, 일반 원격 주소, 원격 Docker daemon 뒤의 대상은 다른 연결 상황이다. `127.0.0.1`을 컨테이너에서 그대로 사용하거나 `host.docker.internal`로 무조건 치환하지 않는다. Docker endpoint와 대상의 실제 위치를 확인하고 사용자가 선택한 도달 가능한 주소를 검증한다.
3. 우선 기존 target/registry로 표현할 수 있는 실제 주소를 사용하라. 논리적 대상과 물리 접속 endpoint가 달라야 한다면 현재 계약의 표현 가능성을 먼저 확인한다. 불가능할 때만 operator-owned 최소 binding을 추가하고 생산자·Control 소비자·증거·fingerprint를 함께 연결한다. 주소 치환만으로 scope 허가를 만들지 않는다.
4. 명시한 route에는 논리 대상, 실제 IP·port, 연결 방식과 Docker endpoint identity가 연결돼야 한다. 원격 VM의 loopback이 사용자의 Mac loopback과 같다고 취급하지 않는다. host forwarding/relay가 필요하면 명시적인 operator 배포/선택으로 다루고, 목적지의 동일성을 확인할 수 없으면 unknown/미준비로 남긴다. 구현 중 임의 터널을 만들지 않는다.
5. registry pin 검증을 새 route와 일관되게 유지하라. 기존의 선택 host→resolved IP 일치 검사와 충돌하는 mapping을 validator 우회로 해결하지 않는다. 원래 승인 대상과 허용한 실제 route를 함께 검증하고 원래 승인 해시가 변경된 endpoint에도 재사용되지 않게 한다. DNS 결과 변경, redirect·추가 목적지 발견은 허가 확대가 아니다.
6. 기존 window의 target/port·flow admission·byte/시간 한도·namespace 격리·deny 우선·닫기/settlement를 유지하라. flow-init delivery, 패킷/바이트, 프로그램 호출, HTTP 요청은 다른 수치다. 통신량 한도를 요청 개수로 보고하지 않는다. 범용 프로그램 한 호출의 여러 연결도 기존 ledger에 누적돼야 한다.
7. Model 프로세스에 NET_ADMIN/CAP_NET_RAW를 부여하거나 host network/privileged로 바꾸지 않는다. image manifest가 선언하는 사용 가능한 프로그램 모드로 실행한다. 지원되지 않는 프로그램 모드는 누락/대안 정보를 반환하고 성공으로 숨기지 않는다.
8. timeout·stop·승인 취소·sidecar 실패·job terminal에서 gate 닫기와 원래 실행 식별자의 정합성을 확인하라. 보호된 실행 결과가 없으면 UNKNOWN을 보존하고 신규 ID 재실행이나 네트워크 quota 초기화로 복구하지 않는다. job restart도 기존 protected manifest/recovery를 사용한다.

## 최소 검증과 완료 조건

- unit/injected runner로 loopback 혼동, operator route 불일치, scope 밖 IP/port, 정책 변경 후 승인·resume 거절을 확인한다.
- 소유한 Docker mock의 TCP 응답을 통해 허용 route 도달성과 비허용 route 거절을 구분한다. 지원되는 환경에서 실제 패킷 검증은 7e와 연결하고, kernel 불지원으로 못 했으면 그 사실을 명시한다.
- 같은 instance의 창 충돌, shared budget, stop·timeout·gate teardown, UNKNOWN 유지와 기존 job 복구 시험 중 영향 받는 경로만 실행한다.
- 설정 화면·Planner/worker에는 실행 가능한 대상 참조와 readiness만 제공한다. 네트워크 grant/epoch/holder 값은 기존 trusted invocation에서 얻고 모델에게 생성시키지 않는다.

이 단계 완료는 대상 연결과 scope/gate 정합성이다. 취약점 발견으로 보고하지 말고 다음 단계는 자동 실행하지 마라.
