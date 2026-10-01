# A 단독 진행 — VDB 조회 확장 검수 계획

기준: capability `08e261e`. B/C 구현과 무관하게 진행하며 dev 변경·push·PR은 하지 않는다.
기존 사용자 변경 파일은 보존한다. 검수 원본은 AppThreat VDB 7.0.1,
commit `2dc040a44986da0c4eac1366ba77633f09e593b6`의 실제 검색 코드다.

## 작업 순서

1. 원본 GHSA/alias·CPE·metadata·버전 비교 경로와 안전한 파일 읽기 구현 검수.
2. 기존 `vdb_readonly` 3개 도구는 유지. 별도 `vdb_readonly_extended` 프로필에
   GHSA, 정확한 버전 CPE, 버전 PURL 목록, 승인 CycloneDX JSON 조회를 추가.
3. metadata 조회 누락/부분 자료를 명시하며 source 필터는 필요한 metadata가
   완전한 경우에만 사용. 결과 없음은 안전·패치 완료·실제 취약점 확정이 아님.
4. SBOM은 운영자가 등록한 불투명 참조·상대 경로·SHA-256으로만 조회.
   임의 파일 경로/URL, 심볼릭·하드 링크, digest 불일치, 크기 초과는 차단.
5. 목록 길이·시간·출력 크기·SBOM 크기/구성요소 수를 제한. 항목별 실패,
   미조회, 잘림, 다음 페이지를 구조화하고 조용히 항목을 버리지 않음.
6. manifest와 실제 stdio discovery/schema, Candidate → Control → Gateway,
   원본 backend 합성 DB, 경계/부분 실패/기존 3개 프로필 회귀 검증.
7. 실행 설정·운영 DB 준비/갱신 절차·제한·실행한 테스트를 결과 문서로 기록.

## 이번에 하지 않는 것

- 대량 공개 DB 다운로드, shard 자동 fetch/fan-out, Agent DB 갱신.
- 금융기관 제품/버전 추측, reference 자동 방문, PoC·Exploit 실행.
- 실제 외부 LLM 선택 품질, B/C 결과 통합, dev 원격 반영.
- 임의 일반 URL을 제품 식별자로 해석하거나 근거 없는 fix_version 생성.
