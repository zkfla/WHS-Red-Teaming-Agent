# Capability 팀 기본 컴퓨터 도구·취약점 조사 연동 계획 및 3인 역할 분담

- 작성일: 2026-10-01
- 담당 범위: MCP Tool Registry / MCP Adapter / Execution Substrate
- 검토 기준: `origin/dev`의 `8979cac5f73de106c7433405cdb26c69ae3b4f74`
- 문서 성격: 구현 계획. 아래 도구 이름과 신규 분류는 제안이며, 구현 완료나 계약 확정을 뜻하지 않는다.
- 추가 반영: 사용자가 제공한 AppThreat VDB, EIP, GitHub MCP, m4vic·Rifteo Skills, Burp MCP, MobSF MCP 참고 워크플로우
- 외부 프로젝트 확인일: 2026-10-01. 공개 README 및 설정 문서 확인이며, 설치·코드 보안 감사·실제 실행 검증 완료를 뜻하지 않는다.
- 상세 보완: 이미지의 7개 항목을 기능별 작업 ID, 담당자, 입출력, 완료 기준, 지원·연동 상태로 분리했다. 아래 표는 계획이며 완료 보고가 아니다.

## 1. 개발 목표

보안 점검 도구뿐 아니라 Agent가 자료를 찾고, 읽고, 필요한 작업을 실행하고, 결과를 저장할 수 있는 기본 도구를 제공한다.

쉽게 말하면 보안 검사 장비에 더해, 파일을 열고 정리하고 저장할 수 있는 손발을 만드는 작업이다.

이번 범위는 다음과 같다.

- 작업공간 안의 폴더 조회, 파일 읽기, 검색, 작성, 수정
- 격리된 환경에서의 작업 명령 실행
- 웹검색과 허용된 페이지의 텍스트 읽기
- 기존 브라우저 도구의 등록 및 실제 호출 검증
- Registry 메타데이터, Adapter 결과 형식, 실행 권한, 테스트의 일관성 확보
- 취약점 지식, 공개 PoC·패치 자료, 조사 방법론, 대상 검증 도구의 단계적 연동

기본 컴퓨터 도구를 취약점 조사 도구로 대체하지 않는다. 파일·명령·검색 기반을 먼저 제공하고, 지식 조회와 실제 대상 검증을 별도 단계로 확장한다. 외부 원본의 기능 목록을 우리 Agent가 이미 사용할 수 있는 기능으로 표시하지 않는다.

## 2. 현재 상태와 부족한 부분

아래는 검토 기준 dev의 코드 및 연결 설정에 대한 판정이다. 개발자 개인 환경에 도구가 설치되어 있다는 사실과 프로젝트 Agent에 등록되어 있다는 사실은 구분한다.

| 기능 | 현재 상태 | 부족한 부분 |
|---|---|---|
| 일반 파일 읽기 | 범용 도구 없음 | 지정 APK 분석과 별개로 일반 문서·로그·결과 파일을 읽을 도구 필요 |
| 폴더 목록 및 파일 검색 | 범용 도구 없음 | 작업공간에서 파일 이름·내용을 탐색할 도구 필요 |
| 파일 작성 및 수정 | 범용 도구 없음 | 내부 저장 기능과 별개로 Agent가 호출할 작성·수정 도구 필요 |
| 일반 작업 명령 실행 | 범용 도구 없음 | 고정된 Nmap·ADB 등의 실행 코드와 별개로 격리된 실행 도구 필요 |
| 웹검색 | 없음 | 알려진 URL에 HTTP 요청하는 기능과 검색엔진 조회 기능은 다름 |
| 브라우저 조작 | 일부 연결 설정 있음 | Playwright의 이동·화면 읽기·클릭·입력 등에 대해 서버 실행, 선택, 실제 호출 확인 필요 |
| 증거 저장 | 내부 기반 있음 | 저장소가 있다고 범용 파일 도구까지 제공되는 것은 아님 |

관련 근거:

- `src/redteam/capability/servers/`: 현재 보안 도메인 MCP 서버 구현
- `deploy/mcp/finance/framework.env.example`: MCP 연결 및 도구 allowlist 설정
- `src/redteam/capability/settings.py`: 현재 지원하는 MCP 서버 종류
- `src/redteam/core/contracts/tool.py`: ToolDescriptor 및 ToolResult 계약
- `docs/contracts/actions.md`: capability, operation, 대상 및 정책 계약

## 3. 공통 계약과 선행 합의

세 사람은 아래 항목을 함께 정하고, 담당자 A가 공통 연결 변경을 관리한다.

| 항목 | 결정할 내용 | 합의 대상 |
|---|---|---|
| capability | 기존 분류 사용 또는 `workspace` 등 새 분류 도입 | PM, Control, Agent, Capability |
| operation | 도구 제품명과 분리된 행동 이름 | PM, Control, Capability |
| 실행 대상 | 작업공간·파일·명령 실행 작업의 대상 표현 | Core/계약 담당, Control, Agent |
| 작업공간 | 실행별 전용 폴더, 식별자, 접근 가능한 범위 | Capability, Control |
| 메타데이터 | 필수·선택 인자, 대상, 위험도, 입력·출력 schema | Capability, Agent |
| 권한 | 읽기·쓰기·실행·외부접속의 허용 및 승인 기준 | Control, Capability |
| 큰 결과 | 출력 제한, 잘림 표시, 파일 저장 및 참조 방식 | Capability, Evidence, Agent |
| 지식 조회 대상 | CVE·패키지·공개 저장소를 시험 대상과 구분해서 표현 | Core/계약 담당, Control, Agent |
| Skills 및 MCP 자원 | 방법론 제공 경로와 필요시 resources/prompts 소비 방식 | Capability, Agent |
| 외부 전송 | 제품명·버전·저장소 정보의 외부 조회 허용 기준 | Control, Capability |

현재 canonical capability는 `recon`, `web`, `mobile`, `analysis`이다. 새 분류를 도입한다면 설정과 Registry뿐 아니라 Agent의 capability 선택 및 Control 정책도 함께 맞춰야 한다. 계약을 확정하기 전에 새 분류가 이미 지원되는 것처럼 등록하지 않는다.

기존 Asset 대상 등을 사용할 수 있는지도 검토하되, 파일 경로를 웹 대상처럼 처리하거나 일반 컴퓨터 작업을 모두 `static_analysis`로 포장하지 않는다.

공통 규격을 논의하는 동안 각 담당자는 도구 내부 구현과 단위 테스트를 진행할 수 있다. 최종 연결은 확정된 규격을 사용한다.

## 4. 담당자 A — 파일 도구 및 공통 Registry 연결

### 4.1 책임

Agent가 작업공간의 파일을 찾고 읽고 작성하도록 만든다. 기존 Capability 구조를 아는 사람이 이 파트와 통합 관리를 맡는 것을 추천한다.

### 4.2 구현할 도구

| 제안 도구 이름 | 기능 | 사용 예 |
|---|---|---|
| `list_directory` | 폴더의 파일 및 하위 폴더 조회 | 어떤 검사 결과가 저장됐는지 확인 |
| `read_file` | 파일의 전체 또는 지정 범위 읽기 | JSON, 로그, 문서 확인 |
| `search_files` | 파일 이름 또는 텍스트 내용 검색 | 특정 오류가 포함된 로그 찾기 |
| `write_file` | 새 파일 작성 또는 명시적으로 허용한 덮어쓰기 | 분석 결과를 Markdown으로 저장 |
| `apply_patch` | 기존 텍스트 파일의 제한된 부분 수정 | 보고서 내용 수정 |

### 4.3 구현 조건

- 실행별로 지정된 작업공간 안에서만 접근한다.
- 상대 경로 이탈과 심볼릭 링크를 통한 작업공간 밖 접근을 차단한다.
- 파일 크기, 검색 범위, 읽기 범위와 반환 크기를 제한한다.
- 내용이 잘린 경우 이를 명시한다.
- 바이너리 파일을 무조건 텍스트로 해석하지 않는다.
- 덮어쓰기 및 수정 충돌을 명확하게 처리한다.
- 파일 없음, 접근 거절, 인코딩 문제, 크기 초과를 구분해 반환한다.
- 수정 작업은 가능한 한 원자적으로 처리하고 다른 실행의 파일과 섞이지 않게 한다.

### 4.4 공통 코드 소유권

다음 공통 변경은 담당자 A가 통합 관리한다. 다른 담당자가 변경해야 할 경우 먼저 조율한다.

- `src/redteam/capability/settings.py`
- `src/redteam/capability/bootstrap.py`
- Registry, manifest, 공통 Adapter 경계
- 공통 연결 설정과 Capability 계약 문서

Core·Control 계약 변경은 해당 담당 팀의 검토를 받는다.

### 4.5 산출물 및 완료 기준

- 파일 도구 서버 또는 구현 모듈
- 연결 설정과 도구 메타데이터
- 정상 읽기·쓰기·검색 및 경계 위반 테스트
- 작업공간 사용 안내
- 공통 Registry 경로로 도구가 발견되고 호출됨
- 작업공간 밖 접근 및 다른 실행의 작업공간 접근이 차단됨

### 4.6 취약점 조사 확장 책임

- AppThreat VDB의 로컬 저장 위치, DB 준비·갱신, 조회 allowlist, health·coverage·freshness 전달을 관리한다.
- CVE·패키지 조회 대상을 Registry와 Control 계약에 연결한다.
- m4vic·Rifteo 방법론은 검토된 버전의 읽기 전용 자료로 제공하는 방안을 Agent 팀과 정한다. Skill을 실행 Tool처럼 등록하지 않는다.
- 기본 파일 도구가 안정화된 뒤 확장하며, VDB 갱신·자동 다운로드는 일반 조회와 분리된 운영 권한으로 관리한다.

## 5. 담당자 B — 명령 실행 및 Execution Substrate

### 5.1 책임

Agent에게 격리된 프로젝트 작업용 터미널 기능을 제공한다. 개발자 개인 컴퓨터 전체를 조작할 권한을 주는 작업이 아니다.

### 5.2 구현할 기능

| 기능 | 설명 |
|---|---|
| `run_command` | 허용된 실행 파일과 인자로 작업 명령 실행 |
| 실행시간 제한 | 제한시간을 넘는 작업 종료 |
| 출력 제한 | stdout 및 stderr의 크기를 제한하고 잘림 표시 |
| 실행 취소 | 작업 및 관련 자식 프로세스 종료 |
| 상태 조회 | 비동기 작업의 진행·완료·실패 상태 확인 |

먼저 짧고 제한된 동기 실행을 검증한다. 장시간 작업은 작업 ID 기반 시작·조회·취소 방식으로 확장한다. Gateway의 timeout 및 취소 처리와 일치해야 한다.

### 5.3 구현 조건

- 명령은 검증된 격리 실행 환경에서 실행한다.
- 실행별 작업공간 및 working directory를 지정한다.
- 실행 파일과 인자를 분리하고, 임의 문자열을 기본적으로 shell에 넘기지 않는다.
- 허용할 실행 파일 및 인자 범위를 명시한다.
- 개인 인증정보와 전체 환경변수를 그대로 전달하지 않는다.
- CPU, 메모리, 프로세스, 실행시간, 출력 크기의 제한을 적용한다.
- 외부접속 정책을 실행 환경에서도 적용한다. 쉘 명령으로 HTTP MCP의 범위 제한을 우회할 수 없어야 한다.
- 시간 초과, 취소, 연결 종료 시 관련 프로세스를 정리한다.
- 격리 환경을 사용할 수 없으면 호스트 실행으로 자동 대체하지 않는다.

### 5.4 반환 정보

최소한 다음 구조화된 정보를 제공한다.

```text
exit_code
stdout
stderr
timed_out
truncated
duration
job_id       # 비동기 실행일 경우
artifact_ref # 결과를 파일로 저장하는 계약이 마련된 경우
```

큰 출력은 승인된 작업공간에 저장하고 담당자 A의 읽기 도구로 확인할 수 있게 한다. 파일 참조의 형식과 접근 권한은 공통 계약을 따른다.

### 5.5 산출물 및 완료 기준

- 명령 실행 도구 및 실행 환경 설정
- 명령·인자·자원·네트워크 제한 안내
- 정상 실행, 실행 실패, 시간 초과, 출력 제한, 취소 테스트
- 허용된 명령이 격리 환경에서 정상 실행됨
- 시간 초과와 취소 시 자식 프로세스까지 실제로 종료됨
- 다른 실행의 작업과 인증정보에 접근하지 못함

### 5.6 취약점 조사 확장 책임

- MobSF 본체와 MCP 래퍼의 실행 환경, 저장공간, API 키, 작업 상태 조회를 검토한다.
- 모바일 담당 팀과 협의해 승인된 APK의 정적 분석부터 연결한다.
- 동적 분석은 에뮬레이터·기기·ADB 연결이 준비된 별도 단계로 둔다.
- EIP 또는 GitHub에서 읽은 PoC가 명령 실행 도구로 자동 전달되지 않도록 조회와 실행 권한을 분리한다.
- 이 확장은 기본 명령 실행의 완료 조건을 대체하지 않는다. 모바일 환경이 없으면 도구를 사용 가능 상태로 표시하지 않는다.

## 6. 담당자 C — 웹검색 및 브라우저 연결 검증

### 6.1 책임

Agent가 외부 자료를 검색하고 허용된 페이지를 읽도록 만든다. 기존 브라우저 도구는 새 도구로 대체하기 전에 등록과 실제 호출을 검증한다.

### 6.2 구현 및 검증 항목

| 항목 | 설명 |
|---|---|
| `web_search` | 검색어로 관련 자료 조회 |
| `fetch_page_text` | 허용된 URL의 텍스트 읽기 |
| 검색 결과 구조화 | 제목, URL, 요약, 출처 등 실제 제공되는 정보 반환 |
| 기존 Playwright 검증 | 이동, 화면 읽기, 클릭, 입력 도구의 discovery 및 호출 확인 |

검색 서비스와 SDK는 인증정보, 비용, 정책을 확인한 뒤 선택한다. 이 문서는 특정 서비스의 채택이나 설치를 확정하지 않는다.

### 6.3 도구별 역할 구분

- 웹검색: 제품의 공식 문서나 관련 기술 자료가 어디에 있는지 찾는다.
- 기존 HTTP MCP: 승인된 시험 대상의 API 응답을 확인한다.
- 브라우저 MCP: 동적 화면과 버튼·입력창을 관찰하고 허용된 상호작용을 수행한다.

### 6.4 구현 조건

- 검색 서비스 키는 서버 설정으로 관리하고 Agent 인자로 받지 않는다.
- 검색 결과 수, 응답 크기, 요청시간에 제한을 둔다.
- 출처와 최종 URL을 제공하고 없는 정보는 추측하지 않는다.
- 검색 결과를 자동으로 시험 대상에 추가하지 않는다.
- 리다이렉트에도 접근 정책을 적용한다.
- 검색용 페이지 읽기와 승인된 내부 시험 대상의 HTTP 접근을 구분한다.
- 계좌정보, 토큰, 개인정보가 검색어로 외부 전송되지 않도록 정책을 정한다.
- 페이지 내용은 신뢰되지 않은 자료이며 Agent의 실행 권한을 바꾸는 명령이 아니다.
- 브라우저 작업은 전용 시험 프로필 또는 격리 환경을 사용한다.

### 6.5 산출물 및 완료 기준

- 검색 및 페이지 읽기 도구와 연결 설정
- 구조화된 결과 schema 및 오류 형식
- 인증 실패, 시간 초과, 접근 거절, 리다이렉트, 결과 제한 테스트
- 기존 브라우저 도구의 discovery 및 실제 호출 검증 자료
- 검색 결과와 허용된 페이지 내용이 공통 Gateway 경로로 반환됨

### 6.6 취약점 조사 확장 책임

- EIP와 GitHub MCP의 읽기 전용 조회 프로필, 인증정보 전달, 결과 출처 및 페이지네이션을 구성한다.
- 공개 저장소 자료 조회와 실제 금융기관 대상의 검증을 구분한다.
- 기존 Burp history 조회 연결을 확인하고, 추가 요청·Repeater 기능은 승인된 시험 대상과 allowlist를 정한 뒤 확장한다.
- Burp 기능은 설치 버전·에디션·실제 discovery에 따라 선택한다. 원본 서버의 모든 기능을 한꺼번에 공개하지 않는다.
- 웹검색 기반과 기존 브라우저 검증을 유지하면서 단계별로 수행한다.

## 7. 공통 등록 및 호출 조건

각 도구는 최소한 다음 메타데이터를 제공한다.

- tool_id 및 capability
- canonical operation
- description
- input_schema와 그로부터 도출한 필수·선택 인자
- target_types
- risk_level과 health_status
- output_schema

각 도구의 결과는 기존 ToolResult 계약으로 정규화한다. 신규 공통 필드가 필요하면 별도 계약 변경으로 검토한다.

Agent는 실제 MCP 서버 구현을 직접 호출하지 않는다. 기존 공통 포트와 Registry를 통해 후보를 확인하고 ActionProposal을 만들며, Control Plane의 허용 판단 이후 Gateway와 Adapter를 통해 실행한다.

도구가 Registry에 보이는 것, 실제 호출이 성공하는 것, Agent가 자율적으로 선택해 사용하는 것은 서로 다른 검증 단계이다.

MCP 서버가 제공하는 tools, resources, prompts와 Skill 파일은 별개의 인터페이스다. 현재 tools 경로가 있다는 이유로 나머지도 자동 소비된다고 가정하지 않는다. 필요한 resources/prompts 지원 여부를 확인하고, 초기 범위에서는 검증된 Tool 조회 결과와 읽기 전용 방법론 자료를 우선 사용한다.

## 8. 공동 통합 테스트

### 8.1 파일 및 실행 시나리오

1. 실행 전용 작업 폴더의 목록을 조회한다.
2. 합성 입력 파일을 읽는다.
3. 허용된 명령으로 입력을 가공한다.
4. 결과 파일을 저장한다.
5. 저장된 파일을 읽어 결과를 확인한다.

### 8.2 웹 자료 시나리오

1. 민감하지 않은 검색어로 자료를 검색한다.
2. 허용된 결과 URL을 선택한다.
3. 페이지 텍스트를 읽는다.
4. 출처와 요약을 작업공간에 저장한다.

### 8.3 필수 점검

- discovery schema와 Registry의 필수·선택 인자가 일치한다.
- operation, 대상, 위험도, 입력·출력 schema가 일치한다.
- Agent가 MCP 구현체를 직접 호출하지 않는다.
- Control Plane이 거절한 작업은 실행되지 않는다.
- 파일 경계 위반, 명령 제한 위반, 외부접속 위반이 차단된다.
- 실패, 시간 초과, 취소, 출력 잘림을 구별할 수 있다.
- 실제 도구 호출과 Agent의 선택·실행을 각각 검증한다.

새 capability나 대상 표현을 도입한 경우 Agent 팀과 종단간 테스트를 진행한다. discovery 테스트 통과만으로 Agent 연동 완료를 선언하지 않는다.

### 8.4 취약점 조사 확장 시나리오

1. 합성 제품명·버전 또는 합성 패키지 식별자로 VDB를 조회한다.
2. 조회 결과의 출처, 영향 버전, DB 상태를 확인한다.
3. EIP·GitHub에서 관련 공개 설명·PoC·패치 자료를 읽되 실행하지 않는다.
4. 검토된 방법론으로 가설과 필요한 검증 조건을 정리한다.
5. 승인된 합성 웹 환경이면 Burp로 허용된 관찰 또는 요청을 수행한다.
6. 승인된 APK가 제공된 모바일 환경이면 MobSF 정적 분석을 별도로 수행한다.
7. 참고 지식, 가설, 실제 관측, 확인되지 않은 부분을 구분해 저장한다.

추가 테스트는 DB 미준비·오래된 데이터·coverage 부족, GitHub 인증·rate limit, PoC 조회 후 자동 실행 차단, Burp 범위 밖 요청 거절, MobSF 미설정·비인가 APK 처리 거절을 포함한다.

## 9. 팀 간 책임 분담

| 팀 | 책임 |
|---|---|
| Capability | 기본 도구 및 지식 조회 Adapter, Registry 메타데이터, 격리 실행 환경, Burp·MobSF 연결, 테스트 |
| PM / Core 계약 담당 | 분류, 행동 이름, 대상 및 공통 결과 계약 승인 |
| Control | 읽기·쓰기·명령 실행·외부접속의 정책과 승인 기준 |
| Agent / Orchestration | catalog 소비, 후보 생성, 신규 행동·대상 선택, Skill 선택·로드 및 조사 추론, 종단간 실행 검증 |
| Evidence 담당 | 저장 파일 및 artifact 참조의 접근·보존 계약 협의 |

## 10. 브랜치와 통합 방식

최신 dev를 반영한 공통 기준 commit에서 개인 작업 브랜치를 만든다. 세 사람이 같은 capability 브랜치에 동시에 직접 push하는 방식은 피한다.

| 담당자 | 브랜치 이름 예시 | 소유 범위 |
|---|---|---|
| A | `capability/workspace-tools` | 파일 도구·공통 연결 우선, 이후 VDB 및 방법론 제공 경로 |
| B | `capability/execution-tools` | 격리 명령 실행 우선, 이후 MobSF 실행 기반 및 모바일 팀 협의 |
| C | `capability/search-tools` | 웹검색·브라우저 우선, 이후 EIP·GitHub 조회 및 Burp 검증 연결 |

브랜치 이름은 제안이다. 기존 작업과 팀 정책을 확인하고 확정한다.

진행 순서:

1. 현재 변경을 보존하고 최신 dev를 공통 기준에 반영한다.
2. capability, operation, 대상, 권한, 결과 계약을 확정한다.
3. 기준 commit을 공유하고 개인 브랜치에서 병렬 구현한다.
4. 각자 단위 테스트 및 MCP discovery·호출 테스트를 수행한다.
5. 담당자 A가 공통 연결을 정리하고 capability에 통합한다.
6. 공동 시나리오와 정책 거절 테스트를 수행한다.
7. Agent 팀과 선택·실행 경로를 확인한다.
8. 검증 결과를 포함해 capability에서 dev로 PR을 올린다.

## 11. 우선순위와 후속 범위

### 우선 구현

- 공통 계약과 Registry 연결
- 작업공간 목록, 읽기, 검색, 작성
- 격리된 짧은 명령 실행과 시간·출력 제한
- 웹검색 및 허용된 페이지 읽기
- 기존 브라우저 연결 확인
- 경계 위반 및 정책 거절 테스트

### 후속 확장

- AppThreat VDB 조회 및 DB 상태 전달
- EIP·GitHub의 읽기 전용 취약점·PoC·패치 자료 조회
- 검토된 m4vic·Rifteo 방법론 제공과 Agent 팀의 로드 검증
- 기존 Burp 관찰 연결의 재사용 및 승인된 요청·검증 기능 확장
- 모바일 환경 및 APK 제공 시 MobSF 정적 분석, 이후 동적 분석
- 파일 수정 충돌 및 patch 기능 고도화
- 장시간 작업 시작·조회·취소
- 대형 결과의 artifact 저장·보존·재조회
- 추가 인코딩과 바이너리 파일 처리
- 브라우저 세션 관리 및 복구 고도화

### 이번 범위에서 제외

- 개인 컴퓨터 전체 파일 접근
- 제한 없는 호스트 shell 실행
- 개인 Chrome 프로필 전체 제어
- 데스크톱 전체 조작 자동화
- 금융 인증 우회 및 실제 거래 자동화
- 조회한 공개 PoC의 자동 다운로드·실행
- 공개 DB 검색 결과만으로 금융 보안솔루션 제품·버전이나 실제 취약 여부 확정

## 12. 최종 완료 체크리스트

- [ ] 공통 계약 및 권한 기준 확정
- [ ] 파일 도구 구현 및 등록
- [ ] 명령 실행 환경 구현 및 등록
- [ ] 웹검색 및 페이지 읽기 구현 및 등록
- [ ] 기존 브라우저 실제 호출 확인
- [ ] discovery와 manifest 일치 검증
- [ ] 작업공간 및 실행 격리 검증
- [ ] 정책 거절과 실패·취소·시간 초과 검증
- [ ] 파일·명령·검색 통합 시나리오 통과
- [ ] Agent 팀과 도구 선택·실행 검증
- [ ] 연결 설정, 실행 방법, 제한사항 문서화
- [ ] 기존 사용자 변경 보존 및 결과 commit 기록

확장 단계 체크리스트:

- [ ] 외부 프로젝트별 버전·출처·라이선스·인증·배포 조건 기록
- [ ] VDB 조회와 DB 상태·coverage 검증
- [ ] EIP·GitHub 읽기 전용 allowlist와 결과 출처 검증
- [ ] 방법론 자료 검토 및 Agent 팀의 선택·로드 검증
- [ ] Burp 조회 연결과 추가 요청 기능의 상태 분리
- [ ] 모바일 환경이 준비된 경우 MobSF 정적 분석 검증
- [ ] PoC 자동 실행 및 미승인 대상 검증 차단
- [ ] 지식 조회 결과와 실제 관측 증거의 구분 검증

## 13. 참고 워크플로우의 적용 방식

### 13.1 실제 역할과 도입 상태

다음 역할은 각 원본 문서에서 확인한 설명이다. 아래 대상의 설치 및 우리 프레임워크 연동 완료를 뜻하지 않는다. m4vic·Rifteo는 실행 MCP가 아니라 조사 절차를 설명하는 Skill 자료다.

| 대상 | 유형 | 계획상 역할 | 도입 및 검증 조건 |
|---|---|---|---|
| [AppThreat VDB](https://github.com/AppThreat/vulnerability-db/blob/master/packages/mcp-server-vdb/README.md) | 로컬 취약점 DB + MCP, 사용 가이드 Skill | NVD·OSV·GitHub/GHSA 지식 조회, CVE·URL·PURL·CPE·패키지·SBOM 처리 | 입력별 원본 대응 확인, DB 준비, coverage·freshness 확인 |
| [EIP MCP](https://github.com/exploitintel/eip-mcp) | 읽기 전용 지식 조회 MCP | ExploitDB·Metasploit·GitHub PoC, 소스·재현 환경 자료 조회 | 패치 분석은 수집 자료와 Agent 해석 구분, 자동 실행 금지 |
| [GitHub MCP](https://github.com/github/github-mcp-server) | GitHub 공식 MCP | 코드 검색, repository·commit·PR·Security Advisory 조회, 패치 diff·upstream 추적 | 읽기 전용·최소 권한, 실제 Tool 및 schema 확인 |
| [m4vic/bug-hunting](https://github.com/m4vic/bug-hunting) | 커뮤니티 방법론 Skill | Web/API/Mobile/Cloud/AI 신호별 가설·검증 방법 선택 | 분야별 자료 검토, Agent 로드 및 가설 기록 검증 |
| [Rifteo Skills](https://github.com/Rifteo/skills) | Rifteo의 방법론 Skill 모음 | check-exploit·attack-surface·vuln-diagnose, IDOR·SSRF·SSTI 및 보고 절차 | 선별·검토, 지원 도구 및 승인 정책과의 대응 확인 |
| [Burp MCP](https://github.com/PortSwigger/mcp-server) | PortSwigger 공식 MCP 확장 | Proxy history, HTTP 요청, Repeater/Intruder 탭 전달, WebSocket history, Collaborator | 각 기능별 discovery·에디션·권한 검증, 전달과 실행 구분 |
| [MobSF](https://github.com/MobSF/Mobile-Security-Framework-MobSF) + [MCP 래퍼 후보](https://github.com/plur1bu5/mobsf-mcp) | 모바일 분석 서비스 + 제3자 MCP 래퍼 | APK 정적·동적 분석, manifest·permission·secret·network 설정 분석 | 승인 APK, API 키, 결과 schema, 동적 환경 별도 검증 |

MobSF 본체와 MCP 래퍼는 다른 프로젝트다. 위 래퍼를 MobSF 공식 MCP로 단정하지 않으며, 검토 후 자체 API Adapter로 연결하는 선택지도 둔다. 공개 프로젝트의 공식 여부는 보안 검증 완료를 뜻하지 않는다.

사용자가 이전에 제공한 Burp 로그에서는 로컬 history 조회 도구 4개의 discovery와 연결 테스트가 통과했다. 이는 당시 로컬 연결의 근거이며, 현재 dev의 등록 상태나 요청 전송·Repeater·Intruder·Collaborator까지 검증됐다는 근거는 아니다.

### 13.2 조사 순서와 대상별 분기

이 흐름은 참고용 조사 순서다. 모든 요청마다 모든 도구를 차례로 호출하는 고정 파이프라인으로 구현하지 않는다.

1. 승인된 관측 결과나 제공받은 식별정보를 확보한다. 제품·버전이 불명확하면 그 상태를 기록한다.
2. AppThreat VDB로 알려진 취약점 후보를 조회한다. 패키지나 SBOM이 없으면 패키지 기반 분석 범위를 제한한다.
3. EIP·GitHub에서 공개 설명·PoC·패치 자료를 조회해 영향 조건과 수정 근거를 조사한다.
4. m4vic·Rifteo 방법론 중 관련 부분을 참고해 가설과 검증 조건을 정리한다. Skill은 실행 권한을 부여하지 않는다.
5. Control Plane이 검증 행동을 승인한 경우에만 대상별 실행 도구를 사용한다.
   - 웹/API: Burp의 관찰 또는 승인된 제한적 요청으로 실제 응답을 확인한다.
   - 모바일: 승인된 APK를 확보한 경우 MobSF 정적 분석을 수행한다. 동적 분석은 준비된 시험 기기에서 별도 승인한다.
6. 조회 자료, 가설, 실제 관측, 확인되지 않은 조건을 구분하고 작업공간 및 증거 저장소에 기록한다.

MobSF는 Burp 다음에 반드시 실행하는 단계가 아니라 모바일 대상일 때의 별도 분기다. EIP 조회 결과와 GitHub PoC는 실행 성공이나 해당 대상의 취약성을 보증하지 않는다.

### 13.3 소스코드를 제공받지 못하는 금융권 환경의 적용 한계

- VDB는 공개 취약점 지식이지 대상 서버의 제품·버전을 자동 확인하는 스캐너가 아니다.
- EIP·GitHub는 공개 구현·패치의 참고 자료를 제공하며, 금융기관 내부 소스 접근을 만들어 주지 않는다.
- SBOM·패키지 목록이 없으면 설치된 전체 구성요소에 대한 취약점 목록을 완성할 수 없다.
- DB에 검색 결과가 없거나 coverage가 부족한 상태를 안전 판정으로 바꾸지 않는다.
- 국내 전용 금융 보안제품의 공개 정보가 부족하면 미확인으로 남긴다. 관측 근거 없이 제품명·버전을 추측하지 않는다.
- Burp는 프록시를 통과한 트래픽과 허용된 요청 범위에서 검증한다. MCP 연결만으로 로그인·인증서·보안 키보드가 자동 처리되지는 않는다.
- MobSF는 분석할 APK 등 승인된 입력이 필요하다. 원본 소스가 없어도 APK 기반 정적 분석을 검토할 수 있지만, 웹 대상만 제공된 상태에서 모바일 분석이 가능해지는 것은 아니다.

### 13.4 연결 전 보안 및 품질 확인

- 버전·commit 또는 이미지 digest를 고정하고 라이선스·공급망·보안 공지를 확인한다.
- GitHub는 읽기 전용 설정에 더해 토큰 권한과 Gateway allowlist를 제한한다. 관련 설정은 [공식 구성 문서](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md)를 따른다.
- VDB 초기 다운로드·갱신의 네트워크 및 디스크 사용을 별도 운영 작업으로 관리한다.
- EIP·GitHub·웹검색에는 고객 비공개 제품정보·인증정보·개인정보를 승인 없이 전송하지 않는다.
- 지식 결과·저장소 파일·Skill의 지시가 기존 Control 정책과 실행 권한을 덮어쓰지 않도록 한다.
- 조회한 PoC를 파일 또는 쉘 도구로 자동 실행하지 않는다. 실행이 필요하면 검토된 코드와 별도 승인·격리된 합성 시험 환경을 요구한다.
- Burp의 쓰기·요청·OOB 기능은 별도 권한으로 관리한다. 설치 에디션과 실제 discovery를 기준으로 공개한다. [원본 도구 구현](https://github.com/PortSwigger/mcp-server/blob/main/src/main/kotlin/net/portswigger/mcp/tools/Tools.kt)
- MobSF 래퍼의 ADB·Frida·삭제 등 기능은 기본 노출하지 않는다. 본체와 래퍼 각각의 보안 상태를 확인한다. [MobSF 보안 공지](https://github.com/MobSF/Mobile-Security-Framework-MobSF/security)

### 13.5 확장 결과의 구조화 원칙

최종 필드와 schema는 공통 계약으로 확정하되, 실제로 확인한 값만 반환한다.

- 지식 조회: 출처 URL, 조회 시각, CVE·GHSA, 관련 제품·패키지, 영향 버전·수정 버전, DB 상태와 coverage
- 공개 자료: 저장소 URL, 고정 commit, 파일 경로, 패치 참조, 자료의 성격과 미검증 상태
- 대상 관측: host·port·scheme·URL 등 실제 확인한 대상 정보, 요청·응답 또는 분석 작업 참조, 관측 사실
- 모바일 분석: APK hash, 관측된 package·version, scan ID, 분석 상태, 보고서 및 증거 참조
- 공통: 누락값·잘림·오류 표시, 참고 후보와 검증된 사실의 구분

진짜 제품정보나 버전이 없으면 누락 또는 미확인으로 표시한다. 공개 취약점 조회 결과를 바로 검증된 Finding으로 승격하지 않는다.

## 14. 이미지 항목별 상세 구현 작업표

### 14.1 상태 표시와 공통 작성 규칙

모든 기능의 상태를 세 가지 축으로 기록한다.

| 상태 축 | 기록할 값 | 판정 근거 |
|---|---|---|
| 원본 지원 | 문서·코드 확인 / 조건부 / 미확인 / 미지원 | 버전·commit, 원본 문서, 실제 discovery |
| 우리 프로젝트 연결 | 미검증 / 등록 확인 / 실제 호출 확인 | 기준 브랜치·commit, 환경 설정, 호출 결과 |
| Agent 사용 | 미검증 / 후보 노출 확인 / 선택·정책·실행 확인 | Agent 및 Control 공동 테스트 |

아래 신규 확장 항목은 별도 명시가 없는 한 우리 프로젝트 연결과 Agent 사용 모두 미검증이다. 계획서 수정만으로 상태를 올리지 않는다. 원본 지원은 확인일의 자료에 대한 판단이고, 채택할 고정 버전에서 다시 확인한다.

작업표의 입출력은 우리 팀의 제안이다. 실제 원본 Tool명·필수 인자·출력 schema는 discovery로 확인해 manifest에 매핑한다. 원본에 없는 기능은 명시적인 Adapter 보완 작업으로 남기며, 가상의 remote ToolId를 등록하지 않는다.

### 14.2 AppThreat VDB — 담당자 A

원본은 NVD·OSV·GitHub 등의 데이터와 여러 식별자 검색을 설명한다. MCP의 batch/BOM·상태 응답은 문서에서 확인했으며, 각 입력을 어떤 Tool로 받을지는 고정 버전에서 검증한다. [DB 문서](https://github.com/AppThreat/vulnerability-db), [MCP 문서](https://github.com/AppThreat/vulnerability-db/blob/master/packages/mcp-server-vdb/README.md)

연결 방식: 로컬 MCP 및 전용 DB 저장공간. 조회 프로필과 관리용 갱신 프로필을 분리한다.

| 작업 ID | 개발할 기능 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| VDB-01 | NVD·OSV·GHSA/GitHub 출처 식별·필터 | 출처·필터 → 출처별 결과·참조 | 출처 보존, 지원하지 않는 필터 명시 |
| VDB-02 | CVE·GHSA 단건 조회 | 식별자 → 설명·영향 조건·출처 | 알려진 합성 fixture와 일치, 잘못된 ID 오류 구분 |
| VDB-03 | URL·PURL·CPE·package 조회 | 식별자·버전 → 후보·매칭 근거 | 각 입력 유형의 지원 경로·버전 비교 테스트 |
| VDB-04 | 패키지 목록 bulk 조회 | 제한된 목록 → 항목별 결과·coverage | 부분 실패와 미조회 항목 표시 |
| VDB-05 | SBOM bulk 조회 | 승인된 CycloneDX 파일 참조 → 구성요소별 결과 | 파일 경계·크기·지원 형식 검증 |
| VDB-06 | DB health·metadata·갱신 관리 | 상태 요청 → 준비·갱신·coverage 정보 | DB 비어 있음·오래됨·미포함을 안전 판정과 구분 |
| VDB-07 | VDB SKILL.md 사용 가이드 제공 | 고정 자료 ID → 검토된 안내 | 원본 출처·버전 포함, 로드 여부 Agent 팀 확인 |

GHSA는 식별자이며 원본의 데이터 출처 이름은 GitHub 등으로 나타날 수 있다. 출처 문자열을 추측해서 설정하지 않는다. CPE·CVE·텍스트 검색의 DB 범위 및 metadata 조건도 확인한다. SBOM과 패키지 목록은 제공된 경우에만 사용하며, 대상의 전체 목록을 자동으로 확보한 것처럼 표시하지 않는다.

### 14.3 EIP — 담당자 C, 실행 경계 검토 담당자 B

원본은 ExploitDB·Metasploit·GitHub PoC, 제한된 소스 읽기, Docker 재현 환경 자료 검색을 설명한다. 읽기 전용이며 조회한 내용을 실행하는 기능과는 다르다. [원본 README](https://github.com/exploitintel/eip-mcp), [Tool 가이드](https://github.com/exploitintel/eip-mcp/blob/main/docs/user-guide.md)

연결 방식: 승인된 외부 MCP 또는 로컬 stdio. 전송할 조회 정보와 허용 Tool을 먼저 정한다.

| 작업 ID | 개발할 기능 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| EIP-01 | CVE와 ExploitDB·Metasploit·GitHub PoC 연결 조회 | CVE → 자료별 ID·URL·종류 | 서로 다른 출처와 중복·누락 구분 |
| EIP-02 | PoC source 읽기 | 확인된 자료·파일 ID → 제한된 텍스트·출처 | 크기 제한·잘림·읽기 실패 반환, 자동 실행 없음 |
| EIP-03 | 재현 환경 지식 조회 | CVE·자료 ID → 환경 설명·참조 | 환경 조회와 환경 실행 상태 구분 |
| EIP-04 | 공격 조건·패치 비교 자료 수집 | CVE·제품·공개 참조 → 근거 목록 | 직접 제공값과 Agent의 해석을 구분 |
| EIP-05 | corpus 상태·페이지네이션 처리 | 상태·cursor → 상태·다음 결과 | 불완전 자료·cursor·rate limit 처리 검증 |

EIP-04의 패치 비교는 수집 자료를 바탕으로 Agent가 수행할 분석이다. 전용 패치 분석 Tool이나 신뢰도 판정 기능이 있다고 가정하지 않는다. PoC가 존재해도 현재 대상에서 성공한다고 판단하지 않는다.

### 14.4 GitHub MCP — 담당자 C, schema·권한 검토 담당자 A

공식 서버의 코드 검색 및 global Security Advisory 조회를 확인했다. 아래 예시 Tool은 채택 버전과 토큰 권한에서 다시 discovery한다. [원본 Tool 목록](https://github.com/github/github-mcp-server), [읽기 전용 구성](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md)

연결 방식: 공식 MCP의 읽기 전용 프로필. Tool allowlist와 최소 권한 토큰을 함께 적용한다.

| 작업 ID | 개발할 기능 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| GH-01 | repository 및 코드 검색 | 공개 범위·검색어 → repository·파일·참조 | 검색 범위 제한, rate limit·빈 결과 구분 |
| GH-02 | 공개 파일·commit·PR 조회 | repository·고정 ref → 본문·변경 참조 | 자료의 commit과 URL 보존 |
| GH-03 | Security Advisory 조회 | CVE·GHSA·패키지 → advisory·버전 조건 | 미존재·권한 부족·영향 조건 구분 |
| GH-04 | 취약 코드·patch diff 비교 자료 제공 | 비교 ref 또는 PR → diff·출처 | 크기 제한, 변경 사실과 취약성 해석 구분 |
| GH-05 | upstream 변경 추적 | 기준 commit·범위 → 이후 변경·참조 | 기준 고정, 최신 변경 누락·실패 표시 |

`search_code`, `get_global_security_advisory`, `list_global_security_advisories`는 원본 예시이며 최종 ToolId가 아니다. GH-04/05는 조회 Tool 조합이 필요할 수 있다. 조회를 위해 issue 작성·PR 수정·merge 권한을 공개하지 않는다. 반복 모니터링 스케줄러는 이 작업 범위에 포함하지 않는다.

### 14.5 m4vic bug-hunting — 자료 제공 담당자 A, 사용 구현 Agent 팀

원본은 분야별 router와 신호 기반 참고 자료 선택, 가설 기록, 확인 후 보고 흐름을 설명한다. MCP 서버가 아니다. [원본 방법론 구조](https://github.com/m4vic/bug-hunting)

연결 방식: 검토한 고정 버전 자료를 읽기 전용으로 제공한다. Agent 팀이 분야 선택·로드·추론을 구현한다.

| 작업 ID | 제공할 기능·자료 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| M4-01 | Web/API/Mobile/Cloud/AI 분야 인덱스 | 자료 목록 요청 → 분야·자료 ID·버전 | 모든 분야의 경로·출처 기록 |
| M4-02 | 관측 → 가설 → 검증 방법 참조 | 분야·관측 분류 → 관련 자료 참조 | 관련 자료만 선택하는 Agent 테스트 |
| M4-03 | 가설·확인·기각·미확인 기록 양식 | 가설·근거 → 작업공간 기록 | 중복 조사 방지·상태 보존 확인 |
| M4-04 | 검증 후 보고 조건·양식 | 실제 근거 → 보고 초안 | 범위·재현·증거 누락 시 확인된 결과로 표시하지 않음 |

Cloud·AI 자료를 제공한다고 해당 분야의 실행 capability를 새로 승인하는 것은 아니다. 현재 웹/API 중심 범위에서는 해당 router부터 적용하고 나머지는 선택 가능한 참고 자료로 둔다.

### 14.6 Rifteo Security Skills — 자료 제공 담당자 A, 사용 구현 Agent 팀

아래는 원본 자료에서 확인한 Skill 이름 및 조사 분야다. Skill 설치만으로 우리 Agent가 로드하거나 MCP를 실행하는 것은 아니다. [원본 Skill 목록](https://github.com/Rifteo/skills)

연결 방식: 필요한 자료만 선별·검토·버전 고정하고, Agent가 사용하는 행동·도구 계약에 대응시킨다.

| 작업 ID | 제공할 기능·자료 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| RF-01 | `attack-surface` | 승인된 관측 → 조사 체크리스트 | 실행 가능한 도구와 자료 설명을 구분 |
| RF-02 | `check-exploit`·`vuln-diagnose` | 가설·공개 근거 → 검증 조건·미확인 목록 | PoC 존재만으로 성공·취약 확정하지 않음 |
| RF-03 | IDOR (`idor-hunter`) | 합성 계정·객체 관측 → 권한 비교 절차 | 승인된 다계정 시험 전제·증거 양식 확인 |
| RF-04 | SSRF (`ssrf-hunter`) | 관측·승인 범위 → 조사 절차 | 외부 callback·내부 접근은 별도 권한 처리 |
| RF-05 | SSTI (`ssti-hunter`) | 관측 → 조사 절차·근거 양식 | 자료 읽기와 payload 실행 권한 분리 |
| RF-06 | 보고서 Skill | 근거·미확인 조건 → 출처 포함 보고 초안 | 관측·가설·영향·수정 제안의 구분 확인 |

RF-06은 `bugbounty-reporter`, `pentest-report` 등의 후보에서 실제 채택 자료를 정한다. 공격 절차를 그대로 전부 실행하도록 설정하지 않으며, 기존 Control 승인과 사용 가능한 MCP 범위가 우선한다.

### 14.7 Burp MCP — 담당자 C, 정책 협의 Control 팀

원본 코드에서 HTTP 전송, Repeater 탭 생성, Intruder로 전달, WebSocket history, Professional 조건의 Collaborator 도구를 확인했다. 탭 전달은 공격 시작·응답 수집 완료가 아니다. [원본 Tool 구현](https://github.com/PortSwigger/mcp-server/blob/main/src/main/kotlin/net/portswigger/mcp/tools/Tools.kt)

연결 방식: 기존 로컬 Burp MCP와 stdio proxy를 재사용하되, 관찰 프로필과 요청·검증 프로필을 분리한다.

| 작업 ID | 개발·검증할 기능 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| BURP-01 | Proxy HTTP history 및 검색 | count·offset·필터 → 흐름·참조 | 기존 4개 도구 로그와 별개로 HTTP 조회 호출·마스킹 검증 |
| BURP-02 | 승인된 HTTP 요청·응답 비교 | 승인 대상·검토된 요청 → 응답·증거 참조 | 대상·Host·경로·세션·크기 통제, 범위 밖 거절 |
| BURP-03 | Repeater 탭 전달 | 승인된 요청 → 탭 전달 결과 | 전달 성공과 실제 전송·응답 상태 구분 |
| BURP-04 | Intruder 탭 전달 | 승인된 요청 → 전달 결과 | 대량 실행과 구분, 공격 자동 시작 여부 확인 |
| BURP-05 | WebSocket history 및 검색 | offset·필터 → 메시지·참조 | 마스킹·페이지네이션·접근 거절 확인 |
| BURP-06 | Collaborator 생성·interaction 조회 | 승인된 OOB 작업 → ID·관측 참조 | 에디션·callback 도메인·외부 통신 승인·작업 연계 검증 |

BURP-01/05는 과거 로그에서 discovery가 확인됐으나 실제 데이터 조회와 Agent 선택 완료는 별도 검증이다. BURP-02/03/04/06은 현재 연결 완료로 표시하지 않는다. WebSocket history 지원을 메시지 송신 지원으로 확대 해석하지 않는다.

Intruder의 payload 설정·공격 시작·진행 조회가 실제 Tool로 제공되는지는 별도 확인 대상으로 남긴다. 자동 대량 fuzzing은 이번 구현 범위가 아니다. Collaborator는 외부 서비스 사용과 민감정보 전송 가능성을 Control 팀과 합의한다.

### 14.8 MobSF MCP — 담당자 B, 모바일 팀 협의

래퍼 후보는 APK의 정적·동적 분석과 보고서 조회를 설명한다. 본체의 분석 결과를 우리 ToolResult에 정규화할 때 항목별 근거를 보존한다. [래퍼 기능 목록](https://github.com/plur1bu5/mobsf-mcp), [MobSF 본체](https://github.com/MobSF/Mobile-Security-Framework-MobSF)

연결 방식: 격리된 MobSF 서비스 + 검토된 MCP 래퍼 또는 자체 REST API Adapter. 정적 분석부터 시작한다.

| 작업 ID | 개발·검증할 기능 | 입력 → 출력 제안 | 완료 기준 |
|---|---|---|---|
| MOB-01 | 승인 APK 제출·정적 분석 시작 | 작업공간 artifact·hash → scan 참조·상태 | 임의 호스트 경로·비인가 파일 거절, timeout 처리 |
| MOB-02 | manifest 분석 | scan ID → 구성·component·근거 위치 | 실제 보고서 값과 대응, 누락 표시 |
| MOB-03 | permission 분석 | scan ID → 권한·맥락·근거 | 권한 존재만으로 취약점 확정하지 않음 |
| MOB-04 | secret 분석 | scan ID → 마스킹된 후보·위치 | 원문 비밀정보 대신 접근 제한 증거 참조 |
| MOB-05 | network 설정 분석 | scan ID → endpoint·설정·근거 | 설정 관측과 실제 통신 검증 구분 |
| MOB-06 | 보고서·분석 상태 조회 | scan ID → 상태·구조화된 요약·참조 | 다른 실행의 scan 접근 차단·큰 결과 처리 |
| MOB-07 | 준비된 모바일 환경의 동적 분석 | 승인 기기·APK → 상태·로그·네트워크 관측 | 호환성·시작·중단·정리·범위 통제 검증 |

위 분석 항목은 여러 별도 원본 Tool이 아니라 하나의 보고서 조회 결과를 section별로 처리하는 방식일 수 있다. 최종 manifest는 실제 원본 schema를 따른다. Frida·임의 ADB 명령·자동 우회·분석 삭제는 기본 프로필에서 제외한다.

## 15. 이미지 요구사항 추적 및 인수 기준

### 15.1 이미지와 작업 ID의 대응

| 이미지의 요구사항 | 상세 작업 ID | 주 담당 |
|---|---|---|
| NVD·OSV·GHSA, CVE·URL·PURL·CPE, package·SBOM bulk, VDB Skill | VDB-01~07 | A |
| CVE→ExploitDB·Metasploit·GitHub PoC, source·재현 환경·패치 자료 | EIP-01~05 | C, 실행 경계 B |
| 코드 검색, repository·commit·PR·Security Advisory, diff·upstream | GH-01~05 | C |
| Web/API/Mobile/Cloud/AI, 관측→가설→검증 방법 | M4-01~04 | A, Agent 팀 |
| check-exploit·attack-surface·vuln-diagnose, IDOR·SSRF·SSTI, 보고 | RF-01~06 | A, Agent 팀 |
| Proxy history·HTTP·Repeater·Intruder·WebSocket·Collaborator | BURP-01~06 | C, Control 팀 |
| APK 정적·동적, manifest·permission·secret·network | MOB-01~07 | B, 모바일 팀 |

모든 이미지 항목을 계획 대상으로 기록했다. 원본 미지원·환경 미준비 항목은 이유와 대안을 남기며, 기록됐다는 이유만으로 구현 완료 처리하지 않는다.

### 15.2 각 작업의 결과 기록 양식

각 담당자는 다음 정보를 PR 또는 검증 문서에 남긴다.

```text
작업 ID:
담당자:
우선순위:
기준 브랜치/commit:
원본 프로젝트/고정 버전/출처:
원본 지원 상태 및 제약:
연결 방식 및 필요한 환경:
실제 remote Tool명 또는 자료 경로:
우리 ToolId / capability / operation / target_types:
입력 schema / 출력 schema:
권한 및 네트워크 경계:
단위 테스트 / discovery / 실제 호출 근거:
Agent 후보 노출 / 선택·승인·실행 근거:
미구현·미검증 항목과 다음 단계:
```

Skill에는 ToolId 대신 자료 ID·버전·로드 경로를 기록한다. 메타데이터 또는 fixture 테스트만으로 live 호출과 Agent 사용을 완료 처리하지 않는다.

### 15.3 추가 인수 체크리스트

- [ ] 이미지의 모든 요구사항에 작업 ID와 담당자가 연결됨
- [ ] 실제 원본 지원·우리 연결·Agent 사용 상태를 각각 기록함
- [ ] VDB 각 식별자 및 bulk 입력의 성공·실패·coverage를 검증함
- [ ] EIP의 자료 조회와 환경·PoC 실행을 구분함
- [ ] GitHub 코드 검색·Advisory·diff·upstream 자료를 검증함
- [ ] m4vic 분야별 선택 및 가설 기록 경로를 Agent 팀과 검증함
- [ ] Rifteo 지정 Skill 및 보고 자료의 로드 경로를 검증함
- [ ] Burp 기능별 관찰·탭 전달·요청 실행·OOB 권한을 구분함
- [ ] MobSF 분석 항목별 출력·마스킹·scan 접근 범위를 검증함
- [ ] 승인된 합성 시험 환경에서만 실제 대상 검증을 수행함

## 16. 확장 우선순위 및 3인 협업 순서

기본 도구와 취약점 지식 도구는 둘 다 필요하다. 7개 프로젝트의 모든 기능을 동시에 구현하는 것을 기본 완료 조건으로 삼지 않는다.

| 단계 | A | B | C | 다음 단계로 넘어갈 조건 |
|---|---|---|---|---|
| 기반 | 파일 도구·Registry 계약 | 격리 명령 실행 | 웹검색·기존 브라우저 확인 | 기본 통합·정책 거절 테스트 |
| 지식·관찰 | VDB 단건·상태, 방법론 자료 인덱스 | 결과 파일·실행 권한 경계 | EIP·GitHub 읽기 전용, Burp history | 출처·schema·실제 조회 확인 |
| 조사 확장 | VDB bulk·SBOM, 방법론 상세 자료 | 승인 APK의 MobSF 정적 분석 | GitHub diff·upstream, 승인 HTTP·Repeater 전달 | 입력 제공·각 팀 계약·합성 환경 검증 |
| 환경 의존 | 확장 결과 계약·통합 | MobSF 동적 분석 | Intruder 지원 검토·Collaborator 승인 | 기기·에디션·외부통신·권한 준비 |

담당자 A의 방법론 책임은 자료 제공·버전 관리·계약까지다. Agent의 추론·분야 선택·Skill 로드는 Agent 팀 책임이며, 이를 Capability 팀 작업 완료로 대신하지 않는다. 담당자 C는 확장 항목이 많으므로 같은 단계 안에서도 조회 기능부터 순차 진행한다. 공통 파일은 A가 통합 관리하고 다른 담당자는 자기 모듈·테스트 중심으로 작업한다.

## 17. A 역할 구현 상태 — 2026-10-01

최신 dev `8979cac`를 capability에 병합하고 A의 기초 파일 도구를 구현했다. `analysis.list_directory/read_file/search_files/write_file/apply_patch`를 실제 stdio MCP, Registry, Gateway에 연결했다. 읽기/쓰기 프로필을 분리하고 승인된 Asset·engagement·run, 안전한 상대 경로, 텍스트·검색·quota 제한, SHA-256 기반 수정 경계를 검증했다.

신규 파일 도구 테스트 59개 및 실제 discovery/schema·주어진 CandidateCompiler 후보·Control 승인 후 실행이 확인됐다. operation 5개는 기존 namespace 안에서 공통 명명 목록에 호환 확장했으며 dev 통합 전 PM/Agent/Control 검토가 필요하다. 기본 Agent가 이를 자율적으로 계획·선택하도록 Provider/스케줄을 바꾼 것은 아니다.

AppThreat VDB·m4vic/Rifteo 자료 검토·Agent Skill loader, B/C 도구 및 실제 금융 시험환경 E2E는 완료 처리하지 않는다. 기존 전체 checklist는 이번 구현만으로 일괄 체크하지 않는다.

- [사전 검수 및 protocol](basic-computer-tools-a-protocol.md)
- [구현 근거·테스트·남은 작업 순서](basic-computer-tools-a-result.md)
- [작업공간 MCP 실행·팀 연결 안내](../../deploy/mcp/workspace/README.md)
