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

## 후보 선택과 Graph 반영

후보마다 target type 확인 → 구조적 인자 바인딩 → input schema 검증을 먼저
수행한 뒤, 실행 가능한 후보 사이에서 기존 건강 상태·위험도·ToolId 순위를 적용한다.
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

이 변경은 기본 playbook의 operation 목록을 늘리지 않는다. DNS/TLS/HTTP가 모두
`service_discovery`로 등록된 설정에서는 schema 적합성만으로 HTTP probe 목적을
구별할 수 없으며, DNS 결과 자체도 Web Agent를 활성화하지 않는다. 금융 기본 설정의
host(`mock-bank`)와 policy target_ref(`bank-lab`)를 연결하는 명시적 매핑, 브라우저의
engagement 단위 지속 연결, 신규 operation별 provider는 별도 통합 작업이 남아 있다.
