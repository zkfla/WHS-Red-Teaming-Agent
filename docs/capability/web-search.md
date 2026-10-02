# 웹검색 내부 경계

`redteam.capability.servers.web_search`는 공개 기술 자료를 찾기 위한 검색어의
외부 전송 전 검사와 공급자 결과 정규화만 수행한다. 현재 검색 공급자는 선정되지
않았고, 실제 API·SDK·MCP·Registry·Bootstrap 연결도 없다. `WebSearchReader.search()`는
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
양끝 공백을 제거하며 비어 있으면 `None`으로 둔다. URL 호스트에서 `source`를
추측하지 않는다. 제목·snippet은 설정 길이에서 잘라 `truncated=True`로
표시한다. 결과 수는 요청·설정 중 작은 상한을 적용한다. 공급자 응답의
모든 URL·텍스트를 먼저 검증하고, 반환 JSON이 바이트 상한을 넘으면 끝 항목을
온전한 단위로 제거해 `truncated=True`를 반환한다. 빈 목록조차 상한에
들어가지 않으면 `output_too_large`다. 정상적인 공급자 빈 목록은 성공이며
`items=[]`, `truncated=False`다.

`SearchResult`는 `provider`, UTC `retrieved_at`, `items`, `truncated`만 담는다.
`retrieved_at`은 조회 시각이지 문서 게시 시각이 아니다. 공급자 객체나 SDK 응답은
상위 계층으로 넘기지 않는다. 검색 결과는 비신뢰 관측 자료로, Scope 추가,
지시 실행, PoC 다운로드·실행, 취약점 해석 또는 Finding 승격에 사용하지 않는다.

최종 JSON 크기 제한은 공급자 HTTP 원문 다운로드 제한이 아니다. 실제 공급자
Adapter를 붙일 때 streaming 수신 크기와 네트워크 경계도 별도로 제한해야 한다.

## 오류와 시간 경계

`SearchError.code`는 고정 enum이다. 입력 모델 검증은 Pydantic 오류를 낸다.

| 코드 | 의미 |
|---|---|
| `invalid_input` | Reader에 잘못된 요청 객체 전달 |
| `external_search_disabled`, `sensitive_query` | 외부 전송 전 거절 |
| `authentication_failed`, `rate_limited`, `network_error` | 향후 Adapter가 분류해 전달할 공급자 오류 |
| `timeout` | 비동기 호출 제한 초과 |
| `invalid_provider_response` | 잘못된 응답 모델·URL·텍스트·공급자 ID |
| `output_too_large` | 최소 JSON 결과도 반환 크기 상한 초과 |
| `provider_error` | 분류되지 않은 공급자 실패 |

`asyncio.wait_for`가 공급자 호출 및 결과 정규화 경계를 제한한다. 외부 취소는
`CancelledError` 그대로 전파한다. 공급자가 취소에 협조하지 않거나 동기 작업을
계속 수행하면 강제 종료까지 보장하지 않는다. 자동 재시도는 없다.

## 테스트와 후속 연결

```sh
pytest -q tests/unit/test_web_search.py
ruff check src/redteam/capability/servers/web_search.py tests/unit/test_web_search.py
mypy src/redteam/capability/servers/web_search.py tests/unit/test_web_search.py
```

Fake 테스트는 정상·빈 응답, 전송 거절 시 0회 호출, 오류 분류, timeout·취소,
URL·텍스트 검증, 결과·UTF-8 JSON 제한, 추가 DNS 조회 부재를 확인한다.
실제 외부 검색 성공, 공급자 인증 및 MCP 호출 검증은 아니다.

공급자 선정 후 API 인증정보의 서버 보관 방식, endpoint·egress, 원문 streaming
상한, 요금·rate limit, 개인정보 처리 조건을 검토해야 한다. MCP 연결 시에는
최종 capability·operation·ToolId·Target·위험도와 공개 지식 조회의 Control
정책을 PM/Core·Control·Agent와 합의한다. 현재 Core Contract/Port를 바꾸지
않았으며, 이번 내부 구현을 이유로 검색 결과에 실행 권한을 주지 않는다.
