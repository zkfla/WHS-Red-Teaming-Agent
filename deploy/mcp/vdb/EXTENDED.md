# VDB 확장 프로필 — B/C와 독립적으로 제공하는 지식 조회

기존 `vdb_readonly`의 3개 도구/설정은 그대로 쓸 수 있다. 확장은 명시적으로
`vdb_readonly_extended`를 선택해야 한다. 새 상용 서버/API key가 필요한 기능은
아니며, 기존 별도 VDB runtime에서 우리 stdio MCP bridge를 실행한다.

## 도구와 입력

기본 `vdb_status`, `search_by_cve`, `search_by_purl_like`를 포함한 총 7개다.
추가 4개는 모두 `analysis.static_analysis`, Asset 대상, 최소 medium risk다.
아래 모든 호출은 `asset_ref` 필수이고 engagement/run은 신뢰된 Gateway가 주입한다.

| ToolId | 추가 필수 입력 | 선택 입력 | 하는 일 |
|---|---|---|---|
| analysis.search_by_ghsa | ghsa_id | limit, sources | 정확한 GHSA ID/별칭 조회 |
| analysis.search_by_cpe_like | cpe | limit, sources | 관측된 vendor/product/version의 CPE 조회 |
| analysis.search_packages | purls | limit, sources | 버전 PURL 목록의 항목별 조회 |
| analysis.search_sbom | sbom_ref | offset, max_items, limit, sources | 승인 CycloneDX JSON 구성요소 조회 |

- `sources`: 선택적 출처 이름 배열(최대 5개, 중복 금지). 원본의 출처 정규화
  로직을 사용하며 원본에 없는 출처/필드는 추정하지 않는다. 반환 `requested_sources`
  및 match.source와 snapshot.sources를 함께 확인한다. 미지원 SQL·임의 filter key는 거절.
- GHSA는 `GHSA-2345-6789-cfgh` 형태의 정확한 ID. 원본 substring alias 결과도
  다시 정확히 대조한다. 유사 ID가 걸리면 오류이지 취약점 후보/안전 판정이 아니다.
- CPE는 `cpe:2.3:a:vendor:product:1.2.3:*:*:*:*:*:*:*` 형태의 **제한된 CPE 2.3**.
  part는 a/o/h, vendor/product/version은 문자·숫자·`_.+-`만 받으며 버전 `*`/`-`는 거절.
  나머지 7개 필드는 `*`만 지원한다. escape/locale/edition별 정교한 CPE는 미지원.
  원본은 vendor/product/version 중심으로 후보를 찾으므로 결과는 제품 적용 가능성
  참고자료이며 정확한 대상/edition/환경에 대한 취약점 확인이 아니다.
- PURL은 각 항목에 버전 필수. 기본 목록 1–25개, 운영자 상한 최대 50개.
  패키지명만 보고 버전, URL만 보고 라이브러리, 웹사이트만 보고 SBOM을 생성하지 않는다.

## 승인 SBOM 설정

`extended-policy.example.json`은 SBOM을 등록하지 않은 안전한 기본 설정이다.
이 경우 나머지 도구는 쓸 수 있지만 search_sbom은 승인된 참조가 없어 거절한다.

운영자가 해당 engagement/run에서 사용하도록 승인한 SBOM만 별도 private 디렉터리
(0700)에 넣고, 실제 파일의 SHA-256을 확인한 뒤 아래처럼 정책에 등록한다.
SBOM root는 DB root와 서로 포함 관계가 없어야 한다. 파일은 UTF-8 JSON·일반 파일·
단일 hard link여야 하며 다른 사용자/그룹 쓰기를 허용하지 않는다.

```json
{
  "sbom_root": "/absolute/operator-owned/approved-sboms",
  "sboms": [
    {"ref": "approved-app-bom", "path": "app.cdx.json", "sha256": "실제_64자리_소문자_SHA256"}
  ]
}
```

위 조각은 기존 확장 정책에 더하는 필드이며 그대로 실행할 완성 정책이 아니다.
Agent는 `sbom_ref=approved-app-bom`만 보낸다. 승인 참조는 discovery의 enum으로
전달되고 운영 파일 경로는 전달하지 않는다. 임의 path/URL, symlink/hardlink,
파일/디렉터리 교체, SHA-256 불일치는 차단한다. 이 정책에 등록한 SBOM은 해당
VDB Asset 조회 권한에 부속된 입력이다. 별도 Asset ROE가 필요한 조직은 Control 팀과
다중 Asset 판정 계약을 먼저 합의해야 하며 자동 Scope 추가는 하지 않는다.

지원 형식: CycloneDX JSON 1.4/1.5/1.6, components 배열 필수.
중첩 components와 metadata.component도 포함한다. 기본 최대 64,000바이트·200개
구성요소(운영자 상한 500개). XML·SPDX·archive·외부 BOM 링크는 지원하지 않는다.
schema 전체 적합성 검사기는 아니며 조회에 필요한 JSON 구조/입력 경계를 검사한다.
거대한 BOM/미지원 형식은 조회 범위가 자동 늘어나는 것이 아니라 오류다.

## 결과를 읽는 법

- 단건: match_basis, advisory_id, affected_versions, description, 출처·참조·별칭,
  severity 및 날짜를 원본에서 제공할 때만 반환. 긴 텍스트는 text_truncated 표시.
  fix_version은 범위 경계에서 추측하지 않으며 이번에는 추출하지 않는다.
- `database.metadata_coverage`: full/partial/missing/unknown. index row별 metadata와
  text row 연결 및 출처 필드 존재 여부다. 원천 공급자 alias/description의 완전성이나
  모든 제품·모든 취약점의 coverage 보증이 아니다.
- GHSA/source filter는 metadata_coverage=full 필요. 비어 있거나 부분 metadata는
  `vdb_metadata_coverage_gap`으로 거절. metadata 없는 기본 DB의 index PURL 조회는 유지.
- 묶음: 각 item에 index, query, state=checked/error/not_checked, error_code, lookup.
  잘못된 버전 PURL은 해당 항목 오류. DB 미준비/시간 초과/관측 PURL 없음은 NOT CHECKED.
  중복 입력도 위치별 결과를 유지하며 실패 항목을 조용히 삭제하지 않는다.
- SBOM은 offset/max_items로 페이지 조회. total_items와 checked/error/not_checked는
  **전체 BOM 대비 이번 호출의 조회 상태**다. 이전 페이지의 성공을 서버가 기억/합산하지
  않으며 Agent가 동일한 sbom_sha256 기준으로 페이지를 합쳐야 한다. next_offset을
  확인하고 전체 파일 조회 완료로 착각하지 않는다. 버전/PURL 없는 component는 미조회.
- truncated는 페이지 제한·개별 검색 상한·출력 제한을 표시한다. 항목 상태는 유지하고
  증거 row가 잘리면 해당 lookup.truncated도 표시한다. 텍스트 잘림은 text_truncated 별도.
- 한 호출은 협력적 SQLite query/묶음 시간 제한, JSON 출력 200,000바이트 이하.
  SQLite 진행 콜백은 OS CPU/메모리/네트워크 sandbox가 아니다. 원본 검색 API는 내부에서
  결과를 먼저 materialize할 수 있으므로 운영자 OS quota/읽기 전용 mount/egress deny 필요.
- 항상 knowledge_only / target_vulnerability_confirmed=false. 결과 없음은 이 snapshot·
  입력·필터에서 일치 자료 없음이지 안전, 실제 금융 제품 식별, 규제 준수 판정이 아니다.

## 실행과 검증

1. 기존 VDB 7.0.1 고정 runtime을 사용한다. 기본 프로젝트 .venv는 변경하지 않는다.
2. 확장 정책의 DB 경로·Asset·engagement/run을 실제 승인 값으로 준비한다.
3. `extended.env.example`의 COMMAND/ARGS_JSON 경로를 운영자 파일에서 수정한다.
4. 해당 파일을 `set -a; source /absolute/operator.env; set +a`로 로드한다.
5. discovery는 `REDTEAM_EXTERNAL_MCP_SERVERS=vdb-extended`로 실행한다.
   실제 App은 config의 capability.mcp_servers에 vdb-extended를 명시한다.
   기존 다른 서버 목록을 보존하되 vdb-readonly와 vdb-extended는 같은 ToolId가 중복되므로
   둘 중 하나만 등록한다. CLI의 임의 새 Run에 예제 ID를 자동 재사용하지 않는다.
6. Agent는 Registry metadata → CandidateCompiler → Control → Gateway → MCP로 호출한다.
   확장 프로필은 discovery/schema/operation/output drift 시 연결을 거절한다.

```sh
export REDTEAM_VDB_TEST_PYTHON='/absolute/runtime/vdb/venv/bin/python'
./.venv/bin/pytest -q tests/unit/test_vdb_readonly.py tests/unit/test_vdb_extended.py tests/integration/test_vdb_readonly_mcp.py tests/integration/test_vdb_extended_mcp.py
```

검증은 작은 합성 DB와 승인 합성 BOM을 사용한다. 실제 원본 Python 라이브러리의
검색을 실행하지만 실제 금융 대상/실제 모델 검증은 아니다. 선택 환경변수가 없으면
원본 backend 테스트는 skip이며 연결 완료로 계산하지 않는다.

## 실제 DB 준비·갱신은 운영자 작업

- 내부 검수 mirror/빌드가 있으면 우선 사용한다. upstream 공급 artifact를 사용할 경우
  고정 출처·버전·checksum·라이선스·자원 요구량을 운영자가 확인한다.
- 기본 DB는 metadata가 비어 있을 수 있다. alias/source 조회가 필요하면 원본의
  `app-extended`/`app-10y-extended` flavor 또는 --include-metadata 빌드를 준비한다.
- 원본 고정 버전 안내의 `vdb db refresh full --flavor app-extended`는 **운영자가 별도
  staging VDB_HOME에서 실행하는 다운로드 절차**다. 이번 작업에서는 실행하지 않았다.
  원본 refresh/빌드/네트워크 권한은 Agent/MCP tool allowlist에 제공하지 않는다.
- staging에서 schema/rows/metadata/freshness/출처를 검증하고 변경 전후 checksum과
  빌드 자료를 보관한다. 알려진 조회 fixture를 확인한 뒤 조회 서버를 종료하고 snapshot을
  교체한다. 이전 snapshot을 보관해 rollback 가능하게 하며 열린 DB를 in-place 갱신하지 않는다.
- completeness/full·created_utc를 임의로 고치거나 빈 alias 자료를 자동 backfill하지 않는다.
  full은 artifact 범위의 선언이고 최신 날짜도 원천 advisory의 최신성 보증이 아니다.

아직 별도: 일반 URL/OSV alias/fulltext 검색, shard fan-out, fix/patch 근거 추출,
대규모 실 DB 성능 검증, 실제 외부 LLM 활용, B/C 통합, dev 반영.
