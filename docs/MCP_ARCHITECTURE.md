# MCP 전환 아키텍처

## 실행 순서

```text
Recon
  -> Attack Surface Modeling
  -> Planning
  -> Policy / Approval
  -> Bounded Test Execution
  -> Validation
  -> Exploitability Decision
  -> Cleanup
  -> Reporting
```

이 프로젝트에서 `침투`는 운영 데이터를 변경하거나 권한을 장악하는 범용 exploit이
아니다. 허가된 대상에서 요청 수, method, actor, payload 종류가 제한된 검증을 실행하고
재현 가능한 최소 증거를 만드는 `Bounded Test Execution`을 뜻한다.

## 호출 경계

```text
Rules/LLM Planner
  -> ActionProposal
  -> Persistent ActionQueue
  -> CapabilityPolicy + ScopeGuard + ApprovalVerifier + Budget
  -> ActionDispatcher
  -> ToolAdapter
       -> LocalProbeAdapter -> current Python Probe
       -> MCPProbeAdapter   -> allowlisted MCP server/tool
```

Planner가 MCP server 이름, tool 이름, shell command 또는 임의 네트워크 client를 선택하지
않는다. MCP tool은 capability별 코드 고정 binding으로만 결정된다.

## 현재 계약

`ToolCall`에는 run/action/queue ID, engagement, capability, target, method, risk, actor와
중앙 승인 여부가 들어간다. 승인 토큰 원문이나 actor credential은 포함하지 않는다.
`ToolExecutionResult`는 tool identity, 마스킹 대상 output, 선택적인 cleanup contract만
반환한다.

민감 capability를 원격 MCP로 보낼 때 중앙의 `approval_verified=true`만 신뢰해서는 안
된다. 현재 adapter는 서버 검증용 서명 receipt 생성기가 없으면 민감 호출을 거부한다.
실제 MCP server를 추가할 때 server는 다음을 독립적으로 다시 검사해야 한다.

1. receipt 서명·만료·issuer·engagement
2. action/run/capability/target path/method binding
3. ScopeGuard와 excluded path
4. DNS/private address/redirect 정책
5. 공유 request/rate/runtime budget
6. actor reference와 secret 비기록 규칙
7. cleanup binding 존재 여부

## MCP server 분리 순서

1. Browser MCP: 읽기 전용 navigation, DOM/form/network metadata
2. Safe HTTP MCP: allowlisted method, redirect 재검사, response size 제한
3. API Schema MCP: 관찰 request schema 기본 구현 완료, OpenAPI import는 남음
4. Actor Session MCP: 격리된 synthetic actor context
5. Canary MCP: 소유 callback 및 무해 marker correlation
6. Report MCP: evidence hash 기반 보고서 생성

Browser와 Safe HTTP server를 먼저 만들고, 두 server에 동일한 server-side policy 검사를
통과시킨 뒤에만 IDOR/XSS/SQLi용 제한 Probe를 추가한다.

## 구현된 연결부

- MCP Python SDK 2.x `Client`를 사용하는 `OfficialMCPTransport`
- Streamable HTTP URL 검증과 인메모리 MCP server 테스트
- 호출 전 bound tool 존재 확인과 structured object output 강제
- Browser `discover`, API Schema `summarize_api_schema`, Actor Session
  `summarize_actor_sessions`, Resource Model `model_resources`, Safe HTTP
  `validate_findings` server factory
- server 내부 engagement/capability/method/scope 재검증
- 상태 변경 전에 `RESERVED` cleanup task를 먼저 남기는 write-ahead 정리 계약

## 현재 남은 차단점

- 별도 MCP server process 실행·배포 구성이 없다.
- WebExplorer와 FindingValidator를 server handler로 조립하지 않았다.
- 원격 server가 검증할 비대칭 서명 receipt 발급·검증기가 없다.
- server-side DNS와 공유 persistent budget 연결이 없다.
- 휴리스틱 path template은 구현했지만 OpenAPI import와 명시적 path parameter binding이 없다.
- 재시작 시 MCP binding과 cleanup handler를 복원하는 runtime 구성이 없다.
- SQLi/XSS/IDOR oracle과 fixture 기반 정밀도 평가가 없다.

따라서 현재 코드는 실제 MCP 호출이 가능한 안전한 연결부와 server guard를 만든
상태이지, 정식 run이 MCP worker를 사용하는 상태나 자동 exploit agent가 완성된 상태는
아니다.
