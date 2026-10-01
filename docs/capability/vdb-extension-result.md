# A 단독 VDB 확장 결과 — 2026-10-01

작업 브랜치: capability, 기준 `08e261e`. B/C 변경을 기다리거나 대신 구현하지 않았다.
dev 변경·push·PR·원격 병합은 하지 않았다. 사용자 기존 수정/미추적 파일은 보존했다.

## 구현한 것

기존 `vdb_readonly` 3개 ToolId와 입력 설정은 유지하고 별도
`vdb_readonly_extended` 프로필에 4개를 추가했다. 등록되는 도구는 총 7개다.
입력/출력 schema·필수/선택 인자·operation·Asset 대상·최소 medium risk를 Registry에
제공하며 실제 MCP discovery와 대조한다. 기존 출력에는 metadata_coverage,
query_kind/requested_sources, description/match_basis/text_truncated가 추가됐다.
서버/클라이언트 코드는 같은 릴리스로 배포해야 하며 schema 불일치는 거절한다.

| 추가 도구 | 실제 확인 범위 | 제한 |
|---|---|---|
| analysis.search_by_ghsa | 정확한 GHSA alias → 합성 CVE 조회 | 완전한 metadata 필요, 유사 ID 거절 |
| analysis.search_by_cpe_like | 구체적 vendor/product/version, 영향/미일치 버전 비교 | 제한된 CPE 2.3, unknown/wildcard 버전 거절 |
| analysis.search_packages | 버전 PURL 목록의 항목별 조회·실패·미조회 | 기본 25개/최대 50개, 묶음 시간/출력 제한 |
| analysis.search_sbom | 승인된 CycloneDX JSON·digest·페이지 조회 | 운영자 참조만, 기본 64KB/200개 구성요소 |

추가 4개의 canonical action key는 기존 `analysis.static_analysis`를 재사용한다.
Agent는 ToolId/설명/필수 인자/schema를 보고 선택해야 한다. Analysis라는 같은
namespace 안에서도 APK와 VDB 도구의 대상/입력 조건이 같다고 가정하지 않는다.

### 안전·오해 방지

- 원본 AppThreat 검색 라이브러리 7.0.1을 사용한다. upstream MCP 전체를 노출하거나
  startup의 자동 다운로드/갱신을 Agent 실행 경로에 넣은 것은 아니다.
- metadata 없는 기본 DB를 GHSA/source filter 조회의 정상 빈 결과로 취급하지 않는다.
  index row별 metadata/text/source 존재를 확인하고 누락·부분이면 명시적으로 거절한다.
- 묶음 조회는 입력 위치를 유지하며 checked/error/not_checked와 전체 대비 건수를 제공한다.
  실패한 한 패키지 때문에 다른 항목 결과를 숨기지 않는다. 시간 초과 뒤 새 조회를 시작하지 않는다.
- DB context는 묶음 상단에 한 번만 반환한다. 항목별 증거 예산을 먼저 적용해
  큰 결과를 모은 뒤 반복적으로 버리는 비용을 줄인다. JSON/항목 상태는 자르지 않으며
  증거 상한은 해당 lookup.truncated로 표시한다.
- SBOM은 별도 private root의 승인 상대 파일 경로·SHA-256과 불투명 참조로 연결한다.
  임의 파일/URL, scope 추가, symlink/hardlink, 다른 run, 파일 교체·변조는 허용하지 않는다.
  페이지는 동일 digest로 Agent가 합산해야 하며 이전 페이지 조회를 서버가 기억하지 않는다.
- DB는 READONLY/immutable/query_only, 자동 fetch/backfill 금지. 조회 전후 합성 snapshot
  checksum이 동일했다. 결과는 knowledge_only / target_vulnerability_confirmed=false.
- 원본 version 비교/metadata 자료를 사용하며 없는 제품명·버전·fix_version은 추측하지 않는다.
  일반 reference/description은 참고 데이터이지 실행 지시나 권한이 아니다.

## 검증 근거

- 전체 회귀: **1967 passed, 10 skipped**, 82.54초.
- VDB 기존/확장 집중 검증: **83 passed**, 18.93초. 원본 backend 검증을 실제로
  켠 실행이며 skip을 통과로 계산하지 않았다.
- 원본 MCP discovery 별도 검수: **1 passed**, 1.06초. 검수 checkout HEAD가
  `2dc040a44986da0c4eac1366ba77633f09e593b6`임을 확인한 후 변경 없는 원본을 실행했다.
- Ruff 및 git diff 공백 검사 통과.
- 합성 DB 3건과 합성 CycloneDX를 사용한 실제 stdio discovery, 7개 schema 대조,
  4개 새 도구 각각 CandidateCompiler → 실제 Control → Gateway → 원본 검색 실행 검증.
- 정책 Asset 변경은 Control DENY, 실행 identity 변경은 Gateway/서버 거절.
- metadata 누락/부분/text 부재, 유사 GHSA, 출처 필터, 잘못된 CPE/PURL,
  SBOM 링크·변조·크기/구성요소 수·형식·페이지 경계, 시간 초과 및 출력 잘림 검증.
- 처음 전체 실행은 sandbox의 localhost bind 제한으로 기존 HTTP fixture 2개가
  막혔으며 네트워크 권한 승인 후 재실행해 통과했다. 실제 금융 사이트 접속은 없었다.
- 별도 기존 검증 interpreter 및 기존 고정 VDB runtime을 사용했다. 프로젝트 .venv나
  원본 checkout을 변경하거나 추가 feed/패키지를 다운로드하지 않았다.

10개 skip은 선택적 외부 MCP/원본/환경 테스트다. 그 기능까지 통과했다고 계산하지 않는다.
이는 실제 원본 라이브러리의 **작은 합성 데이터 검증**이지 대규모 취약점 feed,
실제 금융 대상 취약점, 실제 LLM 자율 계획/선택 품질을 검증한 것이 아니다.

## 아직 남은 A/VDB 범위

1. URL/OSV 등 추가 alias/fulltext 및 patch/fix 근거 조회를 원본 입력/출력 지원과
   데이터 크기/출처/버전 정확성 기준으로 추가 검수.
2. 실제 운영 DB 선택·외부 artifact provenance/checksum·갱신/rollback 수행 및
   대규모 데이터 성능 검증. 운영 절차는 문서화했으나 실제 다운로드/운영 전환은 안 했다.
3. shard coverage/fan-out 및 큰 SBOM/추가 형식은 이번 제한을 자동 완화하지 않고 별도 설계.
4. 실제 LLM 활용/중단·재개는 Agent 팀 공동 검증, B/C 결과 통합·PM/Control 검토·dev 반영 별도.

읽기 전용 확장 기능을 구현했다는 이유로 전체 A 계획/이미지 checklist를 일괄 완료 처리하지 않는다.

- [작업 전 검수 계획](vdb-extension-plan.md)
- [실행 설정·입력/출력·운영 DB 준비/갱신 안내](../../deploy/mcp/vdb/EXTENDED.md)
