# Web/API MCP 확장과 인수인계

기존 금융 랩 5개 MCP에 `web-zap`, `web-nuclei`를 선택적으로 추가한다.
기존 `web-http`에는 query/header와 브라우저 cookie snapshot 인증을 추가했다.
대상 미정 상태이므로 모두 합성 금융 랩을 기준으로 한다.

## 실행

```sh
docker compose -f deploy/compose/finance-lab.yml \
  -f deploy/compose/finance-extensions.yml run --build --rm extensions-smoke
docker compose -f deploy/compose/finance-lab.yml \
  -f deploy/compose/finance-extensions.yml down
```

확장 테스트는 실제 ZAP proxy를 통해 mock-bank `/health`에 GET 1회를 보내 캡처하고,
MCP로 그 캡처를 조회한다. Nuclei는 CSP 부재의 양성 결과와 nosniff 헤더 부재의 음성
결과를 검사한다. 자동 Spider/Active Scan은 실행하지 않는다.

ZAP 2.16.1, Nuclei 3.4.10 이미지는 검증용 버전과 digest를 고정했다.
이는 최신 버전 또는 운영환경 보안 적합성의 보증이 아니다. 신규 버전 채택 시 동일한
양성·음성 smoke 검증을 실행한다. ZAP API key는 랩 전용 합성 값이며 포트는 호스트에
공개하지 않는다. 실제 시험환경은 별도 credential/네트워크 설정이 필요하다.

Framework 설정은 `framework.env.example`에 `extensions.env.example`을 함께 로드하고,
AppConfig의 `capability.mcp_servers`에 `web-zap`, `web-nuclei`를 추가한다.
공통 Core 계약 변경 없이 기존 `web.response_analysis`에 매핑한다.

`web-nuclei`를 AppConfig에 추가하면 Web/API Agent는 실제 HTTP 응답이 Graph에 있는
경우 `web.nuclei_response_check`를 하나의 bounded 후보로 만들 수 있다. 이 후보는
Control을 통과한 뒤에만 실행되고, 실행 결과는 `observation_only`로 Graph에 기록된다.
같은 방식으로 기본 `web-browser`와 `web-proxy`가 healthy하면 Agent는 각각 관찰된
URL의 `web.browser_navigate`와 대상별 `web.proxy_list_flows` 후보를 만들 수 있다.
Browser는 HTTPX 요청보다 낮은 우선순위의 렌더링 fallback이고, Proxy 후보는 history
조회만 자동 제안한다. 전체 경로의 합성 검증표는
`docs/capability/agent-mcp-readonly-integration.md`에 있다.

| ToolId | operation | 입력 | 지원 범위 |
|---|---|---|---|
| `web.http_request` (확장) | `http_request` | 기존 입력 + `query`, `request_headers` | 경로별 허용 key와 허용 header를 사용한 단일 요청 |
| `web.zap_capture_snapshot` (신규) | `response_analysis` | `target_ref` | 실행 중 ZAP의 HAR 첫 페이지를 즉시 읽고 승인 범위 flow 요약 반환 |
| `web.nuclei_response_check` (신규) | `response_analysis` | `target_ref`, `path`, `check_id`, 선택 `session_ref` | scoped GET 1회 후 Nuclei passive 엔진으로 헤더 존재 검사 |
| `web.nuclei_active_query_probe` (선택) | `parameter_fuzz` | `target_ref`, `path`, `query_key`, 고정 `check_id` | 검증 IP에 직접 연결해 허용 query key에 marker를 넣는 GET 1회; reflection 관찰만 반환 |

## HTTP query/header

TargetProfile 예시:

```json
{
  "origin": "https://bank.test",
  "allowed_paths": ["/accounts"],
  "allowed_query_keys": {"/accounts": ["page", "account_ref"]},
  "allowed_request_headers": ["accept", "x-test-case"]
}
```

호출 인자는 `query={"page":"1"}`, `request_headers={"X-Test-Case":"case-01"}` 형태다.
Query는 HTTPX가 인코딩하고 값 안의 `&` 등을 추가 파라미터로 해석하지 않는다.
각 mapping은 최대 20개, 값은 최대 1024자다. key별 값의 업무 의미와 계정 권한은
Control/시험 시나리오가 추가 검증해야 한다. method와 정확한 path 제한은 그대로 적용한다.
Host·Authorization·Cookie·Content-Length·라우팅 변경용 헤더 등은 운영자 allowlist에도
넣을 수 없다. 자격증명은 계속 session_ref로만 선택한다.

## 브라우저 쿠키 재사용

운영자가 Playwright의 `context.storage_state(path=...)` 등으로 **계정 전용** 저장 상태를
내보내고 파일 권한을 `0600`으로 설정한다. HTTP MCP 프로세스가 읽을 수 있는 절대 경로를
아래처럼 등록한다. 컨테이너에서는 해당 계정의 파일만 읽기 전용 마운트한다.

```json
{
  "target_ref": "bank-test",
  "principal_ref": "test-alice",
  "storage_state_path": "/run/private/alice-state.json",
  "storage_state_max_age_seconds": 3600
}
```

한 SessionProfile에는 `bearer_env`와 `storage_state_path` 중 하나만 설정한다.
기존 `session_ref` 인자로 호출하면 서버가 매 요청마다 파일을 다시 읽어 Cookie를 만든다.
cookie 이름/값이나 localStorage는 Agent에 반환하지 않는다. Bearer 인증 세션도 그대로 동작한다.

적용 제한:

- 파일은 일반 파일, 최대 1 MiB, group/other 접근권한 없음, 기본 생성/갱신 1시간 이내여야 한다.
- 정확한 hostname, cookie path, 만료시간과 Secure 속성을 확인한다. domain에 선행 `.`이
  있는 공유 도메인 쿠키와 partitioned cookie는 현재 제외한다.
- 유효한 쿠키가 하나도 없으면 무인증 요청으로 넘어가지 않고 오류를 반환한다.
- 브라우저와 HTTP 세션의 실시간 양방향 동기화, 로그인 자동 실행, 토큰 갱신,
  localStorage/sessionStorage 토큰 변환, cross-site SameSite 동작 재현은 제공하지 않는다.
- 운영자가 export 파일을 원자적으로 교체하면 다음 요청에 반영된다. 계정 바인딩의 정확성은
  export 작업자의 책임이며 snapshot 내용으로 실제 사용자 신원을 검증하지 않는다.

## ZAP 실시간 연결

`web-zap --policy ... --zap-api-origin http://zap-engine:8090 --zap-api-key-env ZAP_API_KEY`
형태로 실행한다. API 주소와 key 환경변수 이름은 운영자 설정이며 Agent 입력이 아니다.
ZAP Import/Export add-on의 `exportHar` API가 필요하다. API key는 header로 전달한다.

각 MCP 호출마다 새 HAR 첫 페이지를 가져온다. 응답은 `flows`, `page_limit`,
`coverage=first_page_only`, `target_request_count=0`, `zap_api_request_count=1`을 제공한다.
flow_ref는 해당 snapshot의 식별자이며 기존 `web-proxy`의 flow_ref와 교환하지 않는다.
범위 밖·query 포함·지원하지 않는 body 형식은 기존 HAR 규칙에 따라 제외한다.
ZAP 자체가 자동으로 캡처 범위를 늘리거나 새 요청을 보내도록 설정하지 않는다.
ZAP 관측 목록은 정적 과거 캡처일 수 있으며 취약점의 현재 상태를 보장하지 않는다.
자동 polling/cursor, scan 시작·중단, Spider, Repeater/Intruder 원격 제어는 후속이다.
기존 중립 `web-proxy`의 승인 요청 재실행 기능은 계속 사용할 수 있다.

## Nuclei 연동 범위

`web-nuclei --policy ... --nuclei-binary /usr/local/bin/nuclei`로 실행한다.
현재 허용 check_id는 `missing-csp`, `missing-nosniff-header` 두 개다.
Nuclei가 대상에 직접 접근하지 않으며, 기존 HTTP 클라이언트가 DNS/IP pinning·TLS 검증·
path·method 제한을 적용해 GET 한 번을 수행한다. 검사 엔진에는 **헤더 이름만 남긴**
일시 파일을 전달한다. 원본 body·쿠키·header 값은 전달하지 않는다.

템플릿은 소스에 정의된 고정 HTTP passive matcher로 생성하고 임의 경로/URL/템플릿/CLI
인자를 받지 않는다. 자동 template update와 Interactsh를 비활성화한다. 원시 엔진 결과는
버리고 `check_id`, `matched`, `status_code`, `request_count=1`, `verdict=observation_only`만
전달한다. timeout/취소/출력 초과 시 자식 프로세스를 종료하고 일시 파일을 정리한다.

이는 **Nuclei 연결과 결과 정규화의 첫 구현**이다. 기존 http_security_posture와 일부
관찰이 중복되며, CVE 스캔·대량 검사·임의 템플릿은 지원하지 않는다.
미탐/매칭 없음은 안전하다는 판정이 아니다.

선택적 `web-nuclei-active` 프로필은 같은 MCP 서버에서
`nuclei_active_query_probe` 하나만 노출한다. `active_enabled=true`, 정확한 path와
`allowed_query_keys`가 모두 필요하다. 서버가 먼저 DNS 결과 전부를 CIDR로 검증하고 첫
검증 IP를 Nuclei target으로 사용하며 원래 Host와 HTTPS SNI를 보존한다. 생성 템플릿은
고정 marker의 반사 여부만 1회 확인하고 redirect·retry·Interactsh·update를 비활성화한다.
이는 XSS 증명이 아니며 `observation_only`다. 인증 세션, community/CVE template,
headless/code/file/network/workflow template은 지원하지 않는다.

## 남은 범위

공동인증서/금융인증서/보안모듈, MFA/WAF 우회 검증, 실거래, race/멱등성,
거래 복구는 추가되지 않았다. 앞선 설명의 금융 전용 실행기는 시험환경 계약 후 작업한다.
Agent가 스스로 scope를 확대하거나 승인하지 않으며, 규제 준수·취약점 확정도 도구가 하지 않는다.

참고: [Playwright 인증 상태](https://playwright.dev/docs/auth),
[ZAP API](https://www.zaproxy.org/docs/api/),
[Nuclei passive 모드 및 template 다운로드 설정](https://github.com/projectdiscovery/nuclei-docs/blob/main/docs/nuclei/get-started.md).
