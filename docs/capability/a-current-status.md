# A 담당 최신 상태 — 2026-10-02

기준: capability `2fc9613` + 이번 로컬 변경. 예전 결과 문서는 각 구현 시점의 기록이다.
이 표와 후속 기록으로 현재 상태를 구분한다. 실제 LLM/운영 DB/금융 대상 검증 완료는 아니다.
사용자는 이번 후속에서 **로컬 검증·운영 준비까지만 진행**하도록 범위를 정했다.

## 담당 범위와 현재 가능한 것

| A 범위 | 현재 제공 | 남은 조건 |
|---|---|---|
| 파일 도구 | analysis 목록/읽기/검색/쓰기/부분 수정 5개, 실행별 Asset/root/context, 변경 SHA-256 및 승인 경계 | Mac/Linux POSIX, 기본 Agent의 실제 LLM 범용 파일 업무는 별도 |
| 공통 연결 | 실제 schema/필수·선택 입력/operation/target/risk metadata, Registry/Gateway 및 프로필 검수 | 모든 외부 도구의 환경/Agent 사용 일괄 완료 아님 |
| VDB 지식 | 상태/CVE/PURL/GHSA/CPE/package bulk/승인 CycloneDX 7개, 원본 7.0.1 합성 DB 검증 | 운영 DB provenance/준비/부하/교체·복구 미검증 |
| 방법론 제공 | 고정 원본 문서 19개와 보호 경로, 운영자 선택 최대 3개 MCP paging/본문 hash/LLM 데이터 전달 | 실제 모델의 선택 품질·주입 저항성·interactive wizard 별도 |
| 승인 설정 동기화 | DeploymentSpec → 정책/Engagement/stdio 실행 설정, 해시·경로·세션 바인딩 | web-http/recon-passive만; 기본 CLI wizard와 모든 원격 서버 자동 적용 아님 |
| 운영 DB 사전 점검 | 별도 운영자 명령: 예상 hash, 상태/metadata/freshness, 긍정·부정 조회, 시간/크기, 조회 전후 보존 | 다운로드/활성화/갱신/rollback 도구 아님 |

### 초기 상세 계획과 대응

- VDB-01/02: 출처 필터 및 CVE/GHSA 조회 구현. metadata가 부분/누락이면 정상 빈 결과로
  숨기지 않는다. 실제 corpus 출처 coverage는 아직 운영 검증하지 않았다.
- VDB-03: 버전 PURL, 구체 CPE, 버전 패키지 입력 구현. 일반 URL/OSV alias/fulltext는 별도다.
- VDB-04/05: 제한된 package bulk와 승인 파일 참조의 CycloneDX JSON 구현. 전체 구성요소를
  자동 발견하지 않으며 큰 SBOM/다른 형식/shard fan-out은 별도다.
- VDB-06: 상태와 운영 절차 및 사전 점검 명령 준비. 실제 DB 준비·갱신·복구 실증은 안 했다.
- VDB-07/M4/RF: 선별한 사용 가이드·조사/보고 자료 제공 및 조회/참고 데이터 전달.
  상세 링크/스크립트 실행/PoC 실행/자율 Skill 설치는 하지 않는다.

## 이번 순서대로 수행한 검수

1. 로컬 capability를 원격까지 fast-forward했다. 기존 사용자 미커밋 변경 5개는 보존했다.
   dev는 조회만 했다. 기본 Analysis Agent가 schedulable=true가 된 최신 모바일 통합을
   옛 방법론 테스트가 거절하던 두 실패를 수정했다. 현재 기본 Agent의 모든 정의와
   capabilities를 보존하는 검사로 바꾸고 Analysis Agent를 비활성화하지 않았다.
2. 현재 프로세스의 LLM 관련 환경변수 이름/존재만 확인했다. 키 없음, LiteLLM 없음.
   운영 DB는 제공되지 않았다. 별도 설치·외부 모델 호출·대량 feed 다운로드를 하지 않았다.
   기존 격리 runtime에서 사용할 [오프라인 VDB 사전 점검](../../deploy/mcp/vdb/SNAPSHOT-AUDIT.md)을
   구현하고 원본 검색 라이브러리의 합성 DB로 검증했다.
3. 실제 Workspace MCP와 SQLite checkpoint/work/task/path/graph/operator-intent 및 파일
   Evidence/Approval 저장소를 사용했다. 256자 첫 페이지를 승인 후 읽고 두 번째 승인을
   기다릴 때 첫 MCP/Control/LLM/runtime 객체를 닫았다. 새로 구성한 객체에서 디스크
   상태로 복구했다. 미승인은 실행 안 됨, 거절은 읽은 한 페이지만 보존/본문 전달 없음,
   각 페이지 승인 후에는 offset 중복 없이 검증된 본문을 테스트 모델에 전달함을 확인했다.
   별도 OS 프로세스 전체 종료/실제 모델 장애/모든 종료 상태의 복구 보장은 아니다.
4. 실제 한 Gateway에 파일 읽기/쓰기 5개와 확장 VDB 7개를 함께 등록했다. 12개 ToolId
   중복 없음, schema, action별 조회, Candidate/Control 호출 및 승인 필요 상태를 검증했다.
   두 Asset가 둘 다 허용돼 있어도 인자를 바꿔 서로 호출하는 것은 거절한다.
5. B/C 제출된 코드의 회귀를 전체 테스트에 포함했다. 이는 모든 외부 MCP가 현재 실행
   중이거나 실제 Agent로 선택 가능하다는 검증은 아니다. 아래 인수인계 조건을 유지한다.

## B/C·Agent 팀에 전달할 기준

| 상대 | A가 확인/제공할 것 | 상대가 준비할 것 |
|---|---|---|
| B: shell/MobSF | 고정 namespace/operation/schema/risk/허용 Asset와 실행 제한 검수, 결과 file reference | 제한된 실제 실행 구현·격리 환경·discovery·정상/거절 fixture |
| C: 검색/페이지/공개 지식 | Registry/설정/Adapter 변경 검수, 결과 구조와 외부 전송 경계 | 검색 공급자·MCP 구현, EIP/GitHub 읽기 프로필 및 실제 schema |
| Agent | core/ports catalog/gateway, 승인 설정/자료/VDB fixture | 실제 모델 기반 accepted path/action, 결과 해석, 잘림·오류·재개 처리 |
| PM/Control | ToolId와 capability.operation 분리, 위험도/Asset/ROE 검수 자료 | 신규 action key 확정과 승인/범위 규칙 공동 검토 |

현재 page_text는 MCP/Registry/Gateway 경로가 있으나 `page_read`는 canonical Agent 명명
목록에 없어 Agent/PM 연결이 남았다. 웹검색은 내부 기반만 있고 실제 공급자/MCP 등록이
없다. 일반 shell/MobSF/EIP/GitHub의 완료 증거도 현재 capability에서 확인되지 않았다.
제한된 ADB device_shell은 범용 컴퓨터 shell과 다른 도구다. A가 대신 구현하거나
계약/권한을 임의로 늘리지 않는다.

결과는 승인된 root의 `{asset_ref, path}`로 공유한다. 같은 ToolId의 별도 workspace-read
서버를 두 개 붙이지 않는다. 동일 read 서버 아래 방법론 보호 폴더를 사용한다.
호스트 절대 경로·인증키·개인정보를 Agent 결과/검색 공급자에게 전달하지 않는다.

## 앞으로 필요한 결정

1. 운영 DB 출처·보관 경로·용량 한도·검수 checksum/조회 정답 선택 후 실제 사전 점검.
2. 실제 모델/공급자·비용·승인된 공개 입력 확정 후 LLM 공동 E2E 평가.
3. B/C 실제 제출 프로필 통합·거절/오류/권한 회귀와 PM/Control 검토.
4. 전체 검증 뒤 capability push/PR/dev 반영은 사용자 별도 요청으로 진행.

검증 수치와 변경 파일은 [이번 후속 결과](a-integration-followup-result.md)를 따른다.
