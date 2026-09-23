# 금융권 Web/API · Recon MCP 개발 구성

## 목적과 서버 배치

HTTP query/header, 브라우저 cookie snapshot 재사용, 선택적인 ZAP 실시간 캡처 조회와
Nuclei passive 검사 연동은 [확장 실행 및 인수인계](EXTENSIONS.md)를 참고한다.

MCP 서버는 프로그램이다. 개발·통합 모두 동일 컴퓨터에서 stdio로 실행할 수 있다.
HTTP는 별도 프로세스/호스트에 연결하는 선택지이며 GCP가 필수는 아니다.
이 예제는 브라우저 상태를 유지하고 팀이 동일한 환경을 재현하도록 로컬 Docker
Compose의 다섯 MCP 서비스와 합성 금융 API 한 개를 사용한다.

| MCP 서비스 | 기반 | 실제 등록 도구 | 금융 테스트에서의 용도 |
|---|---|---|---|
| web-http | HTTPX를 감싼 소형 MCP façade | `web.session_list`, `web.http_request`, `web.http_compare_sessions`, `web.http_security_posture`, `web.http_compare_anonymous` | 테스트 계정별 인증 API 호출, 계정/비로그인 응답 비교, 보안 헤더·쿠키 속성 관찰 |
| web-proxy | 제품 중립 HAR 1.2 façade | `web.proxy_status`, `web.proxy_list_flows`, `web.proxy_get_flow`, `web.proxy_reload_capture`, `web.proxy_replay_flow`, `web.proxy_mutate_json`, `web.proxy_compare_sessions` | ZAP/Burp에서 수집한 승인 범위 요청을 공통 목록으로 조회하고 제한적으로 재실행·비교 |
| web-browser | 공식 Microsoft Playwright MCP 0.0.81 | `web.browser_navigate`, `web.browser_snapshot`, `web.browser_click`, `web.browser_fill_form`, `web.browser_close` | 로그인·추가 인증·동적 UI의 후속 연결 기반 |
| recon-passive | Python DNS/TLS 및 HTTPX façade | `recon.dns_resolve`, `recon.http_probe`, `recon.tls_inspect`, `recon.openapi_inventory`, `recon.oidc_inventory`, `recon.html_inventory` | 승인 호스트 해석, TLS 검증, API/OIDC/HTML 경로 목록 수집 |
| recon-active | 고정 인자의 Nmap façade | `recon.active_status`, `recon.port_scan` | 한 IP의 운영자 지정 포트 최대 16개 TCP connect 점검; 기본 비활성 |

`recon-passive`는 기존 계획의 이름을 유지한다. DNS/TLS/HTTP 조회는 실제 트래픽을
발생시키므로 순수 passive OSINT 또는 자동 허가를 의미하지 않는다.
Nmap/httpx CLI/ZAP은 그 자체가 이 프로젝트의 MCP 서버가 아니다. 기존 기능을 MCP로
노출하는 wrapper와 공식 MCP 배포판을 구분한다. `web-proxy`는 ZAP이나 Burp 프로세스를
직접 제어하지 않고 제품별 connector가 만든 HAR 1.2 파일을 읽는다. ZAP은 Import/Export
add-on의 `exportHar` API, Burp는 `Save items`가 만든 XML을 connector가 같은 계약으로
변환한다. 교체된 파일은 `proxy_reload_capture`로 다시 읽는다. 자체 exploit이나
스캐너 엔진은 구현하지 않는다.

## 기존 계획을 조정한 이유

- OIDC discovery를 추가했다. 금융 API의 인증 방식·PKCE 공시 여부를 수집하되
  공시 내용만으로 준수/취약을 확정하지 않는다.
- `session_create` 대신 운영자가 미리 등록한 두 테스트 계정의 `session_ref`를 쓴다.
  현재 stdio transport는 매 호출마다 자식 프로세스를 시작하므로 인메모리 로그인
  세션을 유지한다고 가정하면 안 된다. 동적 로그인·쿠키 갱신 저장소는 후속 구현이다.
- 원시 응답·토큰·쿠키는 HTTP 도구 출력에 포함하지 않는다. 상태/크기/해시만 반환한다.
  응답의 동일/상이 여부는 관찰이며 BOLA나 권한 상승의 증명이 아니다.
- Proxy 캡처의 Authorization·Cookie·Set-Cookie와 원시 body는 MCP 출력에 노출하지
  않는다. 재실행은 캡처 자격증명이 아니라 policy에 등록된 `session_ref`를 사용한다.
  정확한 path별 method, 재실행 path, 변경 가능한 최상위 JSON 필드는 운영자가 정한다.
- 브라우저는 `open_mcp_tool_gateway()`의 지속 MCP 연결과 `--isolated` context를
  사용한다. 호출마다 연결하는 기존 `build_mcp_tool_gateway()`로는 페이지가
  사라지므로 브라우저 워크플로에 사용하지 않는다. 실행/테스트 계정별 전용
  lifecycle을 사용하며 계정 A/B 비교는 HTTP 도구로 검증한다.
- Nuclei 직접 Active Scan, ZAP/Burp scan 제어, httpx CLI, 대량 fuzzing,
  거래 재전송·경쟁 상태 검사는 후속이다. ZAP 실시간 캡처 조회와 Nuclei passive 검사는
  선택적인 [확장 구성](EXTENSIONS.md)으로 제공한다.
  지금 HTTPX 기반 저부하 조회와 기능이 겹치는 엔진들을 함께 설치하지 않는다.

## 빠른 시작 (저장소 루트)

```sh
pip install -e '.[dev]'
docker compose -f deploy/compose/finance-lab.yml up --build -d --wait
docker compose -f deploy/compose/finance-lab.yml run --build --rm smoke
docker compose -f deploy/compose/finance-lab.yml run --build --rm agent-smoke
docker compose -f deploy/compose/finance-lab.yml down
```

`smoke`는 MCP 도구를 직접 호출한다. `agent-smoke`는 기본 Recon/Web Agent가
Control Plane을 거쳐 실제 MCP 도구를 실행하는지 확인한다. 먼저 기본 승인 정책에서
Web 요청이 대기하는지 확인하고, 다음으로 합성 대상의 승인 요구만 제거한 독립
engagement에서 Recon → Graph → Web GET을 확인한다. 대상 별칭은 `app.yaml`의
engagement metadata에 명시하며, 승인 없이 실제 금융기관에 요청하는 설정은 없다.

## 현재 금융권 웹 접근 가능 여부

기본 배포 설정은 **합성 mock-bank 전용**이다. Compose `internal: true`,
policy의 `http://mock-bank:8080` 및 `172.30.50.0/24` 제한 때문에 외부 금융사에는
접속할 수 없다. 시험 대상이 미정인 현재 실제 국내 금융사 접근 성공은 미검증이다.
DNS/HTTP/HTTPS 기능의 존재와 특정 금융사 환경 접속 성공은 구분한다.
로컬 HTTPS 검증은 `tests/integration/test_finance_tls.py`에서 IP pinning, Host/SNI 유지,
신뢰하지 않는 인증서 거부를 검사한다. 실제 금융 솔루션 탐지·우회 성공을 의미하지 않는다.

시험 대상이 정해지면 별도 engagement policy에 정확한 HTTPS origin, 허용 path/method,
승인 CIDR를 등록하고, 필요하면 시험망 VPN/라우팅 및 해당 대상 전용 egress를 구성한다.
Bearer 테스트 계정은 `session_ref`/환경변수로 등록한다. 현재 기본 HTTP 도구는
공동인증서·키보드보안 모듈·MFA 로그인이나 쿠키 기반 세션 갱신을 수행하지 않는다.
IP 또는 CDN 구성이 바뀌면 승인 CIDR를 재검토해야 하며, 발견 주소를 자동 허용하지 않는다.
기존 합성 랩의 격리 설정은 유지하고 별도 시험 환경 설정을 만든다.

## 추가 관찰 도구와 Agent 호출 계약

추가 MCP 프로세스 없이 기존 Web/API·Recon 서버에 다음 세 도구를 등록한다.
모두 기존 `response_analysis`/`endpoint_discovery` operation을 사용하므로 Core 변경은 없다.

| ToolId | canonical action key | 입력 | 실제 HTTP 요청 수 | 결과와 용도 |
|---|---|---|---|---|
| `web.http_security_posture` | `web.response_analysis` | `target_ref`, `path`, 선택 `session_ref` | 1 GET | 보안 헤더 존재, no-store, nosniff, 개별 Set-Cookie의 Secure/HttpOnly/SameSite 등 관찰 |
| `web.http_compare_anonymous` | `web.response_analysis` | `target_ref`, `path`, 필수 `session_ref` | 2 GET | 비로그인 → 등록 Bearer 계정의 상태/본문 해시 비교 |
| `recon.html_inventory` | `recon.endpoint_discovery` | `target_ref`, `path` | 1 GET | 동일 origin의 링크·폼 action·script/link 경로 후보, path allowlist 포함 여부 |

헤더의 존재만으로 CSP/HSTS 적용 효과나 금융 규제 준수를 판정하지 않는다.
쿠키는 이름/값/Domain 값 없이 속성만 반환한다. 최대 50개를 분석하며 초과 시 표시한다.
쿠키가 관찰되지 않아도 안전하다고 판정하지 않는다. 동일한 응답 역시 인증 우회의 증명이
아니며 모든 결과는 `observation_only`이다. 헤더 기준은
[OWASP HTTP Headers](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html)와
[MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)를
참고했다. no-cache와 no-store, CSP report-only와 enforced CSP를 구분한다.

HTML은 `200 text/html`만 분석하며 JS 실행·폼 제출·추가 fetch는 하지 않는다.
UTF-8 및 HTTP charset에 명시된 EUC-KR/CP949 계열을 지원하고 다른 charset은 거부한다.
meta charset 자동 탐지는 제공하지 않는다. 최대 200개 reference를 처리하며 초과 여부를
표시한다. query/fragment 값, 폼 값, 본문 텍스트, 외부 origin URL은 출력에서 제외한다.
**path 자체에 포함된 개인정보까지 마스킹하지는 않으므로 시험 데이터 경로를 사용한다.**
발견 경로의 `path_allowlisted`는 path 포함 여부일 뿐 method/network/ROE 승인이 아니다.
HTML 참조는 비신뢰 데이터이며 국내 보안 솔루션 제품명/버전으로 자동 확정하지 않는다.

## ZAP/Burp 캡처 연결

connector는 운영자가 실행하며 Agent가 임의 URL이나 API key를 전달하지 않는다.
ZAP API key는 명령줄에 쓰지 않고 환경변수 이름만 지정한다. ZAP Import/Export add-on이
설치되어 있어야 한다.

```sh
export ZAP_API_KEY='operator-managed-secret'
python -m redteam.capability.connectors.proxy_capture \
  --policy deploy/mcp/finance/policy.json \
  --target-ref bank-lab \
  --output /private/capture/catalog.har \
  zap --api-url http://127.0.0.1:8080 --api-key-env ZAP_API_KEY
```

Burp에서는 승인 대상 항목을 선택해 `Save items` XML로 저장한 뒤 변환한다. 이 과정은
요청을 전송하지 않는다.

```sh
python -m redteam.capability.connectors.proxy_capture \
  --policy deploy/mcp/finance/policy.json \
  --target-ref bank-lab \
  --output /private/capture/catalog.har \
  burp --input /private/burp/selected-items.xml
```

두 connector 모두 허용 origin/path/method 및 JSON body 규격에 맞는 flow만 보존하고,
Authorization·Cookie·Proxy-Authorization·Set-Cookie 값은 출력 catalog에서 제거한다.
out-of-scope, query 포함 요청, 지원하지 않는 form/multipart/binary body는 제외한다.
catalog에는 재실행용 JSON body와 응답 body가 남을 수 있으므로 실데이터가 아닌 테스트
데이터만 사용하고 접근권한·보존기간을 별도로 관리한다. 새 파일은 권한 `0600`으로
만들며 기존 파일 교체 시 기존 권한을 유지한다.

실행 중 교체는 connector가 Proxy MCP와 같은 파일시스템의 catalog를 원자적으로 바꾼
후 `web.proxy_reload_capture`를 한 번 호출한다. Compose 예제는 원자적 교체가 보이도록
finance 디렉터리를 읽기 전용 마운트한다. 별도 호스트 배포에서는 private volume을
connector와 Proxy MCP가 공유해야 한다.

Python 예제는 다섯 서비스의 등록/호출을 확인하는 **운영자 실행 연결 테스트**이다.
실제 Agent는 `ActionProposal → Control Plane → build_tool_invocation → ToolGateway`
경로로 실행해야 한다. `deploy/mcp/finance/app.yaml`은 `build_application()`에 넣는
AppConfig 예제이며 현재 engagement 전용 CLI 입력 파일과 구별한다.

Orchestration의 기존 composition root는 아직 호출별 연결 방식을 사용한다.
브라우저를 연결할 때 아래처럼 owner task에서 지속 Gateway를 열고 기존 주입
지점에 전달해야 한다. 이 PR은 다른 팀의 app/Orchestration 코드를 변경하지 않는다.

```python
from redteam.capability.bootstrap import open_mcp_tool_gateway

async with open_mcp_tool_gateway([
    "web-http", "web-proxy", "web-browser", "recon-passive", "recon-active",
]) as gateway:
    # build_application(config, tool_gateway=gateway) 등 기존 주입 지점에 연결
    # 승인된 invocation만 gateway.invoke(invocation)으로 전달
    ...
```

같은 async task에서 scope를 열고 닫아야 하며 호출 작업들을 모두 끝낸 뒤 닫는다.
연결 실패 후 자동 재연결/재전송하지 않는다. 종료 시 MCP 연결과 stdio 자식을
정리하며 시작 도중 실패해도 이미 열린 연결을 정리한다.

MCP 포트와 mock-bank는 호스트에 게시하지 않는다. smoke 실행기도 같은 내부
네트워크 안에서 실행한다. `framework.env.example` 주소는 컨테이너 내부용이다.
향후 Orchestration 컨테이너를 같은 네트워크에 붙여 같은 주소를 사용할 수 있다.
Compose의 `internal: true` 네트워크에 외부 금융 사이트는 없다.
대상 subnet은 `172.30.50.0/24`이며 충돌 시 Compose와 policy를 함께 수정한다.
다섯 서비스는 인증 없는 **단일 운영자 로컬 합성 데이터 랩**이다. HTTP 서비스를
GCP 외부 IP에 그대로 게시하는 배포 예제가 아니다.

Playwright 컨테이너는 Chromium을 `--no-sandbox`로 실행하므로 컨테이너 격리와
합성 테스트망에 한정한다. 인증서·로그인·실계좌 데이터가 있는 운영 배포에는
별도의 네트워크 정책, 프로세스 격리, MCP 인증과 보안 검토가 필요하다.
`--allowed-origins`는 보조 설정이며 리다이렉트까지 막는 보안 경계가 아니다.
동일 랩 네트워크 내 MCP 서비스 간 접근도 완전히 분리하지 않는다.

## Python 서버를 stdio로 실행

```sh
python -m redteam.capability.servers web-http --policy /absolute/path/policy.json
python -m redteam.capability.servers web-proxy \
  --policy /absolute/path/policy.json \
  --capture-path /absolute/path/captures.har
python -m redteam.capability.servers recon-passive --policy /absolute/path/policy.json
python -m redteam.capability.servers recon-active --policy /absolute/path/policy.json
```

컨테이너 밖에서는 policy의 `mock-bank` 대신 로컬 합성 서버의 origin과 CIDR을
지정한다. Framework의 COMMAND는 해당 venv Python 절대 경로, ARGS_JSON은 위 인자를
사용한다. `TOKEN_A` 등 인증 변수는 기존 `PASSTHROUGH_ENV`에 명시해야 한다.
브라우저를 stdio로 사용하는 경우에도 지속 `open_mcp_tool_gateway()` scope가 필요하다.

## 적용된 실행 경계

- URL 대신 운영자 정의 `target_ref`와 정확한 허용 path를 받는다. 경로 탐색·외부 URL은
  받지 않는다. query/header는 운영자가 등록한 key만 허용한다([설정](EXTENSIONS.md)).
- DNS 결과 전체를 CIDR와 검사하고 검증 IP로 접속한다. HTTP Host와 TLS SNI/인증서
  검증 호스트를 보존한다. Link-local/metadata, multicast, unspecified는 거부한다.
- redirect 자동 추적과 HTTP 자동 retry는 없다. 사용자 POST 등을 임의로 재전송하지 않는다.
- 기본 허용 method는 GET/HEAD/OPTIONS이며 금융권의 일부 GET도 부작용이 있을 수
  있으므로 운영자가 정확한 path를 검토해야 한다. 쓰기 method는 별도 설정 사항이다.
- timeout·응답 byte 제한, 압축 응답 거부, 한 프로세스 내 네트워크 동작 직렬화.
  Gateway는 `MAX_CONCURRENCY`로 MCP 서버별 동시 호출을 다시 제한하며 대기시간도
  invocation timeout에 포함한다. 비정상 상태로 발견된 서버는 실제 호출 전에 차단한다.
  `MAX_OUTPUT_BYTES`를 넘는 결과는 일부만 잘라 정상 결과처럼 전달하지 않고 전체를
  생략한 `tool_output_too_large` 오류로 변환한다.
  engagement 전체 요청량/Budget은 Control Plane 책임이다.
- 계정 자격증명은 `bearer_env` 또는 전용 `storage_state_path`로 조회하고 각 session_ref는
  한 target_ref에 묶인다. 브라우저 snapshot 재사용은 [제한된 쿠키 연결](EXTENSIONS.md)이다.
  토큰 자동 발급/갱신, 원본 증거 암호화 저장, 범용 개인정보 마스킹은 구현하지 않았다.
- OpenAPI의 `$ref`/servers, OIDC의 endpoints는 자동 방문하지 않는다. 발견 경로가
  자동으로 승인 범위에 추가되지 않으며 출력은 비신뢰 대상 데이터로 다룬다.
- `ALLOWED_TOOLS_JSON`으로 공식 서버의 도구도 선택 등록하며 직접 호출도 제한한다.
  `TOOL_OPERATIONS_JSON`은 각 원격 Tool 이름에 실행 가능한 canonical operation을
  연결한다. 빈 배열인 운영 도구는 Agent 실행을 막고, 매핑 누락·upstream 이름 변경은
  시작 실패로 알린다. 임의 코드 실행·파일 업로드는 미등록이다.
- Proxy catalog와 connector 입력은 4 MiB/기본 500개 flow로 제한하며 query·fragment·범위 밖 origin/path/
  method를 가져오지 않는다. multipart/form body는 지원하지 않으며, 실행 중 파일 갱신은
  명시적인 `proxy_reload_capture` 호출로만 반영한다. 캡처 파일은 합성·비식별 테스트
  데이터로 만들고 Proxy MCP에는 read-only로 마운트한다.
- `proxy_replay_enabled` 기본값은 false다. 재실행을 켜도 `replayable_paths`에 없는 요청,
  path별 허용 method가 아닌 요청, `mutable_json_fields` 밖의 변경은 거부한다. 자동 retry,
  redirect 추적, captured credential 재사용은 없다. 쓰기 method는 등록된 `session_ref`가
  없으면 실행하지 않는다.
- `dry_run=true`는 Gateway에서 실제 Adapter 호출 없이 반환한다.

## Control / Agent 팀 연결 사항

`risk_level=high`는 metadata이며 현재 Control Plane은 이것만으로 승인 요청을 하지
않는다. `app.yaml`의 승인 목록은 ToolId 목록이 아니라 의미 기반 operation 목록이다.
현재 Control의 `ApprovalPolicy` matcher가 사용하는 legacy 표기인
`capability:operation`을 따르며, ROE의 canonical action key는 PM 계약대로
`capability.operation`을 사용한다. 두 형식을 섞거나 ToolId를 승인 규칙에 넣으면 안 된다.
현 승인 서비스는 사람 승인 해결 흐름이 미완성이므로 Active를 production에서 켜지 않는다.
`active_enabled=true`는 운영자 on/off 스위치이지 승인 토큰이 아니다.

Agent/Orchestration은 먼저 의미 기반 ActionProposal을 만들고 Control의 ALLOW 이후에
실행 ToolId를 선택한다. 아래는 현재 금융 랩 도구의 권장 연결이며, 하나의 operation에
복수 ToolId가 올 수 있다. Gateway는 ToolId로 실제 원격 도구를 찾으며
`ActionProposal.operation == remote tool_name`을 요구하지 않는다.

| canonical action key | 선택 가능한 ToolId | 비고 |
|---|---|---|
| `web.http_request` | `web.http_request`, `web.proxy_replay_flow`, `web.browser_navigate` | 승인된 API/캡처/UI 경로 중 문맥에 맞게 선택 |
| `web.auth_attempt` | `web.browser_fill_form`, `web.browser_click` | 로그인·추가 인증 절차에 한정; 일반 click은 Agent가 더 정확한 operation을 제안해야 함 |
| `web.parameter_fuzz` | `web.proxy_mutate_json` | 허용된 JSON 최상위 필드만 변경 |
| `web.response_analysis` | `web.http_compare_sessions`, `web.http_compare_anonymous`, `web.http_security_posture`, `web.proxy_compare_sessions`, `web.browser_snapshot` | 결과 차이·헤더·쿠키 속성은 관찰이며 취약점 확정이 아님 |
| `recon.service_discovery` | `recon.dns_resolve`, `recon.http_probe`, `recon.tls_inspect` | 동일 intent를 서로 다른 관찰 도구가 수행 |
| `recon.endpoint_discovery` | `recon.openapi_inventory`, `recon.oidc_inventory`, `recon.html_inventory` | 문서/정적 HTML에 노출된 경로만 수집 |
| `recon.port_scan` | `recon.port_scan` | 이름이 같아도 action key와 ToolId는 별도 값 |

`web.session_list`, `web.proxy_reload_capture`, `web.browser_close`,
`recon.active_status`는 운영·상태 관리용이다. Agent의 자율 공격 행동으로 제안하지 않고
운영자 워크플로 또는 명시적으로 합의한 별도 operation 계약에서만 사용한다.

현재 서버는 engagement ID를 전달받지 않는다. policy/credential/browser 인스턴스를
하나의 engagement에 전용 배치하고, Control 팀은 proposal의 실제 target과
`arguments.target_ref` 매핑을 일치시키는 통합 검증을 추가해야 한다. 공통 Core
스키마는 이 PR에서 변경하지 않는다.

`http_compare_sessions`는 한 도구 호출 안에서 GET 두 번을 실행한다. 도구 호출
Budget과 하위 HTTP 요청 수는 같지 않다. 비용·요청량 정책에 2회로 반영해야 한다.
`proxy_compare_sessions`도 같은 방식으로 정확히 두 번 호출하므로 동일하게 반영한다.
`http_compare_anonymous` 역시 성공 시 정확히 두 번 호출한다. 세 신규 관찰 도구의
`request_count`는 완료된 호출의 요청 수이며 실패 전에 전송된 요청의 중앙 집계는 후속이다.
MFA 우회, 실거래, 자동 이체, 대량 병렬 호출, 롤백 보장은 제공하지 않는다.
TLS는 검증된 한 번의 handshake이며 전체 프로토콜/암호군 스캐너가 아니다.

## 후속 후보 (아직 연결되지 않음)

1. ZAP 증분 동기화: engagement별 cursor와 승인 Context 매핑이 정해진 뒤 polling 추가.
2. Burp 실시간 연결: 팀 Burp edition과 공식 MCP 사용 여부가 정해지면 saved-items 변환과 역할 중복 검토.
3. Nuclei Active: passive 연동 위에 검토된 template·egress·요청 수·취소 계약을 추가.
4. 거래 재전송/멱등성/경쟁 상태: 합성 계좌·거래 한도·정합성 oracle·복구 계약이 있는 후속 PR.

출처: [Playwright MCP](https://github.com/microsoft/playwright-mcp),
[HTTPX transports](https://www.python-httpx.org/advanced/transports/),
[Nmap XML](https://nmap.org/book/output-formats-xml-output.html),
[ZAP API](https://www.zaproxy.org/docs/api/),
[PortSwigger MCP](https://github.com/PortSwigger/mcp-server).
