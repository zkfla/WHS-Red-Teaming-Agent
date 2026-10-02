# 웹검색 내부 경계

`redteam.capability.servers.web_search`는 공개 기술 자료를 찾기 위한 검색어의
외부 전송 전 검사와 공급자 결과 정규화를 수행한다. 내부 Tavily 공급자 Adapter는
`redteam.capability.servers.tavily_search`에 구현했지만, 실제 API 호출은 opt-in
테스트 외에는 실행하지 않았다. MCP·Registry·Bootstrap 연결은 없다. `WebSearchReader.search()`는
내부 메서드 이름이며 최종 canonical operation이나 Agent 권한을 확정하지 않는다.

## 입력과 설정

`SearchRequest`는 `query`와 선택적인 `max_results`(기본 5, 1–10)를 받는다.
검색어는 양끝 공백을 제거한 뒤 1–500자여야 한다. 일반적인 내부 공백과 한글 등
Unicode 텍스트는 허용한다. 개행·탭, C0/C1 제어문자, Unicode format 제어문자,
줄·문단 구분자, surrogate는 거절한다. 알 수 없는 필드와 숫자 대신 문자열을 넣은 결과 수는
거절한다. 외부 입력 경계에서는 `parse_search_request()`가 Pydantic 검증 실패를
입력값 없는 `invalid_input` 오류로 바꾼다. 원문 검색어는 최종 결과에 반복하지 않는다.

`SearchConfig`는 서버 소유 생성자 인자로 주입한다. 환경변수·공통 settings와
연결하지 않았으며 요청 인자로 허용 범위나 상한을 변경할 수 없다.

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
Fake 공급자는 `tests/unit/test_web_search.py`에만 있고 운영 등록은 없다.
Tavily 공급자도 생성자 주입으로만 사용하며 공통 설정이나 운영 초기화에는 등록하지 않았다.

### Tavily 공급자 Adapter

[Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)는
`POST https://api.tavily.com/search`와 `Authorization: Bearer <token>`을 사용한다.
별도 SDK 없이 기존 HTTPX로 호출한다. endpoint, 헤더 이름, `search_depth=basic`,
`auto_parameters=false`, `include_answer=false`, `include_raw_content=false`,
`include_images=false`는 코드에서 고정한다. `max_results`는 Reader가 요청값과
서버 상한 중 작은 값을 넘기며, 공급자도 서버 상한을 다시 확인한다. 검색어만
검증된 `SearchRequest`에서 전달된다. API 키는 `TavilySearchProvider(api_key=..., config=...)`
생성자에 주입하고, 환경변수 `TAVILY_API_KEY`는 opt-in 테스트에서만 읽는다.
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
출처·언어·버전 선택과 관련성 평가는 후속 Agent 책임이다. MCP·Control·Agent
연결은 아직 완료되지 않았다.

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

운영 연결 전 API 키의 서버 보관 방식, endpoint egress, 계정 요금·rate limit,
개인정보 처리 조건을 검토해야 한다. MCP 연결 시에는
최종 capability·operation·ToolId·Target·위험도와 공개 지식 조회의 Control
정책을 PM/Core·Control·Agent와 합의한다. 현재 Core Contract/Port를 바꾸지
않았으며, 이번 내부 구현을 이유로 검색 결과에 실행 권한을 주지 않는다.
