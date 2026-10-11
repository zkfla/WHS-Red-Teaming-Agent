# Capability 추가 규칙

루트 `AGENTS.md`의 공통 원칙을 따른다. 담당 범위는 registry·MCP adapter·execution substrate다.

## 등록과 역할 선택

- 제공 모듈의 선언/metadata/factory를 기존 registration·registry·조립 경로에 연결한다. 도구를 추가할 때 중앙 Agent/Control에 이름별 분기를 만들지 않는다.
- 실제 MCP discovery schema와 finalized descriptor의 필수/선택 인자·target type·위험·출력 계약을 대조한다. 버전·allowlist·미등록 참조 불일치는 명확한 오류다.
- ToolCatalog 조회, 역할 선택, readiness, Control admission, ToolGateway 실행은 별개다. 발견·등록·health 확인만으로 에이전트의 실제 선택/실행 완료를 주장하지 않는다.
- 역할 manifest는 선택 정보이고 역할 정의가 아니다. 현재 공통 selector 의미·빈 선택·생략·alias 해석을 재사용하며 별도 선택 판정/허용 목록을 만들지 않는다.
- 도구에 mapper/legacy operation이 없다는 이유만으로 현재 직접 선택 계약에서 무조건 숨기지 않는다. binding mode에 맞는 실행/결과 경로를 검증한다.

## Adapter와 실행 환경

- 외부 서버의 이름·규격·응답 특성은 해당 adapter/제공 모듈이 소유한다. 일반 transport에는 제품별 동작을 넣지 않는다.
- transport 생명주기·initialize/discovery·timeout·취소·job·종료를 분리하고 현재 SDK 계약을 확인한다. stdout 프로토콜에 디버그/비밀값을 섞지 않는다.
- 실행 주소·명령·인터프리터·policy/credential/artifact 경로는 현재 설정/런타임에서 주입한다. 다른 개발자의 절대 경로, 고정 포트·실제 키를 넣지 않는다.
- fixture/mock 결과와 실제 서버 결과를 구분한다. health와 discovery뿐 아니라 허용된 제한 호출·원본 결과·증거/graph 연결까지 단계별로 검증한다.
- 관측한 host/port/protocol/scheme/status 등은 구조화하고 없는 값은 unknown으로 남긴다. 추정값을 관측 사실로 만들지 않는다.
- 실행의 재시도 안전성·자원 충돌·예상 부작용·실제 요청 쿼터를 metadata/실행 계약으로 제공한다. 한 MCP 호출이 여러 요청을 만드는 것을 숨기지 않는다.
- workspace·shell·코드 runner는 경로·권한·egress·자원 한도·취소·산출물 추적을 코드로 강제한다. 프롬프트 경고만으로 격리 완료라고 하지 않는다.
- 새 코드 생성 기능은 산출물 작성·로컬 시험·대상 실행을 분리한다. 검토된 artifact hash와 실행 명세 없는 범용 실행으로 Control을 우회하지 않는다.

소비자는 `core/ports`를 통해 catalog/gateway를 사용한다. 실제 서버 구현에 대한 연결/인스턴스 주입은 조립 지점이 담당하며 Agent가 adapter 구현체를 직접 생성하지 않는다.
