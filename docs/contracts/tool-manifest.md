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
실제로 존재하는지도 확인한다. `operations`는 중복 없는 lower_snake_case여야 하고,
`target_types`는 Core가 이해하는 Graph target 종류만 허용한다. 검증에 실패한
discovery 결과는 Registry에 등록하지 않는다. 실행 후 output이 등록된 schema를
만족하지 않으면 결과를 실패로 기록한다.

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
