> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# Worker A 산출물: capability / MCP / 프로필 / 정책 준비·동작 의미·식별자·외부 연결

워커 A 담당: M01, M06, M08, M13, M14, M15, M16, M17, M25. 읽기 전용 조사.
저장소 ., 브랜치 dev, 기준 커밋 9eb70ed1 — `git rev-parse`로 직접 확인함.
표기: 역할어(정책을 판단하는 구성요소 등)로 서술하고, 코드 심볼은 출처 식별용으로만 괄호 병기한다. 큰 코드 인용 없음.

## 0. 전제 확인

- pyproject.toml: `pydantic-settings>=2.6,<3.0`, `pluggy>=1.6,<2.0` 의존성 선언 확인 (10~12행).
  코드 내 pluggy/BaseSettings 사용처는 없음 — `rg pluggy|pydantic_settings` 결과 pyproject 1건のみ. 즉 설치 예정 상태, 도입 위치는 미정.
- 공통 계약(core) + 앱 설정·조립 경계는 루트 담당. 아래 이관안에서 공통 계약 변경이 필요한 부분은 '루트 조율 제안'으로 분리 표기한다.
- 기본 거부 원칙: 모르는 동작·대상·도구 식별자는 계속 거절. 이름 목록 삭제 같은 무검증 개방은 제안하지 않는다.
- 등록은 실행 허가가 아니다: 새 선언이 후보 적격성을 넓혀도, 정책 판단·범위·승인·원본 증거 검사는 그대로 둔다.
- 참고 자료(스킬 문서 등)에 실행 제공자(팩토리·스키마·바인딩) 작성을 강제하지 않는다 — 등록 선언 필드는 실행 확장에만 필수.

## 0.1 실제로 읽은 파일 (이 문서의 인용 허용 목록)

- src/redteam/core/action_semantics.py (어휘 집합 + 검증 함수 전체)
- src/redteam/intake/models.py (1~90행: 초안 모델 + 어휘 복사본)
- src/redteam/app/cli/commands/validate.py (사전 점검: capability 고정 집합·allowlist 검사·서버 설정 검사)
- src/redteam/orchestration/paths/writing.py (1~90행: 생성 스키마 파생)
- src/redteam/orchestration/candidates/compiler.py (머리부 + 동작 분기 2건 + 대상값 바인딩 350~430행)
- src/redteam/orchestration/target_binding.py (1~120행: 주소 신원·인자 대조)
- src/redteam/orchestration/execution_bridge.py (머리부 + 고정 조리법 + 바인딩 195~280행)
- src/redteam/orchestration/nodes/execute.py (1~90행 재시도 안전 집합 + 210~260행 재시도 게이트)
- src/redteam/capability/settings.py (1~185행: 환경 설정 해석 + 프로필별 검사)
- src/redteam/capability/bootstrap.py (1~60행 + 245~320행: 어댑터 선택·기본 대상·게이트웨이 조립)
- src/redteam/capability/adapters/mcp/naming.py (전체: 식별자 생성 공식)
- src/redteam/capability/registry.py (60~100행: 서버별 교체·충돌 거절)
- src/redteam/capability/catalog.py (185~215행: 동작 후보 조회)
- src/redteam/capability/manifest.py (65~95행: 신원·어휘 검사)
- src/redteam/core/contracts/target.py (1~110행: 대상 합집합)
- src/redteam/control/scope.py (150~175행 해석 + 340~395행 자산 검사·범위 판단)
- src/redteam/core/contracts/policy.py (1~60행: 판단·문맥 계약)
- src/redteam/control/severity.py (전체: 꼬리표 생성·해석)
- src/redteam/orchestration/nodes/policy.py (30~80행: 별도 해석 + 판정 노드)
- src/redteam/control/roe.py (175~210행: 허용 목록 + 병렬 제한)
- src/redteam/control/rate_limit.py (90~125행: 동시성 검사)
- src/redteam/core/contracts/tool.py (도구 기술자 1~80행)
- src/redteam/core/enums.py (위험·심각도 정의, 검색으로 확인)
- src/redteam/orchestration/reporting.py (20~65행: 업무 갈래 분류)
- src/redteam/capability/servers/__main__.py (1~60행: 공용 실행기 역할)
- src/redteam/capability/deployment.py (50~95행: 배포 명세)
- src/redteam/app/mcp_deployment.py (115~160행: 배포 동기화 대조)
- 근거 문서: 2026-10-04 모듈화 감사(전수 구조 파악용), 2026-10-03 폐쇄 루프 계획(부분 검색),
  2026-10-03 검증 계약 감사(부분 검색), 카카오톡 받은 파일 dev-mcp-audit-20261004.md(부분 검색).
  dev-agent-audit 쪽은 경로 공백 인용 실패로 미열람 — 미검증으로 둔다.
## 1. 요약 표

| M | 현재 메커니즘(파일:심볼) | 분류 | 이관 소유자 | 예상 변경 파일 | 루트 결정 |
|---|---|---|---|---|---|
| M01 | 어휘 집합(action_semantics: CANONICAL_*) + 초안 복사본(intake/models) + 점검 복사본(validate:145) + 파생(writing:writing_schema) | 결함(복사본 2) + 정상(파생 1) | 공통 계약=루트, 소비=각 영역 | intake/models, app/cli/commands/validate (+루트: core 상수 공개 방식) | 필요(상수 공개 방식) |
| M06 | 동작별 분기(compiler:377 web_search/endpoint_discovery, target_binding:validate_network_arguments, execution_bridge:EXECUTION_OPERATIONS/bind_execution_arguments) | 결함 | 선언=확장 패키지, 검사=중앙 | compiler, target_binding, execution_bridge (+확장 선언 스키마 신설) | 필요(조리법 승인 방식) |
| M08 | 재시도 안전 집합(execute:_RETRY_SAFE_ACTIONS/_retry_safe) + 4조건 게이트(226행 부근 retry_enabled) | 운영 제약 | 선언=확장 패키지, 게이트=중앙 유지 | nodes/execute (+선언 필드 신설) | 필요(조건 표현 방식) |
| M13 | 환경 해석(settings:from_environment·프로필 검사) + 어댑터 선택(bootstrap:_create_adapter) + 기본 대상(_default_target_types) | 결함 | 묶음 선언=확장 패키지, 조립=앱 조립부(루트) | settings, bootstrap (+등록 레지스트리 신설) | 필요(등록 발견 방식) |
| M14 | 식별자 공식(naming:build_mcp_tool_id, 서버 미포함) + 충돌 거절(registry:replace_server_tools) + 능력 접두 필터(catalog:list_tools_for_action) + 명세 검사(manifest:_validate_identity) | 결함(공식) + 정상(거절·필터·검사) | 공식 변경=루트 조율, 선언=확장 | naming, registry, catalog, manifest | 필요(신원 형식+호환, 별도 결정) |
| M15 | 대상 합집합(contracts/target:AnyTarget 9종) + 해석(scope:_resolve_target) + 조건부 자산 검사(scope:361 부근) + 거절(380 부근) | 결함(닫힌 계약) + 정상(조건부 검사) | 선언=확장 패키지, 해석·거절=공통/통제(루트 조율) | control/scope (+선언 스키마 신설, core는 루트) | 필요(신원 인자 선언 의무 여부) |
| M16 | 꼬리표 운송(severity:to_tag/severity_from_matched_rules) + 별도 판독(nodes/policy:denial_severity) + 정의 일원화(core/enums:Severity) | 결함(이중 파서) + 운영 제약(동결 계약) | 운송·판독=공통 계약(루트), 생성=통제 | core/contracts/policy (+severity·nodes/policy, core/enums는 루트) | 필요(필드 승격 vs 공용 판독기) |
| M17 | 자유 문맥(policy:ControlContext.metadata) + 부재시 거절(roe:_check_max_parallel_actions) + 부재시 구경로(rate_limit:concurrency_exceeded) | 운영 제약(신규 제공자 계약 부재) | 신호 정의=공통(루트), 값 제공=호출부 | core/contracts/policy(루트) + roe·rate_limit + 호출부 | 필요(신호 위치·부재 의미) |
| M25 | 업무 갈래(reporting:_WORKSTREAM_FAMILIES) + 공용 실행기(servers/__main__:role 6종) + 배포 역할(deployment:DeploymentSpec.roles) + 동기화 대조(app/mcp_deployment:131 부근) | 운영 제약(P2) | 선언=각 제공자, 대조=중앙 유지 | reporting, servers/__main__, deployment, app/mcp_deployment | 필요(선언 출처) |

## 2. M01 — 동작 어휘 복사본

(1) 코드 확인. 정본은 동작 의미 계약(action_semantics)에 있다: 능력 4종 frozenset, 동작 20여종 frozenset,
검증 3종(validate_capability_name·validate_operation_format·validate_operation_name)과 조합키 생성(canonical_action_key).
초안 모델(intake/models)은 능력 튜플을 독자 상수로 재선언한다 — `CANONICAL_CAPABILITIES = ("recon", "web", "mobile", "analysis")`.
사전 점검(validate:145행 부근)은 허용 능력을 하드코딩 집합으로 대조한다 — `if capability not in {"recon", "web", "mobile", "analysis"}`.
반면 생성 스키마(writing:writing_schema)는 정본 집합을 읽어 enum을 주입한다 — 파생이라 복사본이 아니다.
(2) 감사 문서 일치. '입력 도우미 복사본·사전 점검 복사본 + 생성 스키마는 반례' 서술과 일치한다. 정정 없음.
다만 점검 쪽은 능력뿐 아니라 동작 키도 정본 함수(canonical_action_key)로 검사하는 이중 구조라, 능력 집합만 맞추면 끝이 아니다.
(3) 이관안. 정본 1곳 유지(루트 소유), 초안 모델·사전 점검은 정본을 import·호출로 전환: 초안 쪽 튜플 삭제 → 정본 집합 참조,
점검 쪽 하드코딩 집합 삭제 → validate_capability_name 호출. 프롬프트 안내 문구도 같은 정본에서 생성.
새 능력·동작 추가는 정본 확장 규격(루트가 정함: 버전·승인 절차) 없이 확장 패키지가 독자 추가할 수 없다 — 그 상태가 정상이다.
(4) 규칙 유지. 모르는 이름 거절(UnsupportedCapabilityError 등), 조합키 외 이름 승인 금지, 등록된 동작의 스키마·대상·정책 검사는 그대로.
(5) 한계·미검증. 읽기 전용이라 실제 import 전환 시 순환 import 발생 여부는 미검증(초안 모델이 core를 import해도 되는지 루트 확인 필요).
문서(docs/contracts/actions.md)와 코드 정본의 동기화 절차도 이번 조사 범위 밖이다.
## 3. M06 — 동작별 대상 바인딩·실행 조리법

(1) 코드 확인. 후보 컴파일(compiler)의 대상값 바인딩에 동작 특례 2건: `operation == "web_search"`면 대상값을 비우고,
`operation == "endpoint_discovery"`면 경로(path) 바인딩을 시도한다. 주소 대조(target_binding:validate_network_arguments)는
동작별 엄격도가 다르다: `page_read`는 URL 대상을 강제하고 URL 전체 일치를 요구, 그 외는 호스트·포트·주소 대조.
실행 연결(execution_bridge)은 `EXECUTION_OPERATIONS`(command_* 4종) 중 실제 허용은 `analysis.command_run` 고정 1건으로 좁힌다 —
도구명·동작·노드 종류·자산 일치 시 모델이 낸 인자를 버리고 운영자 설정 조리법으로 교체(bind_execution_arguments).
(2) 감사 문서 일치. '새 동작이 새 바인딩·조리법을 요구하면 중앙 분기를 함께 수정' 주장과 일치. 반례(모델 직접 인자는 자리표시자 표를 안 탐)도 코드상 맞다 —
해당 바인딩은 스키마 통과 인자에 대상값을 채우는 단계라, 스키마에 맞는 직접 인자는 그대로 간다.
(3) 이관안. 동작 제공자의 등록 선언으로 옮긴다: 대상 바인딩 규칙(대상 종류·인자 매핑·엄격도) + 승인 조리법(허용 도구·고정 인자 출처).
중앙은 일반 검사기로 남는다: 실제 대상 일치·스키마·승인·고정 인자 적용 검사 + 조리법 외 명령 차단.
제한 명령 조리법을 모델 자유 명령으로 바꾸는 안은 금지(감사 문서 방향과 동일).
파일: compiler의 동작 if 2건을 선언 소비로 교체, target_binding의 page_read 특례를 선언(엄격도 abroad)으로 일반화,
execution_bridge의 고정 1건 판정을 선언(허용 도구 기술자 + 조리법 출처)으로 일반화. 선언 스키마 자체는 신설(§8 초안).
(4) 규칙 유지. page_read URL 전체 일치, 포트·주소 불일치 거절, 미승인 실행 에이전트 차단, 조리법 외 명령 거부는 그대로 둔다.
(5) 한계·미검증. 선언으로 일반화해도 page_read급 엄격도를 새 제공자가 스스로 낮춰 선언할 수 있는 문제 — 중앙 하한(최소 엄격도) 필요 여부는 루트 결정.
선언 스키마의 표현력(조건식 vs 열거)도 미정.

## 4. M08 — 재시도 의미 목록

(1) 코드 확인. 실행 노드(execute)가 재시도 안전 집합을 소유: `recon.service_discovery` 등 4건 + `web.http_request`는 GET·HEAD이면서
본문 키(body·data·json·payload) 없을 때만. 재시도 게이트는 4조건 AND — 운영자 설정 허용 + 잔여 횟수 + 기술자의 위험 LOW + 안전 동작 + 장기작업 아닐 것.
주석에 명시: LOW 단독은 멱등성을 말해주지 않는다(저위험 쓰기도 상태 변경 가능).
(2) 감사 문서 일치. '새 읽기 동작은 목록에 없으면 혜택을 못 받는다, 안전한 방향의 운영 비용이며 최초 실행 차단과 다르다'와 일치. 정정 없음.
(3) 이관안. 승인된 도구·동작 선언에 재시도 의미를 넣는다: `retry_safe` + 조건(예: http 무본문 GET 한정 같은 명명된 조건).
일반 실행기가 소비하고, 중앙 게이트(운영자 승인·LOW·게이트웨이 오류 종류)는 그대로 둔다. 제공자 주장만으로 재시도하지 않는다.
(4) 규칙 유지. LOW≠재시도 가능, 쓰기·인증 시도는 원칙 재시도 제외, 게이트웨이 오류 종류 판정은 중앙 소유.
(5) 한계·미검증. 조건 표현을 일반식으로 할지 명명 조건 열거로 할지 미정 — 과설계 방지로 열거를 권장(§9). 장기작업 어댑터 병용 시 제외 규칙은 그대로.
## 5. M13 — MCP 연결기·프로필·명세 중앙 선택 사슬

(1) 코드 확인. 설정 해석(settings:from_environment)은 서버ID→환경 접두어로 KIND·TRANSPORT·RISK·허용 목록·바인딩·프로필을 읽고,
프로필별(vdb_readonly·page_text_readonly 등)로 종류·전송·허용 목록·바인딩 정확 일치를 강제한다.
어댑터 선택(bootstrap:_create_adapter)은 서버ID 특례(mobsf) → 프로필(page_text·execution·vdb·workspace) → 종류(web_api·mobile 등) 순의 if 사슬이다.
기본 대상(_default_target_types)은 종류→대상 힌트 매핑이다. 조립(_build_gateway 부근)은 발견 결과와 명세 대조 후 등록부에 적재한다.
(2) 감사 문서 일치. '새 연결기 종류·관리형 프로필 추가 시 해석·선택·기본 대상·명세 대조를 함께 수정'과 일치. 정정 없음.
'도구 계층 내부 일반 팩토리보다 중앙 사슬이 우선'도 코드상 맞다(선택 함수가 종류별 어댑터를 직접 고른다).
(3) 이관안. 한 제공자의 4점(설정 스키마·연결기 팩토리·대상 의미·명세 검사)을 하나의 등록 서술로 묶는다(§8 초안).
앱 조립부(루트 소유)는 등록을 읽어 설정 해석→어댑터 생성→명세 대조→등록부 적재를 수행한다. 종류별 기존 일반 경로(web_api 등)는
기본 등록으로 남겨 호환을 유지한다. 원격 목록 발견과 실행 승인은 계속 분리(선언 발견 ≠ 호출 허가).
(4) 규칙 유지. 프로필의 정확 일치 강제(vdb 허용 목록·바인딩 등), 종류·전송 교차 검증, 명세 대조 불일치 시 조립 거절, 출처·정책·증거 검사는 각 자리 유지.
(5) 한계·미검증. 등록 발견 방식(명시 목록 vs 진입점 자동 발견 vs pluggy)은 루트 결정(§9). 기존 서버ID 특례(mobsf 등)를 등록 1건으로 옮기는 작업량은 미산정.

## 6. M14 — 서버 미포함 도구 식별자

(1) 코드 확인. 식별자 공식(naming:build_mcp_tool_id)은 `<능력>.<정규화 원격명>` — 서버ID가 없다.
등록부(registry:replace_server_tools)는 타 서버의 동일 식별자를 충돌로 거절한다(덮어쓰기 없음 — 정상).
후보 조회(catalog:list_tools_for_action)는 식별자의 능력 접두 + 동작 포함으로 걸러내고, 명세 검사(manifest:_validate_identity)는
접두=능력 태그 포함 + 정본 어휘 소속을 강제한다.
(2) 감사 문서 일치. '두 서버가 같은 원격명을 주면 충돌 거절' 및 '단순 서버명 네임스페이스로 바꾸면 능력 일치 조회에서 제외'와 일치.
예시(`get` 충돌)는 소스 공식으로 구성한 사례이며 실제 설치 조합 실행 재현이 아님 — 감사 문서의 단서를 유지한다.
서버별 별칭·재명명 항목이 설정 경로에 없음, 허용 목록은 거름 기능이지改名이 아님도 코드상 맞다.
(3) 이관안. 안정 서버별 신원과 능력·동작 의미를 분리한다. 방향: 기존 능력 접두 식별자는 바인딩·조회·증거 호환용으로 유지하고,
등록부·호출·증거 연결용으로 서버 구분자를 포함한 안정 신원을 추가한다(형식은 루트 결정). 충돌 검사는 유지하고 덮어쓰기 전환은 금지.
식별자 호환(기존 승인 목록·바인딩·저장 증거)은 반드시 별도 결정 항목으로 분리한다 — 본 특별 지시대로 이 안에서는 형식을 정하지 않는다.
(4) 규칙 유지. 충돌 시 거절(선착순 유지), 능력 접두 필터, 명세의 능력·어휘 검사, 원본 증거의 식별자 연결.
(5) 한계·미검증. 읽기 전용이라 실제 충돌 재현 없음. 정규화 충돌(서로 다른 원격명이 정규화 후 동형)도 같은 경로로 거절됨은 공식상 자명하나 실행 미확인.
## 7. M15 — 대상 종류·인자 신원 연결

(1) 코드 확인. 대상 합집합(contracts/target)은 host·domain·url·endpoint·service·principal·asset·device·graph_node 9종.
범위 해석(scope:_resolve_target)은 호스트 신원이 있는 종류만 (host, port)로 풀고, 없으면 빈 목록을 반환한다.
범위 판단(check_scope)은 빈 목록이면 `no scope-checkable host identity`로 거절한다 — 모르는 대상 기본 거부(정상).
자산 검사(361행 부근)는 조건부다: 인자에 `asset_ref`가 있고 대상 자산ID와 다를 때만 거절한다.
없으면 곧바로 거절하지 않고 허용 자산 목록 대조(_check_asset_scope)로 간다. 특별 지시대로 '없으면 무조건 거절'이 아님을 확인했다.
범위 우회 취약점 재현 주장은 하지 않는다(실행 금지 준수, 정적 확인만).
(2) 감사 문서 일치. '특정 인자명 조건부 검사이며 우회 재현 아님' 서술과 일치. 정정 없음.
(3) 이관안. 공통 대상의 안정 신원 + 도구별 인자 연결을 별도 선언으로 만든다: 대상 종류별 범위 해석 등록 +
도구별 신원 인자 매핑(예: 이 도구의 어떤 인자가 어떤 대상 신원과 같아야 하는지). 대상 종류별 해석은 등록으로 확장하고,
모르는 대상은 기본 거부 유지. 에이전트 식별자(agent_id)와 대상 서비스 계정(principal)은 계속 구분한다.
중앙 사설 접근 우회는 금지 — 선언을 읽는 공용 판독기(루트 소유 공통 계약 경유)로만 소비한다.
(4) 규칙 유지. 미해석 대상 거절, asset_ref 불일치 거절, 허용 자산 목록 대조, 호스트·도메인·URL·포트 대조 전부 유지.
(5) 한계·미검증. 새 도구가 선언 없이 신원 인자를 쓸 때의 처리(선언 의무 vs 경고 vs 거절)가 미정 — 루트 결정(§9).
기존 도구의 인자명과 새 선언의 충돌 시 우선순위도 미정. device·graph_node 등 호스트 없는 종류의 범위 해석 확장 방식은 루트 조율.

## 8. M16 — 후속 행동 지시 문자열 꼬리표

(1) 코드 확인. 판단 계약(policy:PolicyDecision)은 동결(v0.1)이라 심각도 필드가 없고, 꼬리표가 판정서 규칙 목록에 실린다 — `severity:<값>`.
생성·해석(severity:to_tag/severity_from_matched_rules)과 오케스트레이션 판독(nodes/policy:denial_severity)은 별도 구현이다.
오케스트레이션 쪽이 통제 패키지를 직접 import하지 못하는 의존 규칙 때문에 문서화된 문자열을 다시 읽는다(주석 명시).
값의 정의는 공통 열거(core/enums:Severity BLOCK_ACTION·REPLAN_GOAL·ABORT_RUN) 1곳에 있다. 변형·오염 꼬리표는 None으로 읽는다(테스트 보장 언급).
(2) 감사 문서 일치. '양쪽 파서 + 규격 변경 시 양쪽 수정'과 일치. '위험·심각도·후속 대응은 서로 다른 축' 결론도 유지한다 —
도구 위험(RiskLevel: 호출 전 위험도), 후속 대응(Severity: 거부 후 어떻게 할지), 취약점 심각도(판정 기록의 심각도)는 판단 주체·시점이 다르다. 합치기 금지.
(3) 이관안. 최소안은 전달 규약 + 공용 판독기를 공통 계약(루트 소유)으로 올린다: 쓰기·읽기 함수 1쌍을 공통에 두고 양쪽이 import.
승격안(루트 조율)은 판단 계약에 심각도 정식 필드 추가 — 동결 계약 변경이라 버전·마이그레이션 결정이 필요하다. 1단계는 최소안, 2단계 승격을 권장.
(4) 규칙 유지. 오염 꼬리표→None(기존 동작 유지), 미태그 거부=기존 동작 차단·재계획, 정의 1곳(공통 열거) 유지.
(5) 한계·미검증. 공통 판독기 위치(core 내 어디)가 루트 설계에 달림. 필드 승격 시 저장 증거·완료 검사의 판정서 읽기 호환도 루트 확인 필요.
## 9. M17 — 실행 문맥 자유 메타데이터

(1) 코드 확인. 판단 문맥(policy:ControlContext.metadata)은 자유 dict다. 병렬 제한(roe:_check_max_parallel_actions)은 비행 중 수가
없으면(None) 거짓·닫힘으로 거절한다 — 한도 설정이 무력화되는 fail-open을 막기 위한 의도적 선택(주석 명시).
반면 속도 제한(rate_limit:concurrency_exceeded)은 None이면 거짓(제한 미적용) — 예전 직렬 경로 호환(주석 명시). 같은 None, 정반대 처리다.
(2) 감사 문서 일치. '두 제한의 목적·호환 정책이 다르지만 새 제공자는 차이를 알아야 한다' 및 '주 실행 경로의 오전달 입증 아님'과 일치. 정정 없음.
(3) 이관안. 집행 필수 신호를 정형 계약으로 올린다: 비행 중 수 등 필수 신호 + 제공 조건 + 부재 의미 명시.
현재 두 부재 의미를 바꾼 것처럼 보이지 않게 그대로 명문화한다(병렬=부재시 거절, 속도=부재시 구경로). 새 호출자·워커 구현의 통합 계약으로 둔다.
위치는 공통 계약(루트 소유), 값 제공은 호출부(오케스트레이션·작업 장부)가 책임진다.
(4) 규칙 유지. 병렬 제한 fail-closed, 속도 제한의 직렬 호환, 부재 의미 문서화.
(5) 한계·미검증. 읽기 전용이라 주 실행 경로가 실제로 항상 값을 주는지(호출부 전수)는 미확인 — 전수 호출부 인벤토리는 루트·오케스트레이터 측에 요청한다.
신호 추가 시 기존 저장·체크포인트 직렬화 영향도 루트 확인 필요.

## 10. M25 — 보고 갈래·공용 서버 실행기·배포 조립 고정 목록 (도구·배포 측면)

(1) 코드 확인. 보고(reporting:_WORKSTREAM_FAMILIES)는 자유 문장 키워드→5갈래(인가·인증·정찰·모바일·웹) 매핑, 순서 의존(구체 먼저).
공용 서버 실행기(servers/__main__)는 역할 6종(web-http·web-proxy·recon-passive·recon-active·web-zap·web-nuclei) 고정.
배포 명세(deployment:DeploymentSpec)는 역할 Literal 2종(web-http·recon-passive)으로 고정 + 읽기전용·바인딩 정확 일치 검증.
앱 동기화(app/mcp_deployment:131 부근)는 명세 역할 ⊆ 서버ID, 환경·argv·허용 목록·동작·자격 전달까지 대조한다.
(2) 감사 문서 일치. '새 갈래·역할을 기본 경로에 넣으려면 목록 수정' 및 '서로 다른 목록을 하나로 합치지 말 것'과 일치.
'보고 누락≠실행 불능, 외부·별도 모듈 서버는 공용 실행기를 안 탐'도 코드상 맞다(외부 서버는 별도 어댑터·설정 경로).
(3) 이관안. 3목록을 각각 제공자 선언으로 푼다: 업무 갈래 키워드(보고 제공자 선언), 서버 실행 역할(서버 제공자 선언), 배포 역할(배포 제공자 선언).
중앙은 대조·조립만 유지: 보고는 선언 키워드 합성 매핑 + 매칭 실패→미분류(None, 오류 아님), 배포는 digest·환경·argv·허용 목록 대조 유지.
3목록 통합(거대 역할 목록)은 금지. 배포 Literal 고정은 선언 검증으로 대체하되 동등 강도를 유지한다.
(4) 규칙 유지. 보고 매핑 순서(구체 우선), 배포의 읽기전용·정확 바인딩·digest 대조, 동기화의 환경·argv·허용 목록·자격 검사.
(5) 한계·미검증. 키워드 충돌(두 제공자가 같은 키워드 주장) 시 우선순위 미정. 배포 역할을 넓히면 정책·세션·바인딩 검증이 따라서 넓어져야 하는데 그 범위 미산정.
보고 쪽은 P2라 실행 직접 영향은 없다.
## 11. 확장 등록 선언 초안 (Pydantic 스케치, 도구·MCP 측 최소 정보 + 소비 방식)

원칙: 실행 확장에만 필수, 참고 자료(스킬 문서 등)에는 강제 금지. 모르는 값은 거절(기본 거부).
필드 옆 [필수]/[선택] + 소비자를 함께 적는다. 위치는 신설 파일(루트 배치 결정), 기존 검사는 각 자리에 남는다.

```python
class CapabilityDeclaration(BaseModel):
    capability: str  # [필수] 정본 어휘 소속. 소비: 명세 검사(manifest)가 정본 대조.
    operations: list[str]  # [필수] 정본 어휘 소속. 소비: 명세 검사 + 후보 조회(catalog).

class ProfileSettingsSchema(BaseModel):
    profile: str  # [필수] 예: page_text_readonly. 소비: 설정 해석(settings).
    required_kind: str  # [필수] 종류 교차 검증용. 소비: 설정 해석.
    required_transport: str  # [필수] 전송 교차 검증용. 소비: 설정 해석.
    config_schema: dict  # [선택] 운영자 설정 JSONSchema. 소비: 설정 해석(정확 일치 판정).
    exact_allowlist: list[str] | None  # [선택] None=자유, 값=정확 일치 강제. 소비: 설정 해석.
    exact_bindings: dict[str, list[str]] | None  # [선택] 도구→동작 정확 매핑. 소비: 설정 해석.

class ConnectorFactoryDecl(BaseModel):
    connector_key: str  # [필수] 어댑터 선택 키(종류·프로필·서버ID 특례 대체). 소비: 조립(bootstrap).
    factory_ref: str  # [필수] import 경로(문자열, 직접 import 금지 — 조립부가 해석). 소비: 조립.
    default_target_types: list[str]  # [필수] 보수적 힌트(실행 대상 추가 금지). 소비: 조립·후보 필터.
    discovery_manifest_check: str  # [선택] 명세 대조 함수 ref. 소비: 조립. 없으면 공용 검사만.

class TargetBindingDecl(BaseModel):
    target_kinds: list[str]  # [필수] 이 도구가 받는 대상 종류. 소비: 후보 컴파일·범위 판단.
    identity_arg_map: dict[str, str]  # [필수] 인자명→대상 신원(예: asset_ref→asset_id). 소비: 범위 대조(scope).
    strictness: str  # [필수] standard | page_read급 전체일치 등 명명값. 소비: 주소 대조(target_binding).
    path_binding: str | None  # [선택] endpoint_discovery식 경로 바인딩 규칙. 소비: 후보 컴파일.

class ExecutionRecipeDecl(BaseModel):
    allowed_tools: list[str]  # [선택] 조리법 허용 도구. 소비: 실행 연결. 없음=조리법 미사용.
    recipe_source: str  # [선택] 고정 인자 출처(설정 ref). 소비: 실행 연결. 모델 인자 대체 근거.
    retry_safe: bool = False  # [선택] 기본 False. 소비: 실행 노드. True여도 LOW·게이트 별도.
    retry_condition: str | None  # [선택] 명명 조건(예: http_get_no_body). 소비: 실행 노드. 일반식 금지.

class ProviderRegistration(BaseModel):
    provider_id: str  # [필수] 서버·도구묶음 식별. 소비: 등록부·호출·증거 연결(서버 구분자).
    capability: CapabilityDeclaration  # [필수]
    settings: ProfileSettingsSchema | None = None  # 참고 자료는 None 허용.
    connector: ConnectorFactoryDecl | None = None  # 참고 자료는 None 허용.
    binding: TargetBindingDecl | None = None  # 실행 도구는 필수, 참고 자료는 None.
    recipe: ExecutionRecipeDecl | None = None
    report_keywords: list[str] = []  # [선택] 보고 갈래 키워드. 소비: 보고 매핑.
    server_roles: list[str] = []  # [선택] 공용 실행기·배포 역할. 소비: 실행기·배포 조립.
```

기존 검사가 남는 자리: 어휘 거절(공통), 스키마 일치(컴파일), 대상 일치(바인딩), 범위·승인(통제), 고정 인자(실행 연결),
충돌 거절(등록부), digest·환경 대조(배포 동기화), 원본 증거 연결(검증·완료). 선언은 후보를 넓힐 뿐 어떤 검사도 면제하지 않는다.

### Pluggy vs pydantic-settings 판단

- pydantic-settings: 설정 해석(settings)이 환경변수 접두어→필드 매핑을 수작업으로 하므로 실익 있다.
  단, 적용처는 설정 해석 1곳으로 한정. 어휘·선언·판정까지 끌고 오면 과설계다.
- pluggy: 실익 있는 지점은 어댑터 선택(_create_adapter if 사슬)과 프로필별 명세 대조 분기뿐이다.
  진입점 기반 자동 발견까지 가면 과설계 — 승인된 등록 목록(명시 화이트리스트) + 일반 함수 호출이면 충분하다.
  즉 hookimpl 1~2개(연결기 제공·명세 검사 제공) 수준이 상한, 그 이상(프로필·바인딩·보고까지 훅화)은 과설계다.
- 권장: 선언은 Pydantic 모델(위 초안), 발견은 명시 등록표, pluggy는 어댑터·검사 분기 해소용으로만 — 필요 없으면 뺀다.
## 12. 루트 확정 최소 계약 목록 (선택지 + 추천)

R1 어휘 정본 공개 방식. A: 검증 함수만 공개(호출로 검사) / B: frozenset 상수 + 버전 표식 공개(enum 파생용). 추천 B — 프롬프트·스키마 enum 주입에 열거가 필요하고 함수만으론 부족.
R2 조리법 승인 방식. A: 선언된 조리법도 운영자 설정 ON일 때만 발동(현행 유지) / B: 선언만으로 발동. 추천 A — 고정 명령은 선언보다 운영자 손에 둔다.
R3 재시도 조건 표현. A: 명명 조건 열거(권장, 과설계 방지) / B: 일반 조건식. 추천 A.
R4 등록 발견 방식. A: 명시 등록표(화이트리스트) / B: 진입점 자동 발견 / C: pluggy 훅. 추천 A(+필요시 C를 어댑터·검사 2훅으로 한정).
R5 도구 신원 형식. A: 능력 접두 유지 + 서버 구분자 별도 필드 / B: 식별자에 서버 포함. 추천: 형식 자체는 루트 결정에 맡기고,
  기존 승인·바인딩·저장 증거 호환은 별도 결정 항목으로 분리(특별 지시). 어느 안이든 충돌 거절·덮어쓰기 금지는 유지.
R6 신원 인자 선언 의무. A: 실행 도구는 identity_arg_map 필수, 미선언 신원 인자 사용 시 거절 / B: 선택(미선언은 검사 생략).
  추천: 자산·장치 등 신원 민감 도구는 A, 그 외는 B — 경계는 루트가 대상 종류별로 정한다.
R7 꼬리표 처리. 1단계: 공통 공용 판독기(최소안) / 2단계: 판단 계약 정식 필드 승격(동결 계약 변경). 추천 1→2 순차.
R8 집행 신호 위치. A: ControlContext 정식 필드 / B: 별도 인자 구조체. 추천: 루트 설계에 위임, 단 부재 의미(병렬 거절·속도 구경로)는 현행 유지.
R9 목록 선언 출처. A: 코드 상수 등록 / B: 패키지 데이터 선언. 추천 A(타입 검사·리뷰 가능), digest 대상은 정책 바이트 유지.
R10 공통 계약 변경분(§요약표의 루트 소유 항목)의 버전·마이그레이션 절차 — 전부 루트 조율 제안으로 분리. 내선 사설 접근 우회 금지.

루트 결정 필요 항목 수: 10건(R1~R10, R5-호환·R10-절차 포함).

## 13. 실결함 vs 운영 제약

| 구분 | 항목 | 이유 |
|---|---|---|
| 실결함 | M01 복사본 2건 | 정본과 어긋날 수 있는 중복 진실 |
| 실결함 | M06 중앙 동작 분기 | 새 바인딩·조리법에 중앙 수정 강제 |
| 실결함 | M13 중앙 선택 사슬 | 4점 분산 수정 강제 |
| 실결함 | M14 서버 미포함 공식 | 다중 서버 확장 시 충돌 거절 유발(거절 자체는 정상) |
| 실결함 | M15 닫힌 대상·인자 계약 | 새 표현에 공통·통제 수정 강제(조건부 검사 자체는 정상) |
| 실결함 | M16 이중 파서 | 규격 변경 시 양쪽 수정(동결 계약은 제약으로 병존) |
| 운영 제약 | M08 재시도 혜택 제외 | 안전측 제한, 실행 차단 아님 |
| 운영 제약 | M16 동결 계약 | 당장 필드 승격 불가, 판독기 1단계로 대응 |
| 운영 제약 | M17 None 양면 처리 | 목적·호환이 다른 의도적 설계, 신규 제공자 안내 문제 |
| 운영 제약 | M25 고정 3목록 | P2, 보고 누락≠실행 불능, 외부 서버 우회 가능 |
| 정상(유지) | writing_schema 파생, 충돌 거절, 조건부 자산 검사, 축 분리(위험·후속·심각도), digest·환경 대조, 원본 증거 연결 | 손대면 안 되는 검사들 |

## 14. 확신 없는 부분·미검증 목록

1. import 전환 시 순환 import 여부(M01) — 정적 미확인, 루트 확인 필요.
2. 다중 서버 충돌 실제 재현(M14) — 읽기 전용이라 미실행. 정규화 충돌도 동일.
3. 주 실행 경로의 집행 신호 실제 전달 전수(M17) — 호출부 인벤토리 필요.
4. 기존 서버ID 특례(mobsf 등) 등록 이전 작업량(M13) — 미산정.
5. 공통 판독기·신호 필드 위치의 저장·체크포인트 영향(M16·M17) — 루트 확인 필요.
6. 키워드 충돌 우선순위·배포 역할 확대의 검증 확대 범위(M25) — 미정.
7. dev-agent-audit 교차 근거 — 경로 공백 인용 실패로 미열람. dev-mcp만 참조.
8. 선언 스키마 표현력 상한(조건식 vs 열거, M06·M08) — 열거 권장이나 루트 확정 전.
9. 중앙 하한(새 제공자가 엄격도를 낮춰 선언할 때) 필요 여부(M06) — 미정.
10. R5 식별자 호환(승인·바인딩·저장 증거) — 별도 결정 항목으로 분리, 본 문서에서 형식 예단 금지.

## 15. 폐쇄 루프·검증 계약 유지 확인

- 검증 계약 감사(E01 원본 보존·A07 승인·R04 재개 등): 본 이관안은 원본 저장 순서·승인 체계·완료 증거 연결을 바꾸지 않는다.
  선언은 후보 적격만 넓히고, 실행→검증→완료의 원본·제안·호출·대상·계정 대조는 그대로 둔다.
- 폐쇄 루프 계획(루트 주도·후보 참고·해석 상태 명시): 등록 확장이 루트 판단을 대체하지 않는다.
  후보 부재가 루트 제안을 막지 않고, 후보 존재가 실행 허가가 아니다 — 현행 그대로.
- 감사 문서 10절 기각 주장(위험 통합·해석기=실행· 자리표시자 만능 등)을 재주장하지 않는다. 축 분리·반례를 유지했다.

— 끝. 이후 구현은 루트 후속 지시를 기다린다. —
  코드 내 pluggy/BaseSettings 사용처는 없음 — `rg pluggy|pydantic_settings` 결과 pyproject 1건のみ. 즉 설치 예정 상태, 도입 위치는 미정.
  코드 내 pluggy/BaseSettings 사용처는 없음 — `rg pluggy|pydantic_settings` 결과 pyproject 1건뿐. 즉 선언만 있고 도입 위치는 미정.
