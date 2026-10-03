# 공개 페이지 텍스트 읽기 — C 내부 스펙

기준: `basic-computer-tools-team-plan_rev2.md`의 기반 단계, 개발 시작 HEAD
`8979cac5f73de106c7433405cdb26c69ae3b4f74`, `capability/search-tools`.

## 목적과 구현 경계

`servers/page_text.py`의 `PageTextReader.fetch_page_text()`는 운영자가 허용한
공개 HTTPS 페이지를 읽어 실제 텍스트와 출처를 반환하는 내부 기능이다.
`servers/page_text_mcp.py`가 이 구현을 MCP `fetch_page_text` 도구로 노출한다.
Capability profile이 명시적으로 설정되면 bootstrap이 discovery를 검증해
Registry·Gateway에 등록한다. 2026-10-03 공통 통합에서 사용자 확인에 따라
`page_read`를 공통 canonical operation에 반영했다. 등록·Agent 계획은 같은
Core 작업 정의를 검사한다. 실행 승인·배포 ROE는 별도이다.

JavaScript/브라우저 실행, 로그인, 파일 저장, PDF, 검색엔진, PoC 실행을 지원하지
않는다. 본문은 불신 입력인 관측 자료다. 본문의 지시를 실행하거나 scope에 대상을
추가하거나 가설·Finding을 생성하지 않는다. 모든 민감정보를 본문에서 제거하는
기능은 없으며, 승인된 공개 origin이라도 민감한 본문을 반환할 수 있다.

다른 팀 구현을 import하지 않는다. 시험 대상 정책과 인증 세션에 결합된 기존
`servers/network.py`를 직접 호출하는 대신, 그 코드의 IP 고정·Host/SNI 유지 패턴을
독립적인 공개 자료 정책에 적용했다. 내부 `PageTextError`는 MCP 경계에서
고정 코드의 SDK `ToolError`로 변환되며, 공통 Adapter·Gateway가 기존
`ToolResult`/`ToolError`와 Action·Invocation ID를 유지한다.

## MCP 설정·등록

운영자 JSON 파일 하나가 허용 origin과 모든 페이지 제한값의 원본이다.
예시 값은 다음과 같다. 빈 `allowed_origins`는 모든 접근을 거절한다.

```json
{
  "allowed_origins": ["https://docs.example"],
  "timeout_seconds": 5.0,
  "max_download_bytes": 262144,
  "max_return_chars": 20000,
  "max_redirects": 3
}
```

`.env.example`의 `page-text` 예시는 stdio MCP 서버를 명시적으로 활성화한다.
`REDTEAM_MCP_PAGE_TEXT_PAGE_TEXT_CONFIG`는 운영자 파일의 절대 경로다.
Capability 설정은 기존 `PageTextConfig`로 파일을 검사하고 SHA-256을 stdio
인자에 고정한다. 자식 서버는 실행 직전 같은 파일을 다시 검사하며 바이트가
변경됐으면 시작하지 않는다. 이 profile은 remote tool `fetch_page_text` 하나와
canonical operation `page_read`를 정확히 바인딩한다. 호출 인자에는 URL과 선택적
`max_chars`만 있고 정책·상한·인증정보는 없다. discovery의 입력 한도와 출력
schema를 실제 MCP 응답 및 같은 설정값에 대조한 뒤 등록한다. 이 도구만
`url` 대상 메타데이터를 가지며 기존 `web_api` 기본 target 목록은 유지한다.

`web` namespace, Action Key `web.page_read`, 실행 ToolId
`web.fetch_page_text`는 서로 의미가 다르다. 공통 naming 검증과 Agent 후보 생성·
Control 판단·Gateway 호출은 합성 테스트로 연결 검증했다. 실제 LLM 자유 선택과
운영 배포는 별도 검증이며, 운영자는 알려진 UrlTarget 및 승인 scope·ROE를 설정해야 한다.

## 입력·설정·출력

세 모델은 Pydantic 모델이며 추가 필드를 거절하고 frozen 설정을 사용한다.
설정의 allowlist는 불변 tuple이다. 운영자는 `PageTextReader` 생성 시 설정을
주입한다. 요청에는 정책·클라이언트·resolver·헤더·인증정보를 넣을 수 없다.

| 모델/필드 | 의미 |
|---|---|
| `PageTextRequest.url` | 절대 HTTPS URL, 최대 8192문자 |
| `PageTextRequest.max_chars` | 선택적 양의 정수. 서버 `max_return_chars`보다 크면 오류 |
| `PageTextConfig.allowed_origins` | 정확한 `https://host[:port]` 목록. 기본 `()`는 모든 접근 거절 |
| `timeout_seconds` | 기본 5초, 0초 초과~30초. DNS·리다이렉트·다운로드 전체 작업 제한 |
| `max_download_bytes` | 기본 262144바이트, 1~1048576. 최종 응답 본문 제한 |
| `max_return_chars` | 기본 20000문자, 1~262144. 텍스트와 제목 각각의 상한 |
| `max_redirects` | 기본 3회, 0~5. 최초 요청 외 추가 요청의 최대 횟수 |
| `PageTextResult.requested_url` | 입력 URL 그대로 |
| `final_url` | 실제 응답을 받은 정규화 URL. IP로 대체한 연결용 URL이 아님 |
| `status_code`, `content_type` | 실제 2xx 상태 및 정규화 MIME |
| `title` | 실제 첫 HTML title 또는 `None`; 일반 텍스트에는 `None` |
| `text`, `truncated` | 추출 텍스트와 반환 문자 제한으로 본문/제목이 잘렸는지 여부 |

수치는 기존 `ServerPolicy`의 5초·256KiB를 참고한 **C 내부 기본값**이다.
공통 Control 정책이나 팀 전체 권한 계약으로 확정한 값이 아니다.
다운로드 초과는 오류이며 부분 성공으로 돌려주지 않는다. 정상 수신 후에만
문자 제한을 적용한다. `Content-Length`가 없거나 작게 선언되어도 누적 raw 바이트를
검사한다. 크게 선언된 경우에는 본문을 읽기 전에 거절한다.

모델 생성 단계의 타입·범위·추가 필드 오류는 `ValidationError`다.
함수 실행 중 URL 의미·요청 상한 검사는 `PageTextError(INVALID_INPUT)`이다.
URL/본문/제목은 모델 repr에서 숨긴다. 정상 결과에는 출처 URL의 query와 본문이
보존되므로 결과 전체를 로그에 출력하지 않아야 한다.

## URL·네트워크 접근

- HTTPS GET만 허용한다. scheme/host 대소문자, IDNA, 기본 포트 443 및 host의
  마지막 점을 정규화한 후 **host와 port의 정확한 조합**을 비교한다.
- allowlist는 origin 단위이며 그 origin의 모든 path/query를 허용한다.
  하위 도메인·문자열 prefix·wildcard는 지원하지 않는다. 경로별 정책은 후속 검토다.
- userinfo, 잘못된 포트, 제어문자/공백, 역슬래시, 잘못된 percent escape,
  host의 percent escape/IPv6 zone, 비어 있지 않은 fragment를 거절한다.
- IP literal과 모든 DNS 응답을 검사한다. non-global·사설·loopback·link-local·
  multicast·reserved 주소, mapped IPv6·6to4·Teredo·표준 NAT64 대역을 거절한다.
  공개/비공개 주소가 섞인 DNS 응답도 거절한다. 허용 origin이어도 예외가 없다.
  Python 버전별 특수 대역 판정 차이를 줄이기 위해 IPv4 `192.0.0.0/24`,
  `192.88.99.0/24`도 제외한다. IPv6는 `2000::/3`에 한정하고 `2001::/23`,
  `3fff::/20`을 추가 제외하므로 일부 특수 용도의 공개 주소도 보수적으로 거절한다.
- 첫 검증 IP를 선택하고 그 **숫자 IP로 실제 HTTPX 요청을 생성**한다.
  Host 헤더와 `sni_hostname`은 원래 호스트를 유지하고 TLS 인증서 검증을 켠다.
  원래 hostname으로 연결을 다시 시도하거나 다른 주소로 자동 fallback하지 않는다.
- 각 hop마다 새 `AsyncHTTPTransport`를 연다. proxy·인증·cookie jar·환경변수·
  HTTP/2·재시도·자동 redirect를 사용하지 않는다. 직접 transport를 사용하므로
  `AsyncClient`의 URL 포함 INFO 요청 로그도 만들지 않는다.
- HTTPcore DEBUG 진단은 응답 헤더를 로그에 기록할 수 있으므로 기본 transport는
  관련 DEBUG logger가 켜져 있으면 `connection_error`로 거절한다. 전역 logger를
  변경하지 않는다. 실행 중 외부 코드가 logging 설정을 바꾸거나 별도 wire logger를
  주입하는 환경은 이 기능의 지원 경계 밖이다.

이것은 단순 DNS 사전 검사보다 강한 **애플리케이션 수준 IP 고정 경로**다.
OS 라우팅·VPN·투명 proxy·운영자 정의 주소 변환이나 네트워크 격리를 검증하지
않는다. 시스템 egress 방화벽을 대체하거나 완전한 SSRF 방어를 보장하지 않는다.
숫자 IP 연결과 원래 hostname TLS 검증이 보장되지 않는 배포 경로는 활성화하면 안 된다.

`resolver`와 `transport_factory`는 **신뢰된 생성 코드/테스트 전용** DI다.
임의 transport가 프록시 사용·재해석 등을 하면 기본 경로의 보장을 잃는다.
Agent 인자나 운영자 미승인 플러그인에 이 교체 지점을 노출하지 않는다.

## 리다이렉트·시간·정리

301/302/303/307/308만 수동 처리한다. 상대 Location은 현재 출처 URL 기준으로
해석한다. 단일 Location이 없거나 형식이 잘못되면 `redirect_error`다.
다음 목적지의 origin·DNS/IP를 모두 검사한 뒤 요청한다. 동일 URL 반복과 횟수
초과를 거절한다. fragment만 다른 이동은 지원하지 않는다.

리다이렉트 본문은 읽지 않고 응답을 닫는다. 쿠키·Authorization·Referer를 다음
hop에 전달하지 않는다. DNS를 각 hop에서 새로 검사하므로 같은 hostname의 DNS가
비공개 주소로 바뀌면 다음 요청을 거절한다. 전체 `asyncio.wait_for`와 HTTP I/O
timeout을 함께 적용한다. 응답은 성공·거절·다운로드 취소 시 `finally`에서 닫힌다.
취소된 시스템 DNS 작업의 내부 thread가 종료될 때까지 기다리지는 않지만 그 결과로
후속 HTTP 요청을 실행하지 않는다. 동기 HTML 파싱은 다운로드 상한으로 작업량을
제한하며 실행 중 선점할 수 있는 CPU 격리 기능은 아니다.

## 형식·인코딩·텍스트 추출

- MIME은 `text/html`, `text/plain`만 지원한다. Content-Type 누락·중복도 거절한다.
- charset은 UTF-8(미지정 기본), ASCII, ISO-8859-1, Windows-1252, EUC-KR,
  CP949와 Python codec에서 같은 이름으로 정규화되는 별칭을 지원한다.
  다른 charset, 중복 charset, 잘못된 바이트는 `unsupported_encoding`이다.
  HTML meta charset을 추측하지 않으며 UTF-16/32를 지원하지 않는다.
- `Accept-Encoding: identity`를 보내고 gzip/br/deflate 등 identity 외 응답을
  **디코딩 전에 거절**한다. 압축 폭탄을 풀지 않는다. raw 다운로드 바이트가 이미
  제한되어 있고 지원 charset의 Unicode 출력도 제한된 입력에서 생성된다.
- 표준 라이브러리 `HTMLParser`로 markup·comment·속성 값과
  script/style/template/noscript/iframe/object/svg 내용을 제외한다.
  HTML entity는 텍스트로 해석하고 블록 경계·공백을 정리한다.
  일반 텍스트는 BOM만 제거하고 공백/줄바꿈을 보존한다.
- 렌더링된 화면의 재현이 아니다. CSS visibility/accessibility tree를 계산하지
  않으며 잘못된 HTML의 복구 동작도 브라우저와 다를 수 있다. 링크·이미지·외부
  stylesheet를 추가 요청하지 않는다. 텍스트 내 악성 지시도 실행되지 않은 자료다.

## 오류

`PageTextError.code`는 아래 enum이고 메시지는 고정 `page_text: <code>`이다.
upstream 예외 chain은 출력에서 숨기며 raw 응답·URL·비밀값을 메시지에 넣지 않는다.
Pydantic `ValidationError.errors()`에는 입력이 포함될 수 있으므로 이를 통째로
기록하지 않는다. 디버거의 traceback local-variable 수집도 별도 통제가 필요하다.

| code | 조건 |
|---|---|
| `invalid_input` | URL 오류, 요청별 문자 제한이 서버 상한 초과 |
| `access_denied` | 빈 allowlist, origin 불일치, 비공개/금지 IP |
| `dns_error` | DNS 실패·빈 응답·잘못된 주소 |
| `connection_error` | TLS·연결·프로토콜 실패 또는 비밀 헤더 노출 가능한 DEBUG 설정 |
| `timeout` | DNS/연결/읽기/전체 작업 시간 초과 |
| `http_error` | 최종 비2xx 응답 또는 비정상 Content-Length |
| `redirect_error` | Location 오류, 반복·최대 횟수 초과, HTTPS가 아닌 이동 |
| `unsupported_format` | 지원하지 않거나 누락된 MIME |
| `unsupported_encoding` | charset·디코딩·압축 정책 위반 |
| `too_large` | 선언 크기 또는 실제 raw 다운로드 상한 초과 |

## 테스트

```sh
.venv/bin/pytest tests/unit/test_page_text.py -q
.venv/bin/pytest tests/integration/test_page_text_transport.py -q
.venv/bin/pytest tests/integration/test_page_text_mcp.py -q
.venv/bin/pytest -q
.venv/bin/ruff check src tests examples
.venv/bin/mypy src/redteam tests
git diff --check
```

단위 테스트는 fake resolver, `httpx.MockTransport`, 제한된 `AsyncByteStream`을 사용한다.
외부 사이트·DNS·검색 API·LLM·MCP를 호출하지 않는다. 정책 거절 시 transport
미호출, 실제 구성된 IP/Host/SNI, redirect·cookie 격리, 크기·시간·정리·출력·
비밀값 미노출을 검사한다. 단위 테스트 통과만으로 실제 TLS 연결·네트워크 격리·
MCP 호출·Agent 사용이 검증되는 것은 아니다. 외부 서비스가 필요한 기존 opt-in
검사는 활성화하지 않는다.

별도 로컬 통합 테스트는 임시 인증서를 쓰는 loopback HTTPS 서버와 실제
HTTPX/httpcore TCP·TLS 경로를 사용한다. 테스트 전용 소켓 경계에서 검증된 공개 IP를
`127.0.0.1`로 매핑하므로 외부 사이트를 호출하지 않는다. 백엔드가 받은 IP,
서버가 받은 Host/SNI, 신뢰·호스트명 검사, 리다이렉트 재검사 및
성공·HTTP 오류·본문 timeout·취소 뒤 연결 종료를 확인한다. 이 매핑은 테스트에만
있으며 운영 배포의 라우팅·네트워크 격리 검증을 대신하지 않는다.

MCP 통합 테스트는 공식 Client를 통한 in-process 성공·거절·입력 오류·timeout,
실제 로컬 stdio 자식 프로세스의 discovery·성공·거절, Gateway의 operation·schema·
Action/Invocation ID 보존을 확인한다. in-process 성공 페이지는 Mock HTTP
transport로 제공한다. 별도 stdio 성공 테스트는 실제 로컬 HTTPS 서버에서 제목과
본문을 받아 output schema로 검사한다. 테스트 전용 자식 프로세스의 최종 소켓
경계에서만 검증된 공개 IP를 loopback으로 매핑하며, 운영 설정·제품 서버에는 이
허용 경로가 없다. 실제 외부 사이트는 읽지 않는다. Agent 전체 실행이나 Control
ALLOW 경로 검증은 아니다.

### 2026-10-02 연결 검증 결과

- 기존 페이지 읽기 단위 테스트: **111 passed**.
- 신규 MCP 통합 테스트: 로컬 HTTPS·stdio 성공 사례 추가 후 **7 passed**.
- 전체 pytest: 성공 사례 추가 전 **2065 passed, 32 skipped**. 새 사례는 위
  MCP 통합 테스트 파일 전체 실행으로 검증했다. 외부 서비스 opt-in 없음.
- `ruff check src tests examples`, 변경 파일 mypy, 공백 검사: 통과.
- 전체 mypy: 현재 **94 errors / 15 files**. 같은 가상환경에서 병합 HEAD
  `25846c0`을 별도 임시 경로에 풀어 검사한 결과도 **94 errors / 15 files**이며
  오류 내용·위치가 정확히 같다. 사용자 환경에서 보고한 병합 기준 58개 오류와
  이 실행 환경의 수치가 다르므로 이를 새로운 변경 오류로 세지 않는다.

샌드박스에서 기존 통합 테스트 비동기 대기와 로컬 stdio 대기가 멈춰, 전체 pytest는
승인된 샌드박스 밖에서 로컬 검사로 실행했다. 외부 서비스를 활성화하지 않았다.

## 후속 연결과 계약 검토

1. PM/Core·Control·Agent와 최종 capability/operation/Target·위험도를 합의한다.
   공개 지식 조회의 외부 전송·origin/path/query 허용 정책은 시험 대상 scope와
   구별한다. `UrlTarget` 재사용 가능성을 먼저 검토하고 검색 결과를 시험 대상으로
   자동 추가하지 않는다.
2. MCP 노출·Capability settings/bootstrap/Registry/Gateway 연결은 별도 독립
   테스트로 검증한다. 실제 Agent 후보 생성이나 Control 승인을 뜻하지 않는다.
3. 공통 Adapter·Gateway가 기존 `ToolResult`로 정규화하고 invocation_id,
   action_id 및 실행 context의 run/engagement correlation을 유지한다.
4. capability namespace, lower_snake_case operation,
   `capability.operation` Action Key, `namespace.remote_tool_name` ToolId를
   구별한다. Agent는 ActionProposal→Control→Gateway 경로를 사용한다.
5. 실제 MCP 호출은 독립적으로 검증한다. Agent 후보 노출·선택·승인·실행은
   별도 단계다.
   웹검색 공급자·Playwright·EIP/GitHub/Burp는 이번 완료 조건에 포함하지 않는다.
6. 대형 원문 보존·Artifact/Evidence 연결과 보존/접근 권한은 A·Evidence 담당과
   후속 합의한다. 현재 원문은 메모리에서 읽고 제한된 출력만 반환한다.

### Contract Change Proposal — 필요성 판단 보류

- 현재 Contract: `TargetRef.UrlTarget`, `ActionProposal`, `ToolInvocation`,
  `ToolDescriptor`, `ToolResult`, `ToolCatalog`/`ToolGateway`.
- 문제: 공개 자료 URL과 승인된 시험 대상의 정책 의미가 다르다.
- 필요한 변경: 내부 기능에는 없음. 공개 조회를 기존 Target+operation+정책의
  조합으로 표현 가능한지 먼저 합의한다. 불가능하다는 근거가 생길 때만 별도
  Core 제안으로 구체화하며 새 capability/Target/필드를 여기서 확정하지 않는다.
- 영향 모듈: 후속 Control, Agent 후보 생성, Capability 등록 및 app 조립.
- 호환성 영향: 현재 변경 없음. 기존 계약의 additive 변경 여부도 아직 미결정.
- 대안: 기존 UrlTarget과 기존 ToolResult를 유지하고 구현 계층 Adapter 및
  operation별 Control 정책으로 처리. 결과 정규화에는 새 Core Port가 필요 없다.
