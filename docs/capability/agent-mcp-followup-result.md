# Agent–MCP 후속 통합 결과

작업 ID: `CAP-AGENT-MCP-FOLLOWUP-20260923`  
기준: `dev` `be3a0de` → Draft PR #16 (`capability-agent-mcp-wiring`)

## 완료한 변경

1. Graph의 endpoint에 `path_allowlisted=false`가 명시되면 Recon/Web 후보가 그 경로를 방문하지 않게 했다. 이 신호가 없는 경로에 허가를 추론하지 않는다. MCP 서버의 path/method/network 허용 목록은 최종 집행 지점으로 유지한다.
2. 합성 금융 랩의 Agent 통합 검증을 초기 Recon→Web 호출에서 HTML inventory·Web security posture까지 확장했다. 승인 요구가 있는 실행은 Web 전에 대기하고, 합성 환경 자동 승인 실행만 더 진행한다.
3. `dev` 기준 Python CI를 막던 9개 테스트 파일의 mypy 오류 42개를 테스트 타입 표기·가짜 객체 계약 정리로 해소했다. 제품의 승인·도구 동작은 변경하지 않았다.

## 검증 범위

| 구분 | 현재 근거 |
|---|---|
| Agent→Control→Capability→MCP→Graph→Web 실제 실행 | 합성 `mock-bank` 대상 GitHub Actions `finance-lab` 통과. `recon.http_probe`, `web.http_request`, `recon.html_inventory`, `web.http_security_posture` 실행 확인 |
| 승인 경계 | 기본 정책에서 Recon 이후 Web 실행 전 `approval_pending` 확인 |
| 명시적 미허용 경로 | `/assets/test-sdk.js`가 발견돼 Graph에 남아도 Web 요청을 제안하지 않도록 단위/합성 랩에서 검사 |
| 전체 Python 테스트 | 로컬 1,170 passed, 4 skipped; Ruff 및 `mypy src/redteam tests` 통과 |

## Agent 연결과 서버 등록을 구별한 현황

- 실제 Agent→MCP 실행까지 검증: `recon.http_probe`, `recon.html_inventory`, `web.http_request`, `web.http_security_posture`.
- Agent 후보·Mapper 단위 테스트는 있으나 이 합성 랩에서 끝까지 실행되지 않음: `recon.tls_inspect`, `recon.openapi_inventory`, `recon.oidc_inventory`, `web.http_compare_sessions`, `web.http_compare_anonymous`. 필요한 HTTPS·문서 endpoint·승인된 세션 상태가 관측돼야 한다.
- MCP 서버에는 등록됐으나 현재 기본 Agent의 자율 후보가 아님: `recon.dns_resolve`, `recon.port_scan`, Proxy replay/mutation, Browser navigate/click/form 등. 특히 Active Recon과 쓰기성 도구는 등록만으로 실행 승인되지 않는다.

## 남은 조건과 한계

- 시험 환경 URL·ROE·승인된 세션 및 경로가 정해지지 않아 실제 한국 금융기관 접속은 수행하지 않았다.
- `path_allowlisted`는 MCP inventory가 명시적으로 반환한 경로에 한해 후보 생성에 반영된다. 미지 경로의 최종 허용 여부는 MCP 서버가 계속 판정한다.
- TLS/OIDC/OpenAPI 및 세션 비교의 실제 Agent 실행 검증은 각각의 합성 fixture와 승인 조건을 갖춘 뒤 추가한다. Proxy/Browser 및 Active Recon을 Agent에 자율 연결하는 것은 별도 정책 결정이 필요하다.
