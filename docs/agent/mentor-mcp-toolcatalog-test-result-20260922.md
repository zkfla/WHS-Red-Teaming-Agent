# MCP / ToolCatalog / Orchestration 멘토링 검증 결과

- 기준 브랜치: `dev`
- 기준 커밋: `ea108d0`
- 검증일: 2026-09-22
- 실제 외부 금융기관 및 production target 사용: 없음

## 1. 중단 상태 복구 결과

### 이미 완료

- Core `ToolCatalog` Port와 `ToolDescriptor`의 operation, target type, 입력·출력
  schema, risk, health metadata 연결
- Orchestration Catalog의 Core Port 재사용
- MCP discovery의 input/output schema 보존과 등록 전 Manifest 검증
- Candidate별 target type 확인, 안전한 target 인자 바인딩, JSON Schema 검증
- 첫 번째 Tool이 부적합해도 뒤의 실행 가능한 Tool을 계속 평가하는 fallback
- 실행 가능한 Tool 사이의 deterministic selection
- 등록된 output schema와 실제 Tool output 불일치 차단
- HTTP/DNS/TLS/Nmap/OpenAPI/HTML Result Mapper와 Attack Graph 연결
- `engagement.metadata.target_refs`를 통한 host와 MCP policy target_ref 매핑
- Control Plane을 거치지 않은 Capability 실행 방지 회귀 테스트

### 일부 완료

- `service_discovery`는 DNS/HTTP/TLS를 함께 표현한다. 기본 Recon provider는
  `compatible_tool_ids`로 각 의도를 제한하지만, 제한 없는 custom provider에서는
  세 Tool이 경쟁한다.
- MCP transport에는 연결 재사용·소유 task·종료 처리가 있지만 application bootstrap은
  engagement-scoped `open_mcp_tool_gateway()`를 사용하지 않는다. Engagement별 지속
  browser context는 아직 완성되지 않았다.
- Registry에 있는 operation 중 기본 Agent provider가 제안하는 범위는 일부뿐이다.

### 보고와 달랐던 상태

- `mock-bank` ↔ `bank-lab` 매핑은 미구현이 아니었다. 현재
  `engagement.metadata.target_refs`를 seed 단계에서 Host fact로 저장하며 통합 테스트가 있다.
- 이전 보고의 테스트 수는 현재 repository와 맞지 않았다. 현재 전체 결과는 아래와 같다.

## 2. Core Contract 검토 결과

현재 `ToolDescriptor`는 Agent가 후보 적합성을 판단하는 데 필요한 다음 값을 제공한다.

- `tool_id`, `server_id`, `tool_name`
- `input_schema`, `required_arguments`, `optional_arguments`
- `output_schema`
- `operations`, `target_types`
- `risk_level`, `health_status`

역할은 분리되어 있다.

- `ToolCatalog`: metadata read-only 조회
- `ToolGateway`: 승인된 `ToolInvocation` 실행
- `CandidateCompiler`: 실행 가능성 필터와 Tool 선택
- `ControlPlane`: Action 허용·차단·승인

이번 검증에서 MCP가 `outputSchema`를 제공하지 않은 경우 빈 schema로 바꾸던 문제를
수정했다. 이제 `None`으로 보존하고 output 검증을 건너뛴다. 빈 `{}` schema가 실제로
제공된 경우에는 정상 JSON Schema로 취급해 검증한다.

### PM Contract Review Needed

1. `target_types` 허용 목록에 `device`, `observation`이 있지만 현재 Core `AnyTarget`에는
   해당 variant가 없다. 실제 Core Target 종류와 Manifest vocabulary를 맞출 결정이 필요하다.
2. JSON Schema 전체를 Core 경계에 유지하는 현재 방식은 per-tool compatibility 검증에는
   필요하지만, schema 크기 제한과 지원 draft를 계약으로 명시할 필요가 있다.
3. DNS/HTTP/TLS를 하나의 `service_discovery`로 유지할지 operation을 세분화할지 결정이
   필요하다.

## 3. Tool 후보 선택 로직

실제 순서는 다음과 같다.

1. capability namespace와 canonical operation이 일치하는 Tool 조회
2. unhealthy Tool 제외
3. target type 불일치 Tool 제외
4. schema에 선언된 안전한 target identity 인자만 바인딩
5. Tool별 input JSON Schema 검증
6. provider가 `compatible_tool_ids`를 지정했으면 그 목록으로 제한
7. 실행 가능한 Tool 중 health, 낮은 risk, ToolId lexical order로 결정

실행 불가능한 Tool은 ranking 전에 제거된다. 첫 Tool에 `url`이 필요해 실행할 수 없고
두 번째 Tool이 `target_ref + path`를 받는 경우 두 번째 Tool이 남는 것을 검증했다.

`service_discovery` 충돌 probe에서는 DNS/HTTP/TLS가 동일 schema·risk·health로 등록되면
`recon.dns_resolve`가 lexical tie-break로 선택됐다. 기본 Recon provider는 HTTP와 TLS
draft에 각각 `compatible_tool_ids`를 지정해 이 문제를 막지만, operation 의미 자체는
여전히 모호하다.

## 4. MCP schema validation 상태

- discovery input/output schema를 보존한다.
- Manifest 등록 시 JSON Schema 구조, required/properties, operation 이름, target type을
  fail-closed로 검증한다.
- 실행 후 output schema가 있으면 실제 business output을 검증한다.
- 불일치 output은 `tool_output_schema_mismatch` 실패가 된다.
- output schema가 없으면 `None`을 유지하고 검증을 생략한다.
- 샘플 결과로 schema를 생성하지 않는다.

## 5. Graph mapping 상태

| 결과 | Graph 반영 | 권한 의미 |
|---|---|---|
| HTTP probe | Host → Service → Endpoint, 실제 host/port/scheme/path/status/observation 보존 | 응답 관찰만 기록 |
| DNS | 기존 Host에 정규화된 IP observation 기록 | scope·visited·probed 자동 부여 없음 |
| TLS | 검증된 TLS Service와 protocol/cipher/certificate digest 기록 | HTTPS/TLS 관측이 있을 때만 provider가 제안 |
| Nmap | 전체 port state observation, 열린 TCP port만 Service 생성 | closed/filtered는 Service가 아님 |
| HTML | same-origin path를 discovered Endpoint candidate로 기록 | 접근 성공·scope 승인 아님 |
| OpenAPI | 선언 operation과 Resource/Endpoint candidate 기록 | `scope_unverified=True` 유지 |

## 6. 해결한 통합 gap

- output schema 부재와 빈 schema를 구분
- output schema가 없는 외부 MCP 결과를 정상 전달
- target type 불일치 Tool만 제거하고 호환 Tool은 유지하는 회귀 테스트 추가
- HTTPS 8443과 원래 `/accounts/1234` 경로가 Graph에서 손실되지 않는 회귀 테스트 추가
- 기존 host-policy alias mapping이 실제 seed/실행 경로에 연결된 상태 확인

## 7. 남은 통합 gap

### service_discovery operation collision

기본 provider는 ToolId 제한으로 안전하지만 custom provider가 operation만 지정하면
DNS/HTTP/TLS를 의도별로 구분할 수 없다. operation 세분화 또는 별도 intent metadata가
필요하다.

### Browser engagement lifecycle

Transport session은 한 scope 안에서 연결을 재사용하고 종료 후 호출을 거부한다. 그러나
application composition root가 engagement 단위 context manager를 사용하지 않아 같은
Engagement의 여러 action에서 browser context를 지속한다는 보장은 없다.

### Agent provider 연결

- Implemented + Agent reachable: `recon.service_discovery`,
  `recon.endpoint_discovery`, `web.http_request`, `web.auth_attempt`,
  `web.response_analysis`
- Implemented but not proposed by default Agent: `recon.port_scan`,
  `web.parameter_fuzz`
- Registered but integration incomplete: `analysis.static_analysis`,
  `mobile.app_launch`, `mobile.response_analysis`, `mobile.device_shell`

기본 Mobile Agent는 `mobile.static_analysis`를 제안하지만 제공된 mobile operation과 맞지
않는다. Analysis는 verifier로 주입되며 실행 Agent/provider로 등록되어 있지 않다.

## 8. 변경 파일

- `src/redteam/core/contracts/tool.py`
- `src/redteam/capability/adapters/mcp/transport.py`
- `src/redteam/capability/manifest.py`
- `src/redteam/capability/gateway.py`
- `tests/unit/test_mcp_adapter.py`
- `tests/unit/test_orchestration_candidates.py`
- `tests/unit/test_result_mapper.py`
- `docs/contracts/tool-manifest.md`
- `docs/agent/mentor-mcp-toolcatalog-test-result-20260922.md`

## 9. Contract 변경 여부

이번 검증에서는 `ToolDescriptor.output_schema`를
`dict[str, Any] | None`으로 바로잡았다. 이는 이미 도입된 output schema metadata의 부재
표현을 명확히 한 호환성 수정이다. ToolCatalog 메서드나 Action/Invocation 구조는 바꾸지
않았다.

기존 `operations`, `target_types`, JSON Schema 노출 자체는 이미 `dev`에 반영된 Core
Contract 변경이므로 PM 검토 항목으로 유지한다.

## 10. Issue 후보

1. `service_discovery`를 `dns_resolve`, `http_probe`, `tls_inspect` 등으로 세분화할지 결정
2. Core Target vocabulary와 Manifest `target_types` 정합화
3. Application에서 engagement-scoped MCP/browser lifecycle 연결
4. `recon.port_scan`, `web.parameter_fuzz`, analysis/mobile operation provider 연결
5. Tool Manifest JSON Schema 크기·draft 제한 명시

## 11. 테스트 결과

### 핵심 회귀 묶음

```text
103 passed in 1.04s
```

| 요구 | 검증 |
|---|---|
| A. 첫 Tool input mismatch, 두 번째 호환 | `test_incompatible_first_tool_does_not_hide_executable_alternative` |
| B. target type별 필터 | `test_target_type_mismatch_only_removes_the_incompatible_tool` |
| C. Manifest/discovery drift 거부 | `test_bootstrap_fails_closed_on_discovery_manifest_drift`, mobile manifest drift tests |
| D. invalid output 거부 | `test_gateway_rejects_output_that_breaks_registered_schema` |
| E. output schema 없음 | `test_adapter_preserves_absent_output_schema_as_none`, `test_gateway_skips_output_validation_when_schema_is_absent` |
| F. HTTP port/HTTPS/path 보존 | `test_probe_preserves_observed_https_port_and_original_path` |
| G. DNS IP observation/scope 비확장 | `test_dns_resolution_updates_the_existing_host_without_minting_ip_hosts` |
| H. Nmap open만 Service | `test_port_scan_materializes_only_open_services_and_keeps_all_states_as_observation` |
| I. HTML/OpenAPI candidate | inventory mapper tests의 discovered/scope_unverified 검증 |
| J. 한 Tool 실패가 전체 제거 안 함 | candidate fallback tests, recon→web handoff integration |
| K. Browser session 격리 | transport scope 재사용/종료는 검증, Engagement별 지속 session은 미완성 |
| L. Control Plane 우회 없음 | `test_every_action_type_goes_through_the_gate`, `test_nothing_reaches_the_capability_layer_without_an_allow` |

### 전체 검증

```text
pytest: 1166 passed, 6 skipped in 52.59s
ruff:   All checks passed
mypy:   Success: no issues found in 192 source files
```

6개 skip은 real ADB, 외부 apktool/mobile MCP, sandbox의 로컬 listener 등 명시적 환경이
필요한 opt-in 검증이다. 현재 실행에서는 xfail이 보고되지 않았다.

중단 직전 보고(`943 passed, 4 skipped, 2 xfailed`)와 비교하면 현재 `dev`에는 다른 팀의
추가 테스트와 통합 변경이 합쳐져 있으며, 이번 검증으로 4개 회귀 테스트를 추가했다.
