# MCP 도구의 공유 자원 충돌 계약

이 계약은 도구를 허용하는 정책이 아니다. 기존 Control/ROE, 역할 선택,
입력 검증과 실행 한도를 통과한 요청끼리 같은 물리 자원을 동시에 변경하지 않도록 한다.
기존 WorkStore의 conflict/lease를 사용하며 별도 실행기·lock 서비스·작업 큐를 만들지 않는다.

## 1. 제공자가 자원을 명시한다

MCP discovery의 tool metadata에 `redteam.resource_bindings`를 선언한다.
Registry의 `ToolDescriptor.resource_bindings`로 전달되고, 컴파일러가 입력과 schema를 검사한다.

```json
{
  "redteam.resource_bindings": [
    {"namespace": "browser.profile", "argument": "profile_ref"}
  ]
}
```

위 이름은 설정 예시다. 저장소의 모든 브라우저 MCP가 이미 이 인자를 제공한다는 뜻은 아니다.
`argument`는 실제 discovery input schema의 최상위 property여야 하며 값은 비어 있지 않은 문자열이다.
객체·숫자·누락 값을 임의로 자원 이름으로 바꾸지 않는다. 중첩 JSON pointer는 이번 계약에 없다.

도구가 항상 한 고정 프로필을 쓰면 `argument` 대신 `key`를 선언한다.
같은 기기나 세션의 여러 별칭은 제공자가 `value_map`으로 같은 물리 identity에 연결한다.

```json
[
  {
    "namespace": "mobile.adb-device",
    "argument": "device_ref",
    "value_map": {"device-a": "physical-device-one", "device-b": "physical-device-one"}
  },
  {"namespace": "workspace.root", "key": "physical-workspace-one"}
]
```

- `argument`와 `key` 중 정확히 하나를 사용한다. constant에는 `optional`/`value_map`을 붙이지 않는다.
- `namespace`와 물리 identity는 그 자원을 사용하는 모든 제공자가 동일하게 정해야 한다.
  서버 ID·Run·instance·URL은 자원 identity가 아니다. 다른 대상에 요청하더라도 같은 프로필이면 충돌한다.
- `optional: true`는 입력이 없거나 null일 때 **실제로 그 자원을 사용하지 않는** 도구에만 적합하다.
  생략 시 기본 프로필을 사용한다면 constant 또는 필수 binding을 선언해야 한다.
- `value_map`이 있으면 목록 밖 별칭을 거절한다. 두 ref가 같은 물리 자원인지 문자열 이름으로 추측하지 않는다.
- 잘못된 metadata, schema에 없는 resource argument와 잘못된 런타임 값은 등록/컴파일 단계에서 거절한다.
  진단에 실제 자원 값이나 비밀 입력을 넣지 않는다.
- metadata가 없다고 병렬 실행에 안전하다는 뜻은 아니다. 기존 target 충돌 규칙은 유지하지만
  선언되지 않은 공유 프로필/기기를 자동 탐지해 보호하지는 않는다.

## 2. 외부 MCP에는 운영자가 선언을 추가할 수 있다

기존 서버 설정의 `REDTEAM_MCP_<SERVER_ID>_TOOL_RESOURCES_JSON`을 사용한다.
도구명은 실제 discovery의 remote tool name이며, 발견되지 않은 도구를 설정하면 시작을 거절한다.

```sh
export REDTEAM_MCP_BROWSER_TOOL_RESOURCES_JSON='{"inspect_page":[{"namespace":"browser.profile","key":"approved-test-profile"}]}'
```

이 예시는 원본 도구의 metadata가 없는 경우 운영자가 보완하는 형태다.
설정은 제공자 선언에 **추가**되며, 빈 목록을 설정해도 제공자의 lock을 지우지 않는다.
서버 ID `browser` 및 `inspect_page`가 실제 설치 환경에 존재한다고 가정해 자동 등록하지 않는다.
한 도구의 합산 binding은 최대 32개, 별칭 map은 최대 1,024개다.

이번에 기본 제공자 선언을 추가한 곳:

- Workspace read/write 서버: canonical root 경로 hash에 같은 `workspace.root`를 선언.
  동일 root의 읽기와 쓰기를 모두 배타적으로 실행한다. read/write lock 최적화는 구현하지 않았다.
- Mobile diagnostics `device_shell`: 정책의 device ref를 실제 serial의 hash로 연결.
  같은 serial을 가리키는 별칭은 같은 key, 다른 serial은 다른 key다. 실제 ADB 실행을 이번에 검증한 것은 아니다.

컨테이너별 mount 이름처럼 로컬 경로가 물리 자원의 전역 identity와 다르면,
그 자원을 공유하는 모든 제공자에 동일한 운영자 constant binding을 추가해야 한다.
Workspace의 경로 hash만으로 서로 다른 배포의 mount 동일성을 추측하지 않는다.

## 3. 하나의 Work 원장으로 진입·대기·확정 결과를 관리한다

```text
선택된 도구 metadata + 검증된 인자
  → namespace/물리 identity의 hash → Work.resource_keys + 기존 conflict_keys
  → 원자적 기존 lease 획득 → 현재 인자/Control 재검사 → 실행 진입 marker 기록
  → 도구 실행 → 원본 결과 확인 또는 원래 실행 reconcile
```

여러 자원이 필요한 요청은 모든 conflict를 검사하고 한 Work lease를 얻는다.
일부 자원만 먼저 잡는 순서가 없어 partial-lock 교착을 만들지 않는다.
같은 WorkStore를 사용하는 서로 다른 Run/instance/서버에도 자원 충돌이 적용된다.
Memory는 같은 객체, SQLite는 같은 데이터베이스를 공유해야 한다.

자원 key에는 원문 cookie/ref/serial/경로를 기록하지 않고 hash를 사용한다.
hash는 비밀 저장소나 암호화가 아니다. 실제 비밀 입력·원본 출력의 보호는 기존 소유 모듈의 책임이다.

도구 진입 직전에 owned/live lease로 `resource_execution_pending`을 원자적으로 기록한다.
이미 진입 marker가 있거나 Work가 없거나 resource metadata가 달라졌으면 진입을 거절한다.
정상적으로 원본 결과가 확인된 경우에만 marker를 해제한다. 이 진입 검사는 Control 승인을 대신하지 않는다.

결과가 unknown인 상태에서는 실패·취소·lease 만료·Run abandon만으로 자원을 해제하지 않는다.
기존 실행 영수증 reconcile가 Run/instance/candidate와 원본 결과를 검증한 뒤 확정해야 한다.
timeout만으로 원래 실행이 끝났다고 판단하거나 새 invocation으로 다시 제출하지 않는다.

승인 대기는 아직 제공자에 진입하지 않았으므로 기존 미실행 lease release를 사용한다.
승인이 해결되기 전 도구를 실행하거나 자동 승인하지 않는다.

## 4. native 루프의 유한 대기와 원래 요청 재개

같은 자원을 쓰는 다른 Work가 실행 중이면 native TOOLS에서 원래 call/Work/dispatch를 보존하고 기다린다.
대기 중에는 새 LLM 판단·Task claim·call ID를 만들지 않는다. 다른 자원의 독립 작업은 계속 실행할 수 있다.

`REDTEAM_RESOURCE_WAIT_TIMEOUT_SECONDS` / `resource_wait_timeout_seconds`:
기본 30초, 양수이며 최대 600초. Memory와 SQLite 모두 기존 acquire가 유일한 lock 권위다.
현재 0.1초 이하 간격으로 원장을 재확인하므로 다른 SQLite connection의 변경도 읽는다.

대기 상한 또는 terminal unknown 원본을 만나면 `resource_wait`로 정지한다.
`resource_wait_limit_reached` / `resource_execution_unresolved` 진단을 남기고, 원래 요청은 미완료로 저장한다.
이 상태는 Task 성공/실패 정산이나 새 플래너 판단의 근거가 아니다. 전문 lifecycle/activation도 held다.

기존 checkpoint 재개 경로는 안전한 완료 snapshot과 원래 pending Work의
Run/instance/model call/candidate identity를 확인한 경우에만 원래 TOOLS batch를 복원한다.
취소된 요청, 불일치, 실행 중/expired/unknown Work를 이 경로로 재실행하지 않는다.
현재 Control, Task 목적, Run 상태와 한도는 실제 진입 때 다시 확인한다.
자원이 여전히 unresolved라면 다시 대기 상태를 반환하고 도구/LLM을 호출하지 않는다.
checkpoint가 꺼져 있으면 종료한 호출의 안전한 프로세스 재개를 보장하지 않는다.

## 5. 검증 범위와 제외 범위

합성 Memory/SQLite 시험에서 같은 자원 직렬화, 독립 자원 병렬성, 다중 자원 원자성,
unknown/취소/만료/재시작 보존, pre-entry 승인, metadata drift 및 원래 checkpoint batch 재개를 검증한다.
실제 로컬 MCP SDK discovery에서 workspace read/write metadata와 Mobile serial 별칭을 확인한다.
전문 모드 public start → 자원 대기 → cohort resume → 원래 Task 완료 → 후속 Task도 합성 검증한다.

다음은 완료 주장에 포함하지 않는다.

- 실제 금융 사이트·브라우저 세션·Mobile 기기의 동시 실행/성능 검증.
- 서로 다른 WorkStore 파일/호스트 사이 분산 lock 또는 외부 수동 도구 사용의 통제.
- WorkStore를 거치지 않는 직접 Gateway/MCP 호출의 자원 순서 보장.
- 전문 플래너/Graph writer의 전체 분산 소유권 인증.
  로컬의 동일 Run 조립은 별도 [Run 실행 소유권 계약](run-execution-ownership.md)을 적용한다.
  이 Work 자원 계약 자체가 플래너의 프로세스 소유권을 제공하는 것은 아니다.
- 프로필/세션의 최초 생성·로그인·비밀 저장·유효성 갱신 자동화.

## 6. 이번 수정의 근거

1. 기존 target key만으로는 서로 다른 대상에서 같은 프로필/기기를 쓰는 충돌을 구분할 수 없었다.
   제공자 metadata를 기존 lease에 연결해 이름 기반 중앙 목록을 만들지 않았다.
2. lease TTL만 사용하면 결과가 불명확한 실행의 만료 뒤 두 번째 실행이 자원을 건드릴 수 있다.
   원본 결과 확인 전까지 durable marker를 남기고 timeout/abandon으로 해제하지 않는다.
3. lease 실패를 즉시 모델에 돌려주면 같은 요청의 재생성·중복 claim을 유발할 수 있다.
   TOOLS의 유한 대기와 원래 envelope 복원을 사용한다.
4. 일반 held 종료처럼 새 MODEL에서 재개하면 미실행 원래 요청이 사라질 수 있다.
   별도 `resource_wait` 이유와 guarded original-batch 재개를 기존 runner에 연결했다.
5. 대기를 일반 종료 정산으로 처리하면 Task 조건/소유권이 끊긴다.
   bootstrap·specialist lifecycle·activation에 suspension 의미를 연결했다.
