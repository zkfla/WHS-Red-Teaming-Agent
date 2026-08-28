# 현재 구현 비판적 검토

## 이번 검토에서 수정한 결함

1. Cleanup이 필요한 상태 변경 action도 timeout이면 재시도될 수 있었다.
   이제 cleanup-required capability는 일시 오류여도 자동 재시도하지 않고 terminal 처리한다.
   실행 전에 `RESERVED` cleanup task가 남으므로 먼저 상태를 조정해야 한다.
2. cleanup task가 남은 run이 일반 실패 checkpoint로 돌아갈 수 있었다.
   이제 `BLOCKED`에서 멈추며 모든 cleanup receipt가 성공한 뒤에만 저장된 단계로 복귀한다.
3. Planner가 최신 다른 run의 페이지를 읽을 수 있었다.
   이제 현재 `exploration_run_id`의 page inventory만 계획 입력으로 사용한다.
4. XHR/fetch endpoint URL과 감사 로그에 일반 query 값이 남을 수 있었다.
   endpoint는 query 없는 canonical URL로 저장하고 모든 audit query 값은 마스킹한다.
5. endpoint에 method와 content-type만 있어 후속 Probe가 파라미터를 알 수 없었다.
   query 이름·타입과 body 필드·중첩 타입을 값 없이 병합하는 `api_request_schemas`를 추가했다.
6. actor 비교가 같은 HTTP client를 재사용하여 cookie가 계정 사이에서 섞일 수 있었다.
   환경변수 값은 메모리에서만 읽고 actor마다 별도 cookie jar를 갖는 `ActorSessionPool`로
   교체했다. 영속 action에는 actor 준비 상태와 누락된 환경변수 이름만 저장한다.
7. path와 schema 필드에서 객체 식별자를 추출하는 계층이 없었다.
   숫자·UUID·ULID·object-id path를 `{resource_id}`로 바꾸고 `id` 계열 query/body
   필드를 값 없이 저장하는 `ResourceRecord`를 추가했다.
8. 서로 다른 collection route를 동적 ID로 오인할 수 있었다.
   variant 추론은 공통 collection 뒤의 단일 변경 segment에만 제한하고 version/date
   segment는 제외했다.

## 여전히 부족한 부분

- `mcp_in_memory`는 MCP 프로토콜 경계를 시험하지만 OS 프로세스 격리는 제공하지 않는다.
- 원격 MCP용 비대칭 서명 receipt와 server-side 공유 budget/DNS 저장소가 없다.
- cleanup worker를 재시작 시 자동 구성하는 runtime registry와 운영 CLI가 없다.
- actor별 HTTP cookie jar는 분리됐지만 browser context/storage state는 아직 분리되지 않았다.
- 명시 fixture의 owner/non-owner relation과 bounded IDOR matrix는 구현했지만 자동 소유권
  학습은 아직 없다.
- OpenAPI import, XSS/SQLi oracle, canary, 상태 기반 workflow graph가 없다.
- IDOR fixture는 exact body fingerprint를 사용하므로 동적 필드가 있는 응답의 의미 기반
  비교는 아직 없다.

## 다음 구현 순서

1. actor identity endpoint와 actor별 browser context/storage state
2. OpenAPI import와 명시적 path parameter binding
3. 자동 ownership observation과 동적 JSON 필드 정규화 비교
4. 별도 MCP 프로세스 실행 구성 및 receipt/budget/DNS 재검증
5. 반사 canary 기반 XSS와 제한된 SQLi differential oracle

API schema만으로 취약점을 판단하지 않는다. 관찰된 필드 구조는 다음 Probe의 입력 후보일
뿐이며, actor 비교·반복 재현·negative control을 통과하기 전에는 Finding으로 승격하지 않는다.
