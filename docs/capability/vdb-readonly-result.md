# A 후속 작업 — VDB 읽기 전용 구현 결과

- 작업 ID: `CAP-A-VDB-20261001`
- 브랜치: capability. dev 재병합/원격 push/PR는 하지 않았다.
- protocol commit: `5f027c5`; 파일 도구 기반: `60502e3`; 병합된 dev 기준: `8979cac`.
- 구현 commit: `6ff1aac`. 기본/격리 MCP SDK 모두 2.2.0, 격리 VDB 7.0.1로 검증했다.
- 완료 범위: **원본 검색 라이브러리 + 작은 합성 DB의 실제 stdio 조회 및 팀 간 호출 경계**. 전체 A 역할·실제 금융권 취약점 검증·LLM 자율 사용 완료가 아니다.

## 사전 검수와 작업 순서

1. 기존 A 역할 문서, 브랜치·dirty state 및 Registry/Adapter/Gateway 계약을 읽었다. 사용자 변경 파일을 보존했다.
2. AppThreat VDB 원본 README/라이선스/config/db/search/server/test를 검수했다. 고정 source SHA `2dc040a44986da0c4eac1366ba77633f09e593b6`, package 7.0.1, MIT다.
3. 위험 경로를 확인했다. 원본 server lifespan은 DB 필요 시 download/refresh를 수행한다. `db.get(read_only=True)`도 DB 파일이 없는 경우 CREATE 모드로 fallback할 수 있다. 조회 metadata backfill도 RW 전환 경로가 있어 그대로 Agent에 제공하지 않았다.
4. 기본 `.venv`를 변경하지 않고 `runtime/vdb/venv`에 별도 검증 환경을 만들었다. 검수한 원본 checkout에서 backend를 설치했다. 원본 소스는 수정·vendoring하지 않았다. 대량 DB는 다운로드하지 않았다.
5. 우리 `vdb_readonly` MCP server를 구현했다. 원본 검색 로직에 READONLY + NOFOLLOW + immutable SQLite 핸들을 직접 바인딩하고 query_only와 in-memory temp를 설정했다. 자동 fetch와 metadata backfill을 끄고 원본 server lifespan은 호출하지 않는다. 프로세스별 한 DB root 및 serial lookup 경계다.
6. asset/engagement/run 정책을 고정했다. 공통 scoped Analysis Adapter는 신뢰된 ExecutionContext에서 실행 ID를 주입한다. workspace는 기존 오류 코드와 동작을 유지한다.
7. VDB 전용 profile에 정확한 allowlist/operation binding, 최소 medium risk, 입력과 출력 manifest 검증을 추가했다. 기존 namespace/action key/Target 및 transport 공통 계약은 확장하지 않았다.
8. 원본 MCP server의 실제 discovery를 별도의 신선한 합성 DB로 검수했다. `search_by_cve(cve_id)`, `search_by_purl_like(purl)`, 공통 `max_results`, `db_refresh` 존재 및 원본 output schema 미제공을 확인했다. Agent에는 원본 전체 서버 대신 우리 bridge의 명시적 output schema와 `asset_ref/limit/context` 입력을 제공한다.
9. 가짜 advisory 3건으로 원본 라이브러리 조회·버전 비교, CandidateCompiler 후보·Control ALLOW/DENY·실제 Gateway 실행을 검증했다. DB SHA-256 전후 비교 및 CREATE TABLE 거절로 DB가 바뀌지 않음을 확인했다.
10. 결측·빈 DB·partial coverage·stale/unknown 날짜·손상·metadata 건수 불일치·WAL 잔여·링크·다른 context·필수/추가 입력·manifest drift 거절과 낮은 상한의 기본값을 테스트했다. 실행 예제와 인수인계 문서를 작성했다.

## 실제 가능한 범위

쉽게 말하면 **Agent에게 취약점 백과사전의 읽기 버튼을 준 것**이다. 새 책을 다운로드하거나 내용을 바꾸는 버튼, 공격을 실행하는 버튼은 주지 않았다.

| ToolId | 역할 | 확인한 실제 호출 |
|---|---|---|
| analysis.vdb_status | DB 준비 여부·건수·완전성 선언·날짜 읽기 | ready/missing/empty/invalid/coverage_gap/unknown_metadata 등 |
| analysis.search_by_cve | 정확한 CVE 한 건의 로컬 지식 조회 | 합성 CVE의 advisory·영향 버전·출처·심각도·참조 반환 |
| analysis.search_by_purl_like | 실제 버전이 있는 PURL 한 건 조회 | 1.5.0은 합성 3건 매칭, 2.0.0은 snapshot 내 알려진 매칭 없음 |

자료에 없는 제품 버전·필드·출처를 추측하지 않는다. DB의 full 선언은 해당 artifact 범위의 metadata이며 전 세계 모든 취약점이나 금융 제품 정보를 보증하지 않는다. 모든 조회는 `knowledge_only`, `target_vulnerability_confirmed=false`다. version match는 대상에서의 취약점 재현이 아니다. 실제 금융 사이트 요청은 수행하지 않았다.

요구 source SHA/version과 관측 package version을 구분한다. runtime은 버전만 확인하고 설치된 source commit의 암호학적 provenance는 확인하지 않으므로 `source_revision_verified=false`를 반환한다. pin 설치와 별도 원본 checkout SHA 검수는 배포자 책임이다.

## 테스트 근거

원본 검증 환경 변수는 해당 테스트 실행에만 지정했다. 기본 `.venv`에는 VDB package가 없음을 별도로 확인했다. opt-in 외부 도구 tests의 skip은 해당 도구의 연결 성공을 뜻하지 않는다.

- VDB 신규 tests + 기존 workspace tests: **93 passed**, 26.86초. 그중 VDB 신규 34개, workspace 59개.
- 합성 실행 예제: 상태·CVE·PURL 3개 모두 `control=allow`, `success=true`. Match count는 0/1/3이며 실제 대상 취약점 확정은 모두 false.
- 변경 Python 파일 ruff 및 `git diff --check`: 통과.
- 전체 현재 worktree 회귀: **1,688 passed / 7 skipped**, 153.90초. opt-in VDB 원본 backend/discovery 검증 포함이다. 7개 skip은 MobileNext, apktool, jadx, 외부 Web MCP 및 실제 ADB 조건 미설정이다.

현재 전체 테스트에는 보존한 사용자 미커밋 `test_analysis_mcp.py`도 포함된다. clean commit 스냅샷 전체 검증과 동일하다고 표현하지 않는다. 실제 OS 격리/egress deny나 대량 운영 DB 성능 테스트는 이번 결과에 포함하지 않는다.

## Agent 팀 인수인계 및 다음 작업

1. **Agent 팀**: scope에 승인된 지식 DB Asset를 제공하고 확정된 run/engagement로 policy를 provisioning한 뒤 gateway를 연다. catalog의 ToolId/입출력 schema/target type/operation을 사용한다. 구체적인 조회 후보가 주어졌을 때의 compiler와 실행 경로는 확인됐지만 기본 Provider가 자율 선택하는 부분은 별도다.
2. **Agent 팀**: 처음 `vdb_status`를 호출해 데이터 상태를 확인한다. 관측된 PURL 버전·CVE를 전달하고 stale/unknown/coverage/lookup failure를 “안전”으로 처리하지 않는다. 조회 근거를 Observation/보고서에 연결하며 Finding 확정은 승인된 별도 재현 결과로 판단한다.
3. **A 다음 단계**: m4vic·Rifteo·VDB 방법론 자료를 고정 source/version/hash의 읽기 전용 인덱스로 검수한다. 외부 지침은 자료이며 ROE·승인 권한을 변경하지 않는다. Agent의 Skill loader와 자동 선택은 Agent 팀과 통합한다.
4. **A 확장 단계**: GHSA/alias/CPE/URL 등 실제 지원 입력, patch/fix 근거, shard fan-out, bulk/SBOM. 승인 asset 참조·크기/시간·부분 실패·coverage를 먼저 정의한다. DB 생성/갱신은 계속 별도 운영 권한으로 둔다.
5. **공통**: B의 격리 shell/MobSF와 C의 검색/EIP/GitHub/Burp 결과 참조를 통합하고 실제 LLM의 도구 선택·승인 거절·오류 전파를 합성 E2E에서 검증한다.

- [실행 설정 및 안전 경계](../../deploy/mcp/vdb/README.md)
- [세 사람의 공통 계획과 현재 상태](basic-computer-tools-team-plan.md)

## 후속 상태 — 방법론 자료 제공

위 다음 작업 3번의 자료 제공 단계를 `a2e1a16`에서 진행했다. VDB 안내를 포함한 m4vic/Rifteo 문서 19개를 검수된 읽기 전용 bundle과 도메인 인덱스로 제공했다. Agent loader/자율 선택 및 VDB bulk 확장은 아직 별도다. 최신 검증과 통합 제한은 [방법론 후속 결과](methodology-reference-result.md)를 따른다.
