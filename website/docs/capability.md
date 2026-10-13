# Capability 계층

Capability는 도구를 만드는 곳이 아니라 어댑터로 연결하는 곳이다. `ToolGateway → MCPToolRegistry → MCPAdapter → MCP Server`로 이어진다. 각 도구는 `ToolDescriptor`로 등록되고 operator 정책이 target, method, path 단위로 허용 목록을 건다.

- **실연결과 검증.** Web/API와 Recon 도구(`send_http_request`, `mutate_request`, `replay_request`, `resolve_host`, `probe_http`, `fetch_openapi`, `enumerate_routes` 등). 외부 바이너리 없이 동작하는 범위 제한 HTTP와 recon, proxy를 포함한다.
- **후보.** Mobile과 Analysis 어댑터(apktool, jadx, mobile-mcp 등)는 전체 프로필에 설계돼 있으나 외부 MCP 서버 연동이 미확정이다.
- **정직한 빈 카탈로그.** MCP가 설정되지 않은 배포는 가짜 도구 대신 빈 도구 카탈로그를 반환한다. 미설정을 실제 실행처럼 보이게 하지 않는다. `ExampleMCPAdapter`는 명시적 opt-in(`REDTEAM_MCP_SERVERS=example`) 테스트 더블이다.
