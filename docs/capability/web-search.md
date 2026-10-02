# 웹검색 내부 경계

`redteam.capability.servers.web_search`는 공개 기술 자료를 찾기 위한 검색어의
외부 전송 전 검사와 공급자 결과 정규화를 수행한다. 내부 Tavily 공급자는
`redteam.capability.servers.tavily_search`에 있다. opt-in stdio MCP 서버와
Capability Registry·Gateway 연결도 구현했다. 이 연결을 통한 실제 Tavily API
호출은 아직 검증하지 않았으며, Control·Agent 연결도 완료되지 않았다.
`WebSearchReader.search()`는 내부 메서드 이름이고, `web_search` operation은
이번 Capability 연결의 **제안 바인딩**이지 확정된 canonical operation이 아니다.

## 입력과 설정

`SearchRequest`는 `query`와 선택적인 `max_results`(기본 5, 1–10)를 받는다.
검색어는 양끝 공백을 제거한 뒤 1–500자여야 한다. 일반적인 내부 공백과 한글 등
Unicode 텍스트는 허용한다. 개행·탭, C0/C1 제어문자, Unicode format 제어문자,
줄·문단 구분자, surrogate는 거절한다. 알 수 없는 필드와 숫자 대신 문자열을 넣은 결과 수는
거절한다. 외부 입력 경계에서는 `parse_search_request()`가 Pydantic 검증 실패를
입력값 없는 `invalid_input` 오류로 바꾼다. 원문 검색어는 최종 결과에 반복하지 않는다.

`SearchConfig`는 서버 소유 설정이다. 독립 Reader 사용 시 생성자로 주입하고,
MCP 프로필 사용 시 운영자 JSON 파일 하나를 설정 검증과 서버 실행 양쪽에서
읽어 SHA-256으로 동일한 내용을 확인한다. 요청 인자로 활성화 여부나 상한을
변경할 수 없다.

| 설정 | 내부 기본값 | 검증 범위 |
|---|---:|---:|
| `external_search_enabled` | `False` | 명시적 bool |
| `max_results` | 10 | 1–10 |
| `timeout_seconds` | 10 | 0 초과–60초, 유한값 |
| `max_title_chars` | 300 | 1–1,000 |
| `max_snippet_chars` | 1,000 | 1–5,000 |
| `max_output_bytes` | 65,536 | 128–1,048,576 UTF-8 bytes |

이 값은 C 내부 초기값이며 공통 정책 확정값이 아니다. 실제 외부 전송에는
`external_search_enabled=True`가 필요하다. 비활성화 상태에서는 공급자를
호출하지 않고 `external_search_disabled`를 반환한다.

## 공급자 경계와 외부 전송 검사

Capability 내부 `SearchProvider` Protocol은 `provider_id`와
`async search(query, max_results) -> ProviderSearchResponse`만 요구한다. Reader는
공급자와 설정을 주입받고, 검사된 검색어 및 요청·서버 상한 중 작은 결과 수만
전달한다. 자동 공급자 선택, 재시도, fallback, 캐시, 페이지네이션은 없다.
Fake 공급자는 테스트 코드에만 있고 운영 등록은 없다. MCP 서버는 테스트에서
Fake를 주입할 수 있으며 운영 stdio 프로세스에서는 Tavily 공급자를 주입한다.

### Tavily 공급자 Adapter

[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)는
`POST https://api.tavily.com/search`와 `Authorization: Bearer <token>`을 사용한다.
별도 SDK 없이 기존 HTTPX로 호출한다. endpoint, 헤더 이름, `search_depth=basic`,
`auto_parameters=false`, `include_answer=false`, `include_raw_content=false`,
`include_images=false`는 코드에서 고정한다. `max_results`는 Reader가 요청값과
서버 상한 중 작은 값을 넘기며, 공급자도 서버 상한을 다시 확인한다. 검색어만
검증된 `SearchRequest`에서 전달된다. API 키는 `TavilySearchProvider(api_key=..., config=...)`
생성자에 주입한다. 운영 MCP 프로필은 부모 시작 환경의 `TAVILY_API_KEY`만
자식 stdio 프로세스에 전달한다. 기존 공급자 직접 호출 opt-in 테스트도 이 변수를 읽는다.
키가 없으면 요청을 보내지 않는다. 키나 endpoint를 Agent 인자로 받는 경로는 없다.

HTTPX client는 호출마다 새로 만들고 닫는다. 환경 proxy·인증 설정을 사용하지
않으며(`trust_env=False`), 리다이렉트를 자동 추적하지 않는다. 응답은 streaming으로
읽고 압축 해제 후 바이트 수를 센다. 원격 응답 기본 상한은 262,144 bytes,
주입 가능한 운영 상한은 1–1,048,576 bytes이며 검색 요청에서는 바꿀 수 없다.
`Content-Length`를 신뢰하지 않고 실제 수신량을 검사한다. Reader의 전체 작업
timeout 기본 10초가 공급자 호출·결과 정규화를 감싸고, HTTPX에도 같은 timeout을
적용한다. timeout·취소·오류에도 응답 stream과 client context를 닫는다. 자동
재시도는 없으며, 공급자가 취소에 협조하지 않는 작업을 강제 종료하진 못한다.

Tavily `results[].title`, `results[].url`, `results[].content`만 내부
`title`, `url`, `snippet`에 매핑한다. `content`는 검색 결과 설명이며 직접 읽은
페이지 원문이 아니다. 공급자가 `source`를 제공하지 않으면 `None`으로 두고,
점수·게시 날짜·출처를 만들어내지 않는다. `score`는 증거 신뢰도나 취약점
확신도가 아니다. 반환 URL을 자동 방문하거나 다운로드하지 않는다.
[API 크레딧 문서](https://docs.tavily.com/documentation/api-credits)에 따르면 basic
검색은 현재 1 API credit이고 advanced는 2 credits이다. 이 Adapter는 basic을
고정하고 `auto_parameters`를 끈다. 실제 비용·한도는 계정과 최신 약관을 확인해야
한다. 결제·플랜·충전 작업은 구현하지 않았다.

전송 전에는 `Authorization:`, `Cookie:`, `password=`, `token:`,
`api_key=` 계열 key/value와 `Bearer <값>`의 명확한 패턴을 거절한다. 값을
가리고 전송하지 않으며, 검사를 끄는 요청 옵션도 없다. 오류 문자열에는
검색어·공급자 예외 문자열을 넣지 않는다. 이 검사는 완전한 DLP가 아니다.
계좌정보·개인정보·비공개 제품정보의 자유 텍스트 탐지와 최종 외부 전송 허용
정책은 공급자 선정 및 Control 협의 후 별도로 필요하다.

## 공급자 응답과 반환

공급자는 `ProviderSearchResponse` 안에 `ProviderSearchItem` 목록을 반환한다.
응답 모델은 최대 100개 항목, 원본 title 10,000자, snippet 50,000자,
source 300자, URL 8,192자를 허용한다. 이 상한을 넘는 원본은 잘못된
공급자 응답으로 분류한다. 실제 Adapter에서는 수신 중 원문 크기를 따로 제한해야 한다.
각 항목의 필수 `url`은 자격증명 없는 절대 `http`/`https` URL이어야 한다.
호스트·포트·제어문자·백슬래시·잘못된 percent escape를 검사한다. URL은
참고 링크이며 DNS 조회·본문 읽기·리다이렉트 추적을 하지 않는다. 따라서
방문 후 확인한 최종 URL이나 안전한 공개 목적지라고 주장하지 않는다.

선택 필드 `title`, `snippet`, `source`는 공급자가 준 값만 사용한다.
`snippet`의 LF·CR·TAB과 연속된 일반 공백은 한 칸으로 접은 뒤 양끝 공백을
제거한다. 따라서 공급자가 반환한 여러 줄 설명은 한 줄의 짧은 설명이 된다.
이 정규화만으로는 `truncated`를 켜지 않고, 정규화 결과가 비어 있으면 `None`이다.
그 밖의 금지 제어문자는 여전히 거절한다. 제목·출처, 검색어 입력, URL의
제어문자 검사에는 이 예외를 적용하지 않는다. URL 호스트에서 `source`를
추측하지 않는다. 제목·snippet은 정규화 뒤 설정 길이에서 잘라 `truncated=True`로
표시한다. 결과 수는 요청·설정 중 작은 상한을 적용한다. 공급자 응답의
모든 URL·텍스트를 먼저 검증하고, 반환 JSON이 바이트 상한을 넘으면 끝 항목을
온전한 단위로 제거해 `truncated=True`를 반환한다. 빈 목록조차 상한에
들어가지 않으면 `output_too_large`다. 정상적인 공급자 빈 목록은 성공이며
`items=[]`, `truncated=False`다.

`SearchResult`는 `provider`, UTC `retrieved_at`, `items`, `truncated`만 담는다.
`retrieved_at`은 조회 시각이지 문서 게시 시각이 아니다. 공급자 객체나 SDK 응답은
상위 계층으로 넘기지 않는다. 검색 결과는 비신뢰 관측 자료로, Scope 추가,
지시 실행, PoC 다운로드·실행, 취약점 해석 또는 Finding 승격에 사용하지 않는다.

최종 JSON 크기 제한과 공급자 HTTP 응답의 수신 제한은 별도다. Tavily Adapter는
압축 해제 후 응답을 별도로 제한한다. 압축 상태의 전송량 상한이나 범용 egress
정책은 이 내부 Adapter의 보장 범위가 아니다.

## 오류와 시간 경계

`SearchError.code`는 고정 enum이다. 입력 모델 검증은 Pydantic 오류를 낸다.

| 코드 | 의미 |
|---|---|
| `invalid_input` | Reader에 잘못된 요청 객체 전달 |
| `external_search_disabled`, `sensitive_query` | 외부 전송 전 거절 |
| `authentication_failed`, `rate_limited`, `credits_exhausted`, `network_error` | Tavily의 401, 429, 432·433, 네트워크 오류 |
| `timeout` | 비동기 호출 제한 초과 |
| `invalid_provider_response` | 잘못된 응답 모델·URL·텍스트·공급자 ID |
| `output_too_large` | 최소 JSON 결과도 반환 크기 상한 초과 |
| `provider_response_too_large` | Tavily 원격 응답 다운로드 상한 초과 |
| `provider_error` | 분류되지 않은 공급자 실패 |

Tavily의 400·422·5xx 및 예상하지 못한 HTTP 상태는 `provider_error`, 잘못된
JSON·schema·MIME은 `invalid_provider_response`다. 오류 본문·전체 요청 URL과
HTTPX 예외 문자열은 사용자 오류에 싣지 않는다. timeout과 취소는 구분하며,
432·433은 429와 구분한다. [공식 응답·오류 예시](https://docs.tavily.com/documentation/api-reference/endpoint/search)를
기준으로 분류했다.

`asyncio.wait_for`가 공급자 호출 및 결과 정규화 경계를 제한한다. 외부 취소는
`CancelledError` 그대로 전파한다. 공급자가 취소에 협조하지 않거나 동기 작업을
계속 수행하면 강제 종료까지 보장하지 않는다. 자동 재시도는 없다.

## 테스트와 후속 연결

### opt-in MCP 및 Gateway 연결

`web-search` 서버 ID는 기본 등록되지 않는다. 운영자가 `.env.example`의 주석
예시를 실제 시작 환경에 적용하고 `REDTEAM_MCP_SERVERS=web-search`를 지정해야
한다. 절대 경로의 JSON 파일(`REDTEAM_MCP_WEB_SEARCH_SEARCH_CONFIG`)은
`SearchConfig` 필드만 가지며, 다음 최소 예시는 외부 전송을 **명시적으로** 켠다.
파일이 없거나 `external_search_enabled`가 기본값 `false`이면 공급자 호출은
거절된다. 키는 JSON 파일에 넣지 않고 서버 시작 환경의 `TAVILY_API_KEY`로만
전달한다. 활성 설정에 키가 없으면 bootstrap 단계에서 거절한다.

```json
{"external_search_enabled": true, "max_results": 5}
```

`MCPServerSettings.from_environment("web-search")`는 `web_search_tavily` 프로필의
stdio·`web_api` kind·명시적 risk level·도구 allowlist·operation 바인딩을
검사한다. 운영 설정 파일 하나를 읽고 해시를 자식 서버 실행 인자로 전달하므로
discovery와 서버 실행의 설정이 달라지면 실패한다. 자식 프로세스에는 키 변수만
추가 전달하며 일반 환경의 proxy·인증값을 그대로 전달하지 않는다. stdio
stdout은 MCP 프로토콜 전용이고 SDK 진단 로그는 stderr로 보낸다.

등록 흐름은 `web_search_mcp.create_server()` → MCP discovery →
`web_search_manifest.validate_web_search_discovery()` → `MCPToolRegistry` →
기존 `WebAPIMCPAdapter` → `MCPToolGateway.invoke()`다. 원격 도구 이름은
`web_search`, ToolId는 `web.web_search`다. 제안 operation도 `web_search`지만
operation은 의도, ToolId는 실행 도구를 가리키는 별개 식별자다. 이 바인딩은
현재 Agent naming 검증이나 Control 승인 정책의 합의를 대신하지 않는다.
검색어는 기존 TargetRef 종류로 표현하지 않았고 descriptor의 target_types는
빈 목록이다. 공급자 API endpoint를 시험 대상 Target으로 간주하지 않는다.

MCP 입력은 `query`(1–500자 문자열, 필수), `max_results`(1부터 운영자
`SearchConfig.max_results`까지의 정수, 기본 5 또는 더 작은 서버 상한)다.
SDK의 기본 도구 모델이 추가 필드를 무시할 수 있어 서버 호출 경계에서 기존
`SearchRequest`로 다시 검증하고, discovery schema에도 추가 필드 금지를
표시한다. 나머지 설정, 키, endpoint, 유료 옵션은 도구 입력에 없다. 출력은 기존
`SearchResult` JSON schema 그대로 `provider`, UTC `retrieved_at`,
`items[{title,url,snippet,source}]`, `truncated`다. `snippet`은 Tavily가
반환한 설명이며 페이지 원문이 아니다. 입력·전송·공급자 오류는 기존 고정
`SearchError.code`를 MCP `ToolError`에 싣고, Gateway에서는 실패한
`ToolResult`가 된다. 원문 검색어·키·원격 오류 본문은 오류 메시지에 담지 않는다.

독립 Fake 검증에는 외부 검색 계정이 필요 없다. 다음 명령은 in-process
discovery/call, Registry·Gateway의 schema와 ID 유지, 로컬 stdio 자식
프로세스 호출을 검증한다. 운영 서버의 실제 stdio 진입점도 비활성 설정·키 없음
상태로 실행해 외부 호출 전에 거절되는지 확인한다. stdio 테스트는 환경의
프로세스 실행 권한이 필요하다.

```sh
.venv/bin/pytest -q tests/integration/test_web_search_mcp.py
```

운영자가 실제 Tavily 경로를 **한 번** 검증할 때만 키를 사전에 비공개 시작
환경에 설정하고 아래 명령을 실행한다. 이 opt-in 테스트는 임시 운영 설정을
만들어 실제 stdio MCP 서버 → Adapter/Gateway → Tavily → ToolResult를
호출한다. 공개 검색어 `OWASP API1:2023 "Broken Object Level Authorization"`로
최대 5개를 요청하고, discovery 출력 schema·ID·결과 개수·JSON 크기를 검사한다.
키나 opt-in이 없으면 skip하며 일반 pytest에서 외부 호출하지 않는다. 이 문서
작성 및 Capability 연결 작업에서는 실제 Tavily MCP 요청을 실행하지 않았다.

```sh
REDTEAM_TAVILY_MCP_LIVE=1 .venv/bin/pytest -q tests/integration/test_web_search_mcp_live.py
```

서버를 독립 실행해 MCP 클라이언트로 discovery할 때는 같은 JSON 파일의 SHA-256
값을 `--sha256`에 전달한다. stdout을 사람이 읽는 로그로 취급하지 않는다.

```sh
.venv/bin/python -m redteam.capability.servers.web_search_mcp --config /absolute/path/to/web-search-policy.json --sha256 CONFIG_SHA256_HEX
```

### 검색 품질 확인 CLI

`TAVILY_API_KEY`가 이미 환경변수로 설정된 환경에서 다음 명령으로 공개 검색어
하나를 조회할 수 있다. 이 명령은 실제 Tavily API 호출을 **한 번** 수행하므로
실행 시 계정 크레딧이 사용될 수 있다. 키 값을 명령줄이나 문서에 적지 않는다.

```sh
python examples/tavily_search_quality.py "OWASP API Security BOLA official documentation"
```

이 예제는 `SearchRequest` 기본값과 서버 설정으로 최대 5개 결과를 반환하며,
기존 `WebSearchReader`의 민감 검색어 거절·결과 정규화·크기 제한을 그대로
사용한다. 제목·URL·설명과 Reader 호출 소요 시간을 출력하고, 오류는 고정
코드로만 표시한다. 결과 순서나 관련성 판단은 자동 검증하지 않는다. MCP나
Agent 실행 경로도 아니다.

### 초기 검색 품질 확인 결과

아래는 운영자가 WSL의 실제 Tavily 실행 로그에서 확인해 전달한 초기 관찰이다.
이번 문서 갱신에서는 API를 다시 호출하지 않았다. CLI → `SearchRequest` → Reader → Tavily
요청 JSON에서 검색어가 정확히 전달되고, 결과 항목의 제목·URL·설명이 서로
섞이지 않는다는 점은 Fake HTTP 테스트로 확인한 별도 사실이다.

| 실제 검색어 | 소요 시간 | 상위 5개 내 공식 자료 | 관찰 |
|---|---:|---|---|
| `OWASP API Security BOLA official documentation` | 0.66초 | 없음 | 인물·무기·게임 등 보안 주제와 무관한 결과 |
| `Android Network Security Configuration official documentation` | 1.96초 | Android 공식 문서 2위, OWASP 자료 4위 | 관련 자료가 반환됨 |
| `Playwright MCP GitHub microsoft` | 1.70초 | Microsoft의 playwright-mcp 저장소 2위 | 관련 공식 자료가 반환됨 |
| `OWASP API1:2023 "Broken Object Level Authorization"` | 1.40초 | OWASP 공식 목록 3위, 해당 취약점 공식 문서 5위 | 전체 용어로 검색했을 때 관련성이 개선됨 |
| `site:owasp.org "Broken Object Level Authorization" API Security` | 2.45초 | 상위 5개 모두 OWASP 자료 | 포르투갈어·프랑스어·영어와 2019·2023 버전 혼재 |

소요 시간은 CLI에 표시된 단일 실행의 `Elapsed` 값이며 반복 측정 평균이나
성능 보장값이 아니다. 이 다섯 건은 소규모 품질 확인이므로 결과 순위나
관련성이 다른 검색어·시점에서도 유지된다고 해석하지 않는다.

검색어를 구체화한 뒤 **이번 사례에서는** 관련성이 개선됐다. 이 관찰만으로
최초 결과의 원인을 확정하거나 Tavily의 전체 검색 품질을 보장하지 않는다.
공급자는 관련성이 낮은 결과를 코드에서 임의 교체하지 않는다. 검색어 재구성,
출처·언어·버전 선택과 관련성 평가는 후속 Agent 책임이다. 검색 품질 관찰 당시
MCP 연결은 없었으며 현재도 Control·Agent 연결은 완료되지 않았다.

### 테스트

```sh
pytest -q tests/unit/test_web_search.py
pytest -q tests/unit/test_tavily_search.py tests/integration/test_tavily_search_live.py
ruff check src/redteam/capability/servers/web_search.py src/redteam/capability/servers/tavily_search.py tests/unit/test_web_search.py tests/unit/test_tavily_search.py tests/integration/test_tavily_search_live.py
mypy src/redteam/capability/servers/web_search.py src/redteam/capability/servers/tavily_search.py tests/unit/test_web_search.py tests/unit/test_tavily_search.py tests/integration/test_tavily_search_live.py
```

실제 API 확인은 계정 키와 별도 opt-in을 모두 갖춘 격리 환경에서만 실행한다.
다음 명령은 **한 번의 Tavily 검색 요청**을 보낸다. 일반 `pytest`는 이 테스트를
skip하며, 키가 없을 때도 skip한다. 테스트는 공개 검색어
`OWASP API Security BOLA official documentation`과 결과 최대 5개만 사용한다.
결과 순서나 특정 URL은 고정하지 않는다. 키를 명령 인자·파일·로그에 적지 않는다.

```sh
REDTEAM_TAVILY_LIVE=1 pytest -q tests/integration/test_tavily_search_live.py
```

Fake 테스트는 정상·빈 응답, 전송 거절 시 0회 호출, 오류 분류, timeout·취소,
URL·텍스트 검증, 설명의 줄 공백 정규화와 기존 제어문자 거절, 결과·UTF-8 JSON
제한, 추가 DNS 조회 부재를 확인한다.
Mock 기반 테스트 성공은 실제 외부 검색·인증·MCP 호출 검증이 아니다.

운영에서 API 키 보관 방식, endpoint egress, 계정 요금·rate limit,
개인정보 처리 조건을 검토해야 한다. Control·Agent 연결 전에는 제안
operation·Target 표현·위험도와 공개 지식 조회 승인 정책을 PM/Core·Control·Agent와
합의한다. 현재 Core Contract/Port를 바꾸지 않았으며, 검색 결과에 실행 권한을
주지 않는다. Agent 실행에는 기존 ActionProposal → Control ALLOW →
ToolInvocation → Gateway 경로가 필요하다.
