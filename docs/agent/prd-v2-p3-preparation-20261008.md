# PRD v2 P3 — Mobile 연결 준비와 안전 보강

작성: 2026-10-08. 대상은 `WHS-Red-Teaming-Agent-dev-orchestration-p0-20261006`
checkout의 `dev-jm` 로컬 변경이다. 이번 작업에서 GitHub 갱신·commit·push·PR은
하지 않았다. 기존 P0/P1/P2 변경, 개인 `.env`, 실행 launcher와 Run 기록을 보존했다.

## 1. 판정과 작업 경계

**P3 전체 완료가 아니라 Mobile 연결 준비·합성 통합 검증까지 진행한 상태다.**

사용자가 최종적으로 기기 serial/참조, 앱 package/APK, Mobile MCP 설정·정책 경로가
없다고 확인했다. 생성 코드의 실제 대상 실행 규격도 미확정이며 비활성 유지가
명시됐다. 따라서 실제 기기·금융 사이트·LLM·생성 코드 대상 실행은 수행하지 않았다.
합성 실행으로 실제 앱 접속이나 침투 성공을 주장하지 않는다.

| P3 항목 | 이번 결과 | 남은 조건 |
| --- | --- | --- |
| Mobile 환경 | 운영자 설정 생성기와 실제 stdio 연결 경로를 합성 검증 | 승인 기기·앱 및 실제 설치/연결 검증 |
| Mobile 실행 역할 | 기존 P2 Mobile 역할·catalog·Control 재사용 | 실제 앱 목적 위임과 원본 결과 평가 |
| Web 브라우저/입력 업무 확장 | 이번에는 기존 경로를 유지 | 승인 브라우저·입력·합성 업무 환경에서 별도 검증 |
| 생성 코드 대상 실행 | 비활성 유지, 기존 네트워크 없는 fixture만 유지 | operation·실행 대상/경로·한도·격리·artifact 승인 계약 확정 |
| 운영 평가 | 추가 모델 호출 없음 | P2 실제 모델 품질 게이트와 P3 운영 시나리오 검증 |
| 다중 MissionTask·승인 큐 | 이번 작업에 추가하지 않음 | 별도 계약과 충돌/복구 시험 |

## 2. 직접 확인한 문제와 수정

### 2.1 설정은 연결돼도 실제 기기 대상이 없을 수 있다

기존 seed는 승인 asset의 `device_refs`, `device_serials`, `package_names`를
DeviceTarget으로 소비한다. MCP의 발견·health만으로 이 값이나 설치 앱이 생기지는
않는다. 필요한 정보를 모델이나 Finance Lab의 정답에서 만들지 않는다.

`capability/mobile_deployment.py`가 기존 `EngagementConfig`, seed metadata,
MCP stdio 설정과 deployment 해시/직렬화를 재사용한다.

- 운영자가 승인한 기존 asset에 기기 참조·serial·package를 연결한다.
- scope 밖·deny asset, 모호한 serial/asset 중복, 기존 metadata 충돌을 거절한다.
- 현재 Python 실행 경로와 policy SHA256을 환경 설정에 바인딩한다.
- 새 private 디렉터리/파일만 만들고 기존 파일을 덮어쓰지 않는다.
- scope·ROE·승인·예산·기본 역할은 변경하지 않는다.
- `operator_declared_not_device_or_app_verified`로 선언과 검증을 구분한다.

서버 선택은 spec의 `additional_server_ids`와 새 서버만으로 명시한다.
기존 Web/Recon 환경 설정을 자동 수집하거나 복제하지 않는다.

### 2.2 다른 기기 인자가 대상 검증을 통과하던 공백

DeviceTarget은 네트워크 주소가 없으므로 기존 네트워크 인자 검사에서 바로
돌아갔다. 알려진 기기와 다른 `device_ref`, serial, package 또는 원격 별칭을
넣은 8개 경우가 수정 전 거절되지 않는 것을 테스트로 재현했다.

`orchestration/target_binding.py`의 기존 검증 진입점에서 DeviceTarget의
알려진 값과 명시 인자를 비교한다. compiler와 행동 검토가 같은 검사를 사용한다.
`candidates/compiler.py`는 provider가 선언한 serial 별칭을 기본 device_ref로
덮어쓰지 않고 알려진 serial을 바인딩한다. 누락된 필수 인자는 기존 schema 검사가
처리한다. 모델이 임의 다른 기기를 지정하면 실행 후보로 받아들이지 않는다.

중앙 루프·Control에 특정 도구/서버/역할/랩 이름 분기를 추가하지 않았다.
외부 코드가 Agent 검증 없이 직접 Gateway를 호출하는 것은 같은 보장을 받지 않는다.
서버의 자체 기기 allowlist도 유지해야 한다.

### 2.3 대상 종류와 별칭이 MCP 발견에서 전달되지 않았다

공통 adapter가 `_meta`의 `redteam.target_types`, `redteam.argument_bindings`를
기존 ToolDescriptor로 전달하도록 추가했다. 새 두 키의 구조가 잘못되면 등록 전에
실패한다. 기존 command fields/URL binding을 재사용하며 새 DTO·등록소는 없다.

Mobile 진단 서버는 device 대상, device_ref 인자 바인딩, command 필드를
자신의 metadata로 선언한다. 따라서 APK asset만 있는 경우를 기기 진단 대상처럼
광범위하게 노출하지 않는다. 최종 manifest 검증·운영자 설정·역할 선택과 Control은
여전히 필요하다. metadata는 권한이 아니다.

### 2.4 ADB 출력 제한이 실질적인 메모리 제한이 아니었다

기존 `communicate()`는 전체 stdout을 메모리에 읽고 나중에 잘랐다.
출력 제한도 문자 수 기준이어서 한글 등 UTF-8 다중 바이트 출력은 설정 바이트
제한을 초과했다. 두 문제는 수정 전 실패 테스트로 확인했다.

`capability/servers/mobile.py`는 출력 prefix와 overflow 1바이트만 유지하고
나머지는 버리며 읽는다. timeout은 출력 drain과 프로세스 종료를 포함한다.
취소/시간 초과 시 자신이 생성한 자식만 종료·회수한다. 잘못된 명령·기기,
실행 파일 부재·비정상 종료는 성공으로 바꾸지 않는다.

정책은 최대 64 KiB와 선택적인 SHA256을 검사한다. 새 생성 설정은 항상
SHA256을 사용하며 기존 CLI의 hash 미지정 사용은 호환을 위해 유지한다.
단일 ADB 호출은 고정 argv를 사용하고 shell 문자열이나 임의 payload를 받지 않는다.
생성기 기본 명령은 `getprop`, `dumpsys_battery` 두 개다. 진단에는 장치/로그 정보가
포함될 수 있으며 바이트 제한을 개인정보 필터로 간주하지 않는다.

## 3. 실제 소비 경로

```text
운영자 spec + 승인 engagement
  → Mobile 설정 생성 (선언/해시, 권한 확대 없음)
  → 기존 seed → 알려진 DeviceTarget
  → 기존 역할 registry / ToolCatalog ← stdio discovery + provider metadata
  → Planner → Atomizer → Mobile 실행 역할의 행동 제안
  → compiler / 인자·대상 검증 → Control
  → 기존 ToolGateway → MCP adapter → 고정 ADB 진단
  → 원본 ToolResult → 기존 evidence / graph 처리
```

앱 package는 설치 대상 선언에 사용하지만 이번 ADB 진단은 앱을 실행하거나
설치 여부를 검증하지 않는다. 진단 성공·원본 기록·Finding·목표 완료는 별개다.
신규 Mobile 역할/프로필, 범용 shell, `code.execute`, 실제 거래는 추가하지 않았다.

## 4. 검증 기록

- 관련 시험: **76 passed / 1 skipped**. skip은 실제 ADB 기기 opt-in 시험이다.
- 기존 Agent/MCP 바인딩·Mobile runtime 합성 연결·P1 전체 연결을 별도 재시험한
  묶음도 **34 passed**였다. 전체 회귀와 중복되는 시험이므로 합산하지 않는다.
- 전체 Ruff 검사 통과. Mypy **611 source files**, 오류 0; untyped 함수 body를
  검사하지 않는 기존 설정의 note는 남아 있으므로 완전한 타입 증명은 아니다.
- 전체 회귀 시험: **3,098 passed / 61 skipped**, 294.45초. 실제 기기/외부 공급자/
  Docker 실행 opt-in을 비활성하고 합성 경로로 실행했다. skip을 실제 검증 완료로
  계산하지 않는다. 이번 변경 전 기록 3,055 passed에서 통과 테스트가 43개 늘었다.
- 생성기/서버 CLI help와 `git diff --check` 통과. 미입력 예제의 기기·앱
  자리표시자는 schema가 거절하는 것을 직접 확인했다.

주요 테스트와 판정 근거:

| 테스트 | 확인 내용 |
| --- | --- |
| `test_mobile_deployment.py` | 현재 runtime, 정책 hash, private 파일, scope/deny·중복·충돌·변조 거절 |
| `test_mobile_server.py` | 고정 명령, UTF-8 바이트 제한, 큰 합성 출력, timeout·취소·자식 정리 |
| `test_device_target_binding.py` | 명시/별칭의 다른 기기·앱 거절, known serial 바인딩 |
| `test_mcp_adapter.py` | 제품 이름 분기 없는 metadata 투영과 malformed 입력 거절 |
| `test_specialist_p3_mobile.py` | 기존 역할→Planner/Atomizer→Control→실제 stdio MCP→원본 증거 경로 |

통합 시험의 모델과 ADB는 명시적인 fixture다. 실제 subprocess/MCP protocol을
사용하지만 Android 기기나 앱에 접속하지 않는다. ALLOW에서 합성 ADB argv 호출
1회와 원본 결과 보존을 확인했다. REQUIRE_APPROVAL, DENY, 다른 기기 제안은
대상 호출 0회이며 합성 성공으로 Finding을 만들지 않았다. 승인 대기의 후속
재개나 실제 앱 평가 완료를 이 시험으로 주장하지 않는다.

## 5. 다음 단계와 수락 기준

1. 승인 기기 serial/참조, 앱 package/APK와 asset, MCP 서버·정책을 확보한다.
2. [연결 안내](../../deploy/mcp/mobile-diagnostics/README.md)에 따라 별도 설정을
   생성하고 discovery와 기기/설치 앱 확인을 **따로** 수행한다.
3. 승인된 read-only 진단을 기존 Mobile 역할로 실행하여 원본 결과·대상 바인딩,
   승인/거절·중단·재개와 증거/graph 연결을 실제 환경에서 확인한다.
4. 필요 앱 launch/UI/log/분석 도구는 해당 provider의 schema와 알려진 대상
   metadata로 추가하고 실제 환경에서 검증한다. 진단 서버를 범용 shell로 바꾸지 않는다.
5. Web 브라우저/입력 확장과 운영 평가를 별도로 진행한다. 실제 코드 대상 실행은
   공통 실행 규격·격리·해시 승인·한도·결과 평가가 확정/검증될 때까지 닫아둔다.

P2 실제 모델 비교는 이전 누적 81/100회·공급자 429 상태 그대로다. 이번에는 추가
모델 호출이나 비용 발생 실행을 하지 않았고, P3 준비를 P2 운영 품질 통과로 해석하지 않는다.
