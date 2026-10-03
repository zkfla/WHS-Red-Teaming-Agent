# dev–Capability 통합: Agent 팀·MCP 팀 작업 및 공통 규격

작성일: 2026-10-03

이하 표는 작업 시작 전 진단이다. capability에서 수행한 후속 구현과 실제 검증,
Agent 팀 검수·운영 환경에 남은 항목은
[작업 결과](dev-capability-integration-result.md)에 구분하여 기록한다.
`d973b36`은 계획 작성 후 원격 capability로 push됐으며 이번 작업은 그 위에서 진행한다.

## 1. 목적과 적용 범위

현재 구현한 MCP 도구를 최신 dev의 Agent 실행 흐름에 안전하게 통합하기 위한
팀별 작업 목록과 완료 기준이다. 도구를 새로 만드는 계획이 아니라, 이미 구현된
도구와 Agent의 작업 이름·목록·대상·인자·실행·결과 규격을 맞추는 계획이다.

이 문서 작성은 코드 수정, 충돌 해결, push, PR 생성 또는 dev 병합을 수행했다는 뜻이 아니다.
작업은 capability 브랜치에서 진행하고 검증 후 dev를 대상으로 PR을 올린다.

### 확인 기준

| 구분 | 확인한 커밋 | 의미 |
|---|---|---|
| dev | `afc529e` | Control의 포트 인식 범위 검사 및 최신 Agent 변경 포함 |
| 원격 capability | `02d8b5c` | GitHub에 올라간 A·B·C 작업 기준 |
| 로컬 capability | `d973b36` | 공통 작업 이름·검증 통합 커밋. 확인 당시 아직 미푸시 |

브랜치는 이후 변경될 수 있으므로 실제 통합 시작 시 remote를 다시 확인한다.
기존 미커밋 변경은 보존하고 통합 작업에 임의로 포함하지 않는다.

## 2. 현재 확인된 문제

| 문제 | 확인 내용 | 통합하지 않고 합칠 때의 영향 |
|---|---|---|
| 공통 코드 충돌 | 원격 capability와 dev는 5개 파일, 로컬 최종 통합까지 포함하면 10개 파일에서 충돌 | 자동 병합 불가. 한쪽을 통째로 선택하면 다른 팀 기능이 사라질 수 있음 |
| 공통 작업 이름 정의 중복 | dev는 `core/action_semantics.py`의 12개 이름, 로컬 capability는 `core/contracts/action_naming.py`의 23개 이름 사용 | 도구 등록 기준과 Agent 계획 기준이 달라질 수 있음 |
| Catalog 최종 메타데이터 처리 차이 | dev는 조회 결과에 확정된 operation 연결을 반영하는 변경 포함 | 조회 방법에 따라 Agent가 보는 연결 정보가 달라질 수 있음 |
| 실행 흐름 변경 중첩 | dev의 후보·실행 전 조건 검사와 capability의 셸·분석 활성 조건이 같은 Engine 부분을 수정 | 검증 또는 도구 노출 제한이 누락될 수 있음 |
| 승인 후 재개 변경 중첩 | dev의 종료·복구 검사와 capability의 checkpoint thread 연결이 같은 Runner 부분을 수정 | 잘못된 재개, 재개 실패 또는 중복 실행 위험 |
| CI 타입 검사 실패 | 같은 로컬 환경의 mypy 검사에서 dev는 통과, 원격·로컬 capability는 각각 194개 오류/28개 파일 | 충돌 해결 후에도 CI 통과를 보장할 수 없음 |

194개는 해당 로컬 환경에서 재현한 수치다. 의존성·환경·병합 결과에 따라
GitHub CI의 오류 개수는 달라질 수 있다. 이 수치를 현재 PR 실패 로그로 간주하지 않는다.

이전 `2345 passed, 22 skipped`는 당시 capability 작업 트리의 pytest 결과다.
최신 dev와 합친 결과 또는 mypy·Docker를 포함한 전체 CI 통과 결과가 아니다.

### 원격 capability 기준 충돌 파일

- `src/redteam/app/bootstrap.py`
- `src/redteam/capability/bootstrap.py`
- `src/redteam/orchestration/action_naming.py`
- `src/redteam/orchestration/engine.py`
- `src/redteam/orchestration/runner.py`

로컬 `d973b36`까지 포함하면 다음 5개가 추가된다.

- `src/redteam/capability/catalog.py`
- `src/redteam/capability/manifest.py`
- `src/redteam/orchestration/candidates/compiler.py`
- `tests/integration/test_mcp_stdio.py`
- `tests/integration/test_mcp_streamable_http.py`

## 3. 역할 소유권과 협업 경계

| 범위 | 주 담당 | 상대 팀의 검수 항목 |
|---|---|---|
| MCP 서버·Adapter·Registry·Catalog·Gateway | MCP 팀 | Agent가 Core Port로 소비 가능한지 |
| Execution Substrate·도구별 실행 정책 | MCP 팀 | Agent에 노출되는 범위와 job 처리 방식 |
| 계획·후보·Engine·Runner·결과 해석 | Agent 팀 | MCP 호출 계약 및 활성 조건 보존 |
| Core 계약·공통 작업 이름·오류 의미 | 공동 검수, 변경 담당자 1명 지정 | PM/Control 계약에 영향이 있는지 |
| `app/bootstrap.py` | 공동 검수, 통합 담당자 1명 지정 | 양쪽 초기화·저장소·MCP 기능 보존 |
| ROE·범위·사람 승인 판단 | Control 팀 기존 책임 | Agent/MCP는 판단을 우회하거나 자체 승인하지 않음 |
| 새로운 공통 작업 의미·권한 정책 변경 | PM 및 관련 계약 소유자 확인 | 구현 목록을 승인 목록으로 오인하지 않음 |

공통 파일은 양 팀이 동시에 독립 수정하지 않는다. 담당자 한 명이 변경하고
상대 팀이 diff와 통합 테스트를 검수한다. 코드 소유권 분담이 새로운 권한 승인을 대신하지 않는다.

## 4. 공통으로 맞춰야 할 규격

### 4.1 행동 의미와 실제 도구 이름

```text
ActionProposal.capability = web
ActionProposal.operation  = page_read
canonical action key      = web.page_read
실제 ToolId               = web.fetch_page_text
```

- capability는 `recon`, `web`, `mobile`, `analysis`를 사용한다.
- operation은 공통 명세에서 지원하는 lower_snake_case 행동 의미다.
- ToolId는 `<namespace>.<remote_tool_name>`이며 operation과 별개다.
- 이름이 우연히 같아도 ToolId가 자동으로 operation 연결을 만들지 않는다.
- 이름 지원, 도구 연결, 실행 권한은 각각 별도로 검사한다.

권장 통합 방향은 dev의 `core/action_semantics.py`에 공통 정의를 모으고,
capability의 검사 기능·확정된 이름을 반영하는 것이다. 다른 경로는 필요하면
호환용 재-export를 둔다. 이는 제안이며 공동 검수 후 경로를 확정한다.
최종 기준은 모든 소비자가 같은 상수·검사 함수를 사용하는 것이다.
후속 구현에서 이 경로로 단일화했으며 기존 경로는 호환 재-export만 유지한다.

#### 현재 이름의 합의 상태

| 이름 묶음 | 상태와 필요한 확인 |
|---|---|
| 기존 dev 작업 이름 12개 | 현재 dev 공통 정의에 존재 |
| `web.page_read`, `web.web_search` | 사용자 확인으로 PM·Agent 확정 전달됨. 통합 시 반영 |
| `directory_list`, `file_read`, `file_search`, `file_write`, `file_edit` | capability 구현·문서에 존재. dev 통합 전 PM/Agent/Control 검수 상태를 기록 |
| `command_run`, `command_job_start`, `command_job_status`, `command_job_cancel` | capability B 구현에 존재. 문서의 제안/확정 상태와 실제 합의 상태를 일치시킴 |

로컬 목록에 23개가 있다는 사실만으로 23개 모두 운영 권한이 승인됐다고 해석하지 않는다.

### 4.2 Agent용 도구 메타데이터

현재 Core `ToolDescriptor`와 `ToolCatalog`를 우선 사용한다.

| 정보 | 공통 의미 |
|---|---|
| `tool_id` | 실제 호출할 도구의 고유 식별자 |
| `operations` | 해당 도구에 명시적으로 연결된 지원 행동 의미 |
| `capability_tags` | 도구 분류. 호출 namespace와 모순되면 안 됨 |
| `input_schema` | 필수/선택 인자, 타입, 허용값과 한도의 원본 |
| `target_types` | 호환되는 대상 종류. 없는 정보를 추측해 채우지 않음 |
| `risk_level` | 도구의 위험도. 정책 판단의 입력이지 승인 자체가 아님 |
| `health_status` | 확인된 서버 상태. 정상 연결이 실행 권한을 뜻하지 않음 |
| `output_schema` | 결과 구조. 미제공일 때 존재한다고 가정하지 않음 |

필수·선택 인자 목록은 `input_schema`에서 파생한다. 별도 수동 목록을 진실의 원본으로 두지 않는다.
Catalog의 `get`, capability 목록, 작업별 목록에서 같은 최종 연결 정보가 보여야 한다.

현재 `ToolCatalog` Port는 다음 조회를 제공한다.

```text
get(tool_id)
list_by_capability(capability_tag)
list_tools_for_action(capability, operation)
```

Capability 내부의 `ToolCandidateMetadata` 편의 조회가 필요하다면 Core 계약에
올리는 변경부터 공동 합의한다. Agent가 Capability 구현체를 직접 import하지 않는다.

### 4.3 대상과 입력 인자

| 도구 종류 | Agent 측 대상 | MCP 측 입력/정책 기준 |
|---|---|---|
| 페이지 읽기 | 알려진 `UrlTarget` | `url`, 길이 한도, 운영자 허용 origin |
| 파일·방법론·VDB | 승인된 `AssetTarget` | 서버별 `asset_ref`와 경로·읽기/변경 제한 |
| 셸 실행 | 승인된 작업 공간 Asset | opt-in, 고정 recipe, run/engagement 연결 및 서버 정책 |
| 웹검색 | 알려진 조사 맥락의 TargetRef | `query`, `max_results`, 공급자 활성 설정·비용/출력 한도 |

현재 검색 입력에는 대상 URL 인자가 없다. 검색어 또는 Tavily endpoint를 시험 대상으로
사용하지 않는다. 현재 ActionProposal은 TargetRef가 필수이므로 조사 맥락의 알려진 대상을
유지한다. 완전히 대상 없는 행동을 지원하려면 별도 Core·Control 계약 변경이 필요하다.

검색 결과 URL은 새 scope 승인이나 자동 방문 허가가 아니다. 페이지를 읽으려면
별도로 알려진 대상, Control의 허용, 페이지 서버의 origin 허용이 필요하다.

포트·scheme·path의 의미를 양쪽에서 임의로 바꾸지 않는다. 최신 dev의 포트 인식
범위 검사를 보존하고, Control scope와 MCP 세부 허용 경로의 교집합 안에서만 실행한다.
MCP 설정 동기화가 승인 범위를 확대하는 기능이 되어서는 안 된다.

### 4.4 실행 순서와 세션 수명

```text
Agent가 Catalog 조회 및 후보 생성
  → ActionProposal 생성
  → Control의 scope·ROE·승인 검사
  → ALLOW인 경우에만 ToolInvocation 생성
  → Core ToolGateway.invoke
  → MCP Adapter / MCP 서버 / Execution Substrate
  → ToolResult 정규화·schema 검사
  → Evidence 저장 및 Agent의 관측/Graph 처리
```

거절·승인 대기에서 MCP의 대상 접근/도구 실행이 발생하면 안 된다.
Agent는 nmap·브라우저·셸 등의 구현체를 직접 호출하지 않는다.

상태ful MCP 연결은 engagement 범위로 관리한다. composition root/runner가
연결을 열고 닫는 책임과 동일 async task 수명 규칙을 유지하고, 종료 시 진행 중인
호출을 정리한 뒤 연결을 닫는다. 다른 engagement와 인증 세션을 암묵적으로 공유하지 않는다.

### 4.5 오류와 결과

아래는 공동으로 맞춰야 할 오류 의미다. 표의 의미가 현재 양쪽에서 모두 구현됐다는 뜻은 아니다.

| 구분 | 전달해야 할 의미 |
|---|---|
| 작업 이름 오류 | namespace 미지원, 이름 형식 오류, 공통 목록 밖 operation |
| 도구 연결 오류 | 지원 operation이지만 연결된 도구가 없거나 요청 도구와 연결이 맞지 않음 |
| 대상 오류 | 알려지지 않은 대상, 대상 종류 불일치, 대상과 입력 인자의 불일치 |
| 입력 오류 | 필수 인자 누락, 타입·허용값·한도 위반 |
| 정책 결과 | 거절, 경고, 사람 승인 대기를 도구 실행 실패와 구분 |
| MCP 실행 오류 | 연결, timeout, 서버 실패, 출력 schema 불일치 등을 구분 |

현재 Compiler에는 연결 도구 부재를 `operation_unsupported`로 표현하는 경로도 있다.
공통 목록 밖 이름과 연결 부재를 구분하는 코드/필드 규격을 합의하고 관련 테스트를 맞춘다.
후속 구현에서는 지원 이름의 연결 부재를 `no_tool_for_operation`으로 분리했다.
dev의 안전한 diagnostic·redaction 처리를 유지하며 비밀키·인증정보·원문 응답을
오류 메시지나 모델 피드백에 그대로 넣지 않는다.

MCP SDK 객체는 Core 경계를 넘어가지 않으며 `ToolResult`로 정규화한다.
실제로 관측한 값만 결과에 넣고 모르는 값은 schema에서 정한 방식으로 누락/unknown/null 처리한다.
Agent는 schema와 실패 여부를 확인한 뒤 Graph/Evidence를 처리한다.
`success=True`는 도구 실행 성공이며 취약점 발견 또는 목표 완료 선언이 아니다.

## 5. MCP 팀 작업 목록

| ID | 우선순위 | 작업 | 완료 기준 |
|---|---|---|---|
| M1 | P0 | Settings·Manifest·Catalog·bootstrap을 단일 공통 작업 정의에 연결 | 등록과 조회가 동일한 지원 목록 사용. 미지원 연결은 실행 전 거부 |
| M2 | P0 | 최신 dev의 Catalog 최종 메타데이터 처리와 capa 기능 통합 | 모든 조회에서 operation·schema·대상 정보 일치 |
| M3 | P0 | 일반 MCP의 명시적 operation 연결 점검 | 원격 tool_name을 암묵적으로 행동 의미로 승격하지 않음 |
| M4 | P0 | 신규 profile과 Adapter 초기화 보존 | 파일·VDB·페이지·검색·실행 profile이 정확한 discovery/schema 검사 후 등록 |
| M5 | P0 | MCP 소유 코드와 테스트의 mypy 오류 수정 | Settings·배포 검증·VDB·방법론·실행 도구 관련 오류 제거 |
| M6 | P1 | 대상 인자 및 서버 정책 일치 검증 | 잘못된 Asset/origin/path는 거부. 설정 동기화로 scope 확대 안 됨 |
| M7 | P1 | 표준 ToolResult·실행 오류·출력 schema 점검 | 실제 결과 구조와 메타데이터 일치. 알 수 없는 값을 추측하지 않음 |
| M8 | P1 | 변경된 규격의 설정 예제·등록 문서 갱신 | 실제 적용 가능한 예제와 합의 상태 명시. 비밀값 미포함 |

현재 재현된 타입 오류의 예:

- `capability/settings.py`: 서로 다른 길이의 튜플 추론/대입 불일치.
- `app/mcp_deployment.py`: 전용 ID 타입 대신 일반 문자열을 사용.
- `capability/servers/vdb_extended.py`: None 가능 결과 접근 및 목록 타입 불일치.
- `capability/methodology.py`: Traversable 경로 결합 호출의 타입 규격 불일치.
- 관련 테스트: Protocol 미충족, Optional 확인 누락, 모호한 fixture 타입.

오류를 숨기는 전역 ignore 추가나 무분별한 Any 변환 대신 실제 계약·타입을 맞춘다.

## 6. Agent 팀 작업 목록

| ID | 우선순위 | 작업 | 완료 기준 |
|---|---|---|---|
| A1 | P0 | PlanStep·후보 공급자·Compiler·Engine을 공통 작업 정의에 연결 | MCP 등록에서 지원된 같은 이름을 계획에서도 검증 |
| A2 | P0 | Catalog 기반 후보/모델 메뉴 유지 | Core Port만 사용. 미연결·사용 불가 도구를 실행 가능하다고 표시하지 않음 |
| A3 | P0 | 최신 dev Engine에 capa 활성 조건 통합 | dev의 원본 후보·실행 전 조건 검사를 유지하면서 셸/분석 제한도 보존 |
| A4 | P0 | 오류 의미와 안전한 diagnostic 연결 | 미지원 작업·연결 부재·대상·인자 오류를 구분. 민감 입력 노출 없음 |
| A5 | P0 | Runner 승인 재개와 종료·복구 로직 통합 | 새 Runner에서도 승인 상태 재개 가능. 완료/취소 상태 재실행·중복 실행 방지 |
| A6 | P1 | 알려진 대상에서 schema에 맞는 입력 구성 | target/input 불일치 거부. 없는 경로·계정·Asset을 임의 생성하지 않음 |
| A7 | P1 | ToolResult의 관측·Evidence·Graph 해석 점검 | 도구 성공과 취약점/목표 완료를 분리. 외부 본문은 불신 데이터로 취급 |
| A8 | P1 | Agent 소유 코드와 테스트 타입 오류 수정 | 후보 공급자·연결·결과 처리 및 fixture의 Protocol/타입 정합성 확보 |

셸 도구의 현재 Agent bridge는 기본 off다. opt-in한 고정 `line_count` recipe의
`command_run`만 모델 메뉴에 노출된다. `command_job_*` 등록이 비동기 job의
Agent 자동 사용을 뜻하지 않는다. 확장 시 Agent lifecycle, 재개·취소·중복 실행과
운영 권한을 별도 설계한다. 단순 통합 과정에서 임의 shell 실행으로 넓히지 않는다.

## 7. 공동 통합 작업

| ID | 작업 | 주 담당/검수 | 지켜야 할 조건 |
|---|---|---|---|
| J1 | 공통 작업 정의 경로·이름·오류 규격 확정 | 변경 담당자 1명, 양 팀 검수 | 중복 정의 제거. 신규 이름의 승인 상태 기록 |
| J2 | `app/bootstrap.py` 충돌 해결 | 통합 담당자 1명, 양 팀 검수 | dev 런타임·영속성 검증과 capa MCP·방법론 연결 모두 보존 |
| J3 | `capability/bootstrap.py` 충돌 해결 | MCP 팀, Agent 검수 | 실제 연결/등록 기능과 공통 naming 검사 유지 |
| J4 | Engine·Compiler·Runner 충돌 해결 | Agent 팀, MCP 검수 | 실행 전 조건·안전한 오류·활성 제한·승인 재개 모두 보존 |
| J5 | 테스트 fixture 및 설정 예제 동기화 | 해당 코드 소유 팀 | 가짜 예제가 운영 profile과 충돌하지 않음. 명시적 연결 사용 |
| J6 | 병합 결과 전체 검증 및 PR 검수 | 양 팀 | capability 단독이 아니라 최신 dev와 합친 결과를 검증 |

CLI 기존 타입 문제는 최신 dev 수정분을 먼저 반영한다. 이후 남는 CLI 문제는
해당 담당자가 처리하며, MCP 기능 코드에서 CLI 오류를 우회하는 패치를 넣지 않는다.

## 8. 통합 검증과 완료 기준

### 필수 기능 시험

- [x] Settings·Manifest·Catalog·PlanStep·Compiler의 지원 이름 기준이 같다.
- [x] `page_read`, `web_search`의 실제 ToolId 연결과 필수 인자를 검증한다.
- [x] Catalog 조회별 최종 descriptor가 일치한다.
- [x] 미지원 작업과 연결 도구 부재를 서로 다른 원인으로 확인할 수 있다.
- [x] 대상 종류 오류·대상/인자 불일치·필수 인자 누락을 실행 전에 거부한다.
- [x] 거절·승인 대기에서는 대상 접근/도구 호출이 0회다.
- [x] 허용된 합성 대상에서는 Agent → Control → Gateway → 실제 MCP → Evidence 흐름이 성공한다.
- [x] 승인 후 새 Runner 재개, 완료/취소 후 실행 차단 및 중복 실행 방지를 검증한다.
- [x] 셸 opt-in·고정 recipe·분석 활성 조건이 통합 후에도 유지된다.
- [x] 검색 결과로 scope가 확대되거나 자동 방문되지 않는다.
- [x] MCP timeout·health·concurrency·출력 한도·dry-run 및 schema 실패 처리가 유지된다.

### CI 및 환경별 검증

- [x] CI 대상 Python 3.10/3.14의 로컬 clean snapshot에서 pytest 통과. GitHub CI와 구분.
- [x] CI와 같은 범위의 Ruff 검사 통과.
- [x] `mypy src/redteam tests` 통과.
- [x] Docker Compose 설정과 MCP 컨테이너 build 검증.
- [x] finance-lab의 MCP smoke 및 Agent/승인 gate smoke 통과.
- [x] 외부 공급자·실제 LLM·모바일/Execution Docker live 시험은 opt-in 미실시/skip으로 기록.

2026-10-03 로컬 검사 결과이며 GitHub CI 및 Agent 팀 최종 검수를 대신하지 않는다.
실행 환경·결과·남은 운영 검증은 [작업 결과](dev-capability-integration-result.md)에 기록했다.

동일한 코드에 대해 커밋, Python 버전, 실행 명령, 통과/실패/skip 이유를 남긴다.
skip 또는 fake 공급자 시험을 실제 외부 서비스 검증으로 표기하지 않는다.

## 9. 실행 순서와 인계 산출물

1. 최신 remote 확인 및 기존 작업 트리 변경 보존.
2. J1의 공통 naming·오류 규격을 먼저 합의.
3. capability에서 최신 dev를 통합하며 M1–M5, A1–A5, J2–J5 수행.
4. 인자·대상·출력·설정 접점 M6–M8, A6–A8 검증.
5. 최신 dev 병합 결과의 필수 시험과 CI 명령 실행.
6. 검증된 capability 커밋을 push하고 dev 대상 PR 작성.
7. 양 팀 검수 및 필수 CI 통과 후 병합 판단.

MCP 팀 인계물:

- 최종 tool_id/operation/schema/target/risk 메타데이터와 실제 discovery 검증 결과.
- 적용 가능한 서버/profile 설정 예제 및 명시적 도구 연결.
- 활성 조건, 제한, 미지원 기능, 실행/출력 오류 규격.

Agent 팀 인계물:

- Core Port 기반 후보 생성과 최신 Engine/Runner 통합 diff.
- 대상·인자 구성, 안전한 오류 전달, 승인 재개·중복 실행 방지 시험 결과.
- 결과 해석·Graph/Evidence 처리 및 별도 운영 설정 목록.

공동 인계물:

- 단일 공통 작업 명세 및 합의 상태.
- 충돌 해결 시 양쪽 기능 보존 설명.
- 병합 커밋 기준 전체 CI 결과와 남은 live 검증 항목.

## 10. 참고 코드와 문서

- [Core 도구 계약](../../src/redteam/core/contracts/tool.py)
- [Core Catalog Port](../../src/redteam/core/ports/tool_catalog.py)
- [Core Gateway Port](../../src/redteam/core/ports/tool_gateway.py)
- [행동·정책 계약](../contracts/actions.md)
- [도구 Manifest 계약](../contracts/tool-manifest.md)
- [capability 단독 공통 코드 통합 결과](common-code-integration-result.md)
- [현재 제한된 Execution Agent bridge](../../src/redteam/orchestration/execution_bridge.py)
- [dev 확인 커밋](https://github.com/zkfla/WHS-Red-Teaming-Agent/commit/afc529e)
- [원격 capability 확인 커밋](https://github.com/zkfla/WHS-Red-Teaming-Agent/commit/02d8b5c)

문서의 상대 링크는 해당 저장소 파일을 기준으로 한다. 통합 과정에서 공통 정의 경로가
바뀌면 계약 문서·호환 import·예제·테스트 링크도 함께 수정한다.
