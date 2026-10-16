# MCP Tool Manifest 계약

Capability Registry는 MCP discovery 결과를 다음 메타데이터 묶음으로 Agent에
제공한다.

| 항목 | 의미 | 원본 |
|---|---|---|
| `tool_id` | 실행 방법의 안정적인 식별자 (`recon.http_probe`) | Capability namespace + remote tool name |
| `operations` | 수행할 수 있는 canonical intent (`service_discovery`) | 운영자 바인딩 + discovery |
| `input_schema` | MCP 호출의 JSON Schema | MCP `tools/list` |
| `required_arguments` / `optional_arguments` | 위 input schema에서 계산한 편의 view | Registry 계산값 |
| `target_types` | 적용 가능한 Graph target 종류 | Capability 기본값 또는 운영자 설정 |
| `risk_level` | 도구의 사전 위험 등급 | Capability 설정 |
| `output_schema` | 결과의 JSON Schema | MCP `tools/list` |
| `health_status` | 마지막 discovery/health 상태 | Adapter |

`required_arguments`와 `optional_arguments`를 별도 설정 파일에 반복해서 쓰지
않는다. 실제 MCP `input_schema`의 `required`와 `properties`가 원본이다. 시작 시
input/output schema가 유효한 JSON Schema인지 검사하고, required 인자가 properties에
실제로 존재하는지도 확인한다. `operations`는 중복 없는 lower_snake_case이며
`core.action_semantics`의 공통 지원 목록에 포함되어야 한다.
형식만 맞는 미지원 연결은 서버·도구·작업 이름을 명시한 설정 오류로 등록 전에 거부한다.

제공자가 최상위 인자에 JSON Schema `readOnly: true`를 선언하면 모델용 schema와
도구 상세 조회에서는 해당 인자를 제외한다. 실행 schema와 검증은 원본 그대로다.
예를 들어 shell의 `workroom_key`는 실행 문맥에서 서버가 주입하며 모델이 추측하지 않는다.
다른 작업실 ID를 제출하면 실행을 차단하고 오류 결과를 반환한다. 이 경우 recovery
reference는 생성하지 않으며, 잘못된 입력만으로 Worker 전체를 종료하지 않는다.

대상 통신용 Kali 도구의 `target_ref`는 제공자가 `argument_bindings`에서 선언한
host selector다. MCP 정책의 opaque target ID와 혼동하지 않고 실제 host·port로 검증한다.
opaque reference를 쓰는 다른 도구의 기존 바인딩 검사는 그대로 유지한다.

HTTP MCP의 대상 연결 실패는 MCP transport 연결 실패와 구분한다. 대상 HTTP 요청이
실패하면 응답은 `is_error`이며 `diagnostic`에 실패 단계·원인 코드·수정 조건을 담는다.
원본 예외·요청 자격증명은 노출하지 않고 자동 재시도하지 않는다. Docker 내부 IP에 대한
호스트 MCP의 연결 실패는 다른 호스트 HTTP 도구로 전환해도 해결되지 않는다. 준비된
Kali 대상 네트워크 경로는 별도 실행 경로이며, MCP 호스트에 경로를 자동 생성하거나
대상 IP·port를 다른 주소로 치환하지 않는다.
환경 설정의 `TOOL_OPERATIONS_JSON`/profile 또는 discovery가 명시한 연결만 Agent에 노출한다.
운영자 연결이 discovery의 비어 있지 않은 operations와 다르면 등록을 거부한다.
설정이 없는 일반 discovery 도구와 빈 연결 `[]`의 관리용 도구는 저장 가능하지만
실행 후보가 아니며 Gateway 호출도 operation binding 검사에서 거부한다.
실제 원격 tool_name을 operation으로 자동 승격하지 않는다.

`get`, `all`, `list_by_capability`, `list_tools_for_action`과 내부 후보 view는
확정된 operation·output schema·target type을 동일하게 제공하며 방어적 복사본을 반환한다.
Gateway 실행·출력 검증도 같은 확정 descriptor를 사용한다. 운영자 metadata override가
있더라도 raw Registry만 검사하여 Agent에게 보여준 schema와 다르게 실행하지 않는다.
대상 종류가 여러 개이면 단일 대상 편의 view는 `None`을 유지하고 원본 목록을 확인한다.
`target_types`는 Core가 이해하는 Graph target 종류만 허용한다. 검증에 실패한
discovery 결과는 Registry에 등록하지 않는다. 실행 후 output이 등록된 schema를
만족하지 않으면 결과를 실패로 기록한다. MCP discovery에 `outputSchema`가 없으면
`None`으로 보존하고 결과 스키마 검증을 건너뛴다. 샘플 응답으로 스키마를 추측하지
않는다.

Agent는 특정 MCP 서버나 구현체를 import하지 않는다. Agent가 사용하는
`redteam.core.ports.tool_catalog.ToolCatalog`가 유일한 조회 계약이며,
`redteam.orchestration.candidates.catalog.ToolCatalog`는 호환용 재-export일 뿐이다.

```text
Agent → core ToolCatalog Port → Capability Registry → ToolGateway → MCP Adapter
```

후보 생성기는 먼저 `capability.operation`으로 등록된 후보를 찾고, Graph target
종류와 input schema가 맞는 후보만 남긴다. 기존 Playbook에 남아 있는 preferred
ToolId는 호환용 힌트이며, discovery에 없으면 canonical action 조회로 대체한다.
실제 실행 직전에는 Gateway가 operation binding과 schema를 다시 확인한다.

Recon 결과는 관찰된 값만 구조화한다. HTTP probe는 가능한 경우 `host`, `port`,
`scheme`, `protocol`, `service`, `status`, `status_code`, `url`을 반환하며,
확인하지 못한 제품명·버전·서비스는 추측하지 않는다. Port scan도 실제로 실행한
주소와 포트 상태만 반환한다.

## 후보 선택과 Graph 반영

후보마다 target type 확인 → 구조적 인자 바인딩 → input schema 검증을 먼저
수행한 뒤, 실행 가능한 후보 사이에서 건강 상태·위험도·ToolId 순위를 적용한다.
첫 후보의 필수 인자가 없더라도 다른 실행 가능한 후보가 있으면 사용할 수 있다.
운영자가 명시한 인자를 삭제하거나 자격증명을 추측하여 맞추지 않는다.

- `recon.http_probe`: 실제 port를 서비스/endpoint 식별자에 포함하고 HTTPS 정보를
  보존한다. 숫자 경로를 정규화하더라도 `observed_target`에 원래 요청 경로를 남긴다.
- `recon.dns_resolve`: 기존 host에 관찰한 IP 목록만 기록한다. HTTP 연결 성공이나
  해당 IP의 scope 승인을 의미하지 않는다.
- `recon.openapi_inventory`, `recon.html_inventory`: 기존 host의
  `operations_candidates`/`references_candidates`에 문서에서 발견한 경로를 기록한다.
  미방문 경로를 응답이 확인된 endpoint로 만들거나 자동 승인하지 않는다.
- `recon.port_scan`: 완료된 스캔의 포트 상태를 기록하고 열린 TCP 포트만 service로
  연결한다. 포트 번호로 HTTP/HTTPS 또는 제품명을 추측하지 않는다.

금융 랩의 `app.yaml`은 engagement metadata에서 실제 scope host `mock-bank`를
정책 별칭 `bank-lab`으로 매핑한다. 이는 `ActionProposal.target`이나 Control
Plane의 scope를 바꾸지 않고, 선택한 MCP 도구의 인자만 바꾼다. CLI 실행은
engagement 동안 MCP 세션을 유지하고 종료·오류 시 닫는다. 기본 playbook의
operation 목록은 Agent provider 구현에 따른다.

## Native schema 전달과 입력 크기

`LocalToolDescriptor.initial_exposure`는 기본 false이며, 발견 navigator의
`bootstrap`과 별개다. 제공 모듈이 선언한 읽기/Task 반환 계약은 첫 요청부터
노출한다. 현재 `evidence.read`, `task.settle`, `task.publish_result`가 선언하며
`goal.settle`의 노출·중앙 목표 판정 책임은 바꾸지 않는다. 읽기/settlement
contribution과 descriptor는 v3, 중간 결과 descriptor는 version 3이다.

외부 도구는 현재 역할 selection·eligibility·health 검사 이후의 전체 집합을
검토한다. `tool_discovery.NativeInputLimit`의 기본값·상한은 256KiB이며,
양의 정수 byte 한도만 허용한다. 새 운영 설정이나 schema cache는 없다.
전체 description/schema, 이미 필요한 local schema, 과거 assistant/tool pairing,
최신 context, 원본 창, 실제 합성된 common/role/runtime system을 포함한
`{messages, tools, system}`을 `ensure_ascii=False`, `allow_nan=False`, compact
JSON으로 직렬화한 UTF-8 byte 수가 기준이다. 이는 출력 token 예산이나 모델별
context window를 추정한 값이 아니라, 코드가 검사하는 논리 입력 envelope다.
SDK가 추가하는 전송 envelope는 이 계산에 포함되지 않는다.

전체 eligible schema가 한도 내이면 모두 제공한다. 초과하면 임의의 일부를
고르지 않고 기존 cards/search/detail 경로와 성공한 `detail_fetched_tool_ids`
이력을 사용한다. lookup 없는 assembly는 기존 전체 노출을 유지하되 같은
입력 한도를 적용한다. 최종 선택된 입력도 초과하면 모델 호출 전에
`native_input_limit_exceeded`로 중단하며 내용을 조용히 삭제하지 않는다.
매 요청 현재 registry/schema를 읽으며 조회 이력은 실행 권한이 아니다.
기존 인자·scope·승인·예산 검사는 실행 경계에서 그대로 수행한다.
