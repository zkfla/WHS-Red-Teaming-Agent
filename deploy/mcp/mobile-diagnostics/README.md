# Mobile 진단 MCP 연결 준비

실제 기기·앱 정보가 없으면 활성화하지 않는다. 예제의 `<...>`는 의도적으로
검증을 통과하지 않는 자리표시자다. 값을 모델이 추측하거나 시험 앱 정답으로
자동 생성하지 않는다. 이 서버는 Android ADB 읽기 진단만 제공한다.
iOS, 앱 설치·로그인·UI 입력·instrumentation·생성 코드 실행은 제공하지 않는다.

## 운영자가 정할 값

- 승인된 기기 serial, 기기 참조, Android package와 대응하는 기존 asset 참조.
- engagement의 `scope.allowed_assets`에 선언된 승인 앱 asset. deny가 우선한다.
- 허용할 고정 진단 명령, 현재 환경의 ADB 실행 파일 위치, 시간·출력 한도.
- 함께 유지할 기존 MCP 서버 ID와 각각의 기존 연결 설정.

`deployment.example.json`을 별도 로컬 설정으로 복사해 수정한다. 기본 명령은
`getprop`, `dumpsys_battery` 두 개다. 추가로 지원되는 명령은
`pm_list_packages`, `logcat_recent`이며 명시적으로 허용해야 한다.
진단 출력에 기기·앱 정보나 로그의 민감정보가 포함될 수 있으므로 시험 기기를
사용하고 출력 보관 정책을 확인한다. 출력 제한이 개인정보 필터를 대신하지 않는다.

## 설정 생성

프로젝트 루트와 그 가상환경에서 실행한다. 아래 `/absolute/...`는 운영자가
선택한 로컬 경로로 바꾼다. 출력 경로는 **새 디렉터리**이며 부모는 미리 존재해야 한다.

```bash
./.venv/bin/python -m redteam.capability.mobile_deployment \
  --spec /absolute/mobile-deployment.json \
  --engagement /absolute/approved-engagement.yaml \
  --output /absolute/new-mobile-runtime
```

현재 Python 경로와 policy SHA256을 가진 `framework.env`, `policy.json`,
`engagement.yaml`, 원본 선언 `deployment.json`을 생성한다. 기존 파일을
덮어쓰지 않으며 scope·ROE·승인·예산을 확대하지 않는다. 기기와 앱 metadata는
**운영자 선언**이고 연결·설치 검증 결과가 아니다. 정책 파일 변경은 시작 시 거부한다.
의도적으로 변경할 때는 새 출력 경로에 재생성한다. 기존 Run의 resume 설정을
이 파일로 몰래 대체하지 않고 새 Run에서 사용한다.

## 준비된 환경에서 discovery / 실행

```bash
# 함께 사용할 기존 서버 설정을 먼저 로드한다. 비밀값은 출력하지 않는다.
source /absolute/new-mobile-runtime/framework.env
export REDTEAM_EXTERNAL_MCP_SERVERS="$REDTEAM_MCP_SERVERS"
./.venv/bin/python examples/external_mcp_discovery.py
```

생성된 `REDTEAM_MCP_SERVERS`는 `additional_server_ids`와 이번 서버만 포함한다.
기존 Web/Recon을 유지하려면 해당 ID를 spec에 명시하고 그 연결 설정도 먼저
로드해야 한다. 외부 discovery가 성공해도 실제 ADB나 설치 앱을 확인한 것은 아니다.

```bash
export REDTEAM_SPECIALIST_PROFILE=config/agents/specialists-p2.yaml
./.venv/bin/python -m redteam.app.cli run /absolute/new-mobile-runtime/engagement.yaml
```

기존 Mobile 역할을 재사용하며 새 역할·operation·범용 shell을 만들지 않는다.
Agent가 알려진 기기와 다른 serial/package/ref를 제안하면 거절한다.
Control 승인이 필요하면 확인된 요청만 승인하며 scope나 정책을 풀지 않는다.
기기 진단 성공은 앱 취약점·로그인 성공·목표 달성이 아니다.

## 실제 기기 없이 가능한 검증

```bash
./.venv/bin/pytest -q tests/unit/test_mobile_deployment.py \
  tests/unit/test_mobile_server.py tests/unit/test_device_target_binding.py \
  tests/integration/test_specialist_p3_mobile.py
```

통합 시험은 fixture 모델과 합성 ADB 프로그램을 사용하지만 stdio MCP 프로세스,
Registry, Agent의 계획/작업 분해, Control, Gateway와 원본 증거 저장은 실제 경로다.
허용 시 합성 호출 1회, 미승인·거절·다른 기기 제안 시 호출 0회를 확인한다.
실제 Android/Mobile 앱 실행 또는 실제 LLM 품질 평가와 구분한다.
