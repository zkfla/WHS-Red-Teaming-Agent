# MCP Tool Manifest 계약

Capability Registry는 MCP discovery 결과를 다음 메타데이터 묶음으로 Agent에
제공한다.

| 항목 | 의미 | 원본 |
|---|---|---|
| `tool_id` | 실행 방법의 안정적인 식별자 (`recon.<server_id>.http_probe`) | Capability namespace + server ID + remote tool name |
| `operations` | 수행할 수 있는 canonical intent (`service_discovery`) | 운영자 바인딩 + discovery |
| `input_schema` | MCP 호출의 JSON Schema | MCP `tools/list` |
| `required_arguments` / `optional_arguments` | 위 input schema에서 계산한 편의 view | Registry 계산값 |
| `target_types` | 적용 가능한 Graph target 종류 | Provider metadata, Capability 기본값 또는 운영자 설정 |
| `argument_bindings` | 원격 인자명 → 알려진 대상의 공통 필드명 | Provider metadata 또는 운영자 설정 |
| `risk_level` | 도구의 사전 위험 등급 | Capability 설정 |
| `output_schema` | 결과의 JSON Schema | MCP `tools/list` |
| `health_status` | 마지막 discovery/health 상태 | Adapter |

`required_arguments`와 `optional_arguments`를 별도 설정 파일에 반복해서 쓰지
않는다. 실제 MCP `input_schema`의 `required`와 `properties`가 원본이다. 시작 시
input/output schema가 유효한 JSON Schema인지 검사하고, required 인자가 properties에
실제로 존재하는지도 확인한다. `operations`는 중복 없는 lower_snake_case이며
`core.action_semantics`의 공통 형식 규칙을 적용한다. Builtin 이름은 폐쇄된
실행 허용 목록이 아니다. 확장은 명시적인 live catalog 연결, 호환되는 역할과
대상을 필요로 하며 이름 형식 통과만으로 실행 권한을 얻지 못한다.
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

## Provider의 대상 metadata

SDK `tools/list`의 `_meta`에서 선택적으로 다음 공통 키를 전달할 수 있다.
Adapter가 기존 `ToolDescriptor`에 투영하며 별도 등록 체계를 만들지 않는다.

- `redteam.target_types`: 문자열 목록. 예: `["device"]`.
- `redteam.argument_bindings`: 문자열 → 문자열 객체. 예: `{"device": "device_serial"}`.
- `redteam.command_fields`: 명령 입력 필드 목록. 기존 계약을 사용한다.
- `redteam.target_binding`: 기존 URL 대상/일치 조건 계약을 사용한다.

새 두 키의 타입이 잘못되면 discovery 단계에서 오류를 반환하고 등록하지 않는다.
정상 타입이어도 Registry의 Graph 종류 등 manifest 검증과 운영자 설정 우선순위를
거쳐야 한다. `_meta`는 대상 바인딩 선언이지 scope 확대나 Control 승인 대체가 아니다.
DeviceTarget 인자 검증은 알려진 `device_ref`, `device_serial`, `package_name`과
원격 별칭을 비교한다. 일반 별칭 `device`는 기본 기기 참조지만 provider가 serial로
명시하면 그 바인딩을 따른다. 다른 기기·앱을 명시한 행동은 후보/행동 검증에서 거절한다.
직접 Gateway만 호출하는 외부 코드는 Agent의 이 검증 경로를 우회하지 않아야 한다.
원격 서버도 자신의 기기 allowlist를 별도로 집행해야 한다.

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
