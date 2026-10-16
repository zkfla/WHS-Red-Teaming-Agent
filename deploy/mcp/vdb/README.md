# AppThreat VDB 읽기 전용 연결

이것은 **원본 검색 라이브러리를 사용하는 우리 MCP bridge**다. upstream `mcp-server-vdb` 전체를 등록한 것이 아니다. 원본 server startup/lifespan 및 `db_refresh`를 실행하지 않는다. 기본 프로젝트 가상환경에는 VDB를 설치하지 않고, 별도 runtime으로 운영한다.

검수 원본: [AppThreat/vulnerability-db 고정 소스](https://github.com/AppThreat/vulnerability-db/tree/2dc040a44986da0c4eac1366ba77633f09e593b6), MIT, Python package `appthreat-vulnerability-db==7.0.1`. 현재 bridge는 이 버전만 허용한다. 설치 pin은 `requirements.txt`; runtime의 버전 확인만으로 commit provenance를 보증하는 것은 아니다. 소스와 의존성 pin은 배포자가 관리한다.

## 실제 등록되는 도구

기본 프로필 아래 3개는 유지된다. GHSA·CPE·목록·승인 SBOM은 별도 opt-in
[`vdb_readonly_extended` 설정 안내](EXTENDED.md)를 따른다(기본 3개 + 확장 4개 = 총 7개).

| ToolId | canonical action key | 필수 입력 | 선택 입력 | 대상/최소 risk |
|---|---|---|---|---|
| analysis.vdb_status | analysis.response_analysis | asset_ref | engagement_id, run_id | asset / medium |
| analysis.search_by_cve | analysis.static_analysis | asset_ref, cve_id | limit, engagement_id, run_id | asset / medium |
| analysis.search_by_purl_like | analysis.static_analysis | asset_ref, purl | limit, engagement_id, run_id | asset / medium |

operation은 기존 명명 계약의 의미 분류를 재사용한다. 실제 선택은 ToolId와 설명·schema를 같이 읽어야 한다. `static_analysis`인 다른 APK 도구와 같은 실행 구현이라고 가정하면 안 된다. 최신 `available_tools` metadata에서 capability, operation, target_types, required/optional arguments 및 output_schema가 제공된다.

CVE는 `CVE-YYYY-NNNN…` 정확한 ID 하나만 받는다. PURL은 `pkg:npm/package-name@1.2.3`처럼 **관측된 정확한 버전**을 포함해야 한다. wildcard·범위 버전·파일 subpath는 받지 않는다. 쿼리 최대 512자, 결과 최대 운영자 설정 1–50건, 기본 limit=min(10, 운영자 상한). 결과가 잘리면 `truncated=true`다.

## 설치 및 합성 검증

저장소 루트에서 운영자가 실행한다. 아래 설치는 코드·Python 의존성만 받으며 대량 취약점 DB는 받지 않는다.

```sh
python3 -m venv ../../runtime/vdb/venv
../../runtime/vdb/venv/bin/python -m pip install -e . -r deploy/mcp/vdb/requirements.txt
../../runtime/vdb/venv/bin/python examples/vdb_mcp_demo.py
```

예제는 임시 디렉터리에 가짜 취약점 3건을 만들고, 실제 stdio MCP → Registry → Control → Gateway를 통과시킨다. 정해진 입력을 쓰는 합성 예제이지 실제 LLM Agent가 자율 판단한 결과가 아니다. 금융 사이트에 접속하지 않는다.

실제 original backend 및 bridge 테스트:

```sh
export REDTEAM_VDB_TEST_PYTHON='/absolute/runtime/vdb/venv/bin/python'
./.venv/bin/pytest -q tests/unit/test_vdb_readonly.py tests/integration/test_vdb_readonly_mcp.py
```

추가 원본 discovery 검수는 `REDTEAM_VDB_UPSTREAM_SOURCE`를 위 SHA의 별도 checkout으로 지정하고 `tests/integration/test_vdb_upstream_discovery.py`를 실행한다. 해당 검수는 갱신 대상이 아닌 신선한 합성 fixture만 원본 startup에 전달한다. Agent 실행에는 원본 startup을 사용하지 않는다. opt-in 설정이 없으면 원본 backend 테스트는 skip이며 연결 완료 증거가 아니다.

## 운영자 설정과 팀 연결

1. 운영자가 **Agent와 별도의 다운로드/빌드 절차**로 VDB7 snapshot을 준비한다. 금융 업무 파일·개인정보·인증정보를 DB 폴더에 넣지 않는다.
2. 완성·종료된 `data.vdb7`, `data.index.vdb7`, `vdb.meta`를 읽기 전용 위치에 배치한다. 심볼릭/하드 링크, WAL/SHM/journal이 남은 DB는 거절한다. 임의 마이그레이션·metadata backfill은 하지 않는다. schema가 맞지 않으면 조회하지 않는다.
3. metadata는 실제 snapshot 빌드 자료를 보존해야 한다. `completeness`를 임의로 `full`로 바꾸거나 `created_utc`를 현재 시각으로 바꿔 통과시키지 않는다. 명시적인 `completeness=full`이 없는 snapshot 및 partial/shard는 이번 bridge에서 조회 거절된다. `full`은 해당 artifact 범위의 선언이지 모든 취약점·제품·OS를 다 담았다는 보증이 아니다.
4. `policy.example.json`을 별도 운영 파일로 복사해 root, asset_ref, engagement_id, run_id와 상한을 지정한다. 실행 중 snapshot을 수정/교체하지 않는다. 갱신은 서버 종료 후 운영자 별도 절차로 수행한다. DB를 쓰기 가능한 workspace 안에 두지 않는다.
5. `readonly.env.example`의 interpreter/policy 경로를 수정하여 로드한다. `REDTEAM_MCP_SERVERS` 또는 discovery용 `REDTEAM_EXTERNAL_MCP_SERVERS`에 `vdb-readonly`를 명시한다. 기존 서버 목록은 덮어쓰지 말고 필요한 목록을 합친다.
6. Agent/Orchestration이 확정한 engagement/run과 **동일한** 정책을 provisioning한 후 `open_mcp_tool_gateway`를 연다. Agent가 정책이나 실행 ID를 임의로 선택하지 않는다. scope에 해당 지식 DB Asset ID를 제공한다. 기본 CLI의 임의 새 Run과 예제 policy ID를 자동으로 같게 만드는 기능은 없다.
7. Candidate의 target은 `AssetTarget(asset_id=asset_ref)`다. Gateway는 신뢰된 context에서 engagement/run을 주입하고 인자/schema를 검증한다. Control의 scope/ROE/budget/approval 판정을 거친 뒤 호출한다. 개별 Agent의 후보 생성·지식 조회 시점·Skill 선택은 Agent 팀이 연결한다.

stdio 프로세스는 한 engagement/run에 한 DB root만 바인딩한다. 각 조회는 별도 `sys.executable` worker에서 실행하며, AppThreat의 전역 DB 연결·설정과 임시 캐시는 해당 worker 안에서만 사용한다. `query_timeout_seconds`에는 worker 인터프리터와 모듈의 cold startup 시간도 포함된다. 시간 초과 뒤에는 최대 0.5초의 단일 cleanup budget을 쓴다. 그 안에 child와 pipe thread의 종료를 확인하지 못하면 `vdb_lookup_cleanup_uncertain`을 반환하고, 해당 reader 및 process-wide worker 슬롯을 재사용하지 않는다. 이 경우 provider 프로세스를 재시작하기 전까지 조회 용량이 줄어든 상태로 남을 수 있다. `max_concurrent_lookups`는 기본 1이며 최대 8이다. 1보다 크게 설정할 때는 연결 측 `REDTEAM_MCP_<SERVER_ID>_MAX_CONCURRENCY`도 함께 지정해야 병렬 호출이 gateway에서 대기하지 않는다. MCP ping healthy는 연결 상태일 뿐이다. 작업 시작 전에 `vdb_status`의 데이터 준비 여부를 별도로 확인해야 한다.

## 출력 해석과 한계

- `state`: ready / missing / empty / invalid / coverage_gap / unknown_metadata / backend_unavailable / backend_version_mismatch.
- `freshness`: fresh / stale / unknown. file mtime 대신 metadata `created_utc`를 기준으로 판단한다. 빌드 날짜도 원천 advisory의 완전성·신선도를 보증하지 않는다.
- `required_backend_version/required_upstream_commit`은 요구 pin, `observed_backend_version`은 확인한 설치 버전이다. `source_revision_verified=false`: runtime은 설치 버전을 확인하지만 설치된 소스 SHA를 암호학적으로 검증하지 않는다. 별도 upstream 테스트는 검수 checkout의 SHA를 확인한다.
- 오래된 DB는 stale 표시를 유지한 채 지식 조회할 수 있다. 날짜를 확인할 수 없으면 unknown이지 fresh가 아니다. missing/empty/partial/invalid는 조회 실패이며 성공한 빈 결과로 바꾸지 않는다.
- 조회 결과: advisory_id, affected_versions, purl_prefix, source, severity, score, published, updated, aliases, references, description, match_basis, text_truncated. 없는 값은 null/빈 배열이며 추측하지 않는다. 검색은 원본의 버전 비교 로직을 재사용한다. 원본 데이터 본문과 arbitrary evidence는 반환하지 않는다.
- 항상 `assessment=knowledge_only`, `target_vulnerability_confirmed=false`. 결과 0건은 `no_known_match_in_loaded_snapshot`이며 “안전”, “패치됨”, “규제 준수” 판정이 아니다. reference URL은 자료이며 자동 방문·명령 실행하지 않는다.
- 결과 건수/필드/UTF-8 출력 크기를 제한한다. SQLite progress 기반 제한은 협력적 query timeout이며 OS 메모리·CPU sandbox가 아니다. 읽기 전용 연결은 snapshot 쓰기를 막지만 프로세스 전체 권한·네트워크 egress 격리를 대신하지 않는다. 운영에서는 최소 권한/읽기 전용 mount/egress deny/프로세스 quota를 별도로 적용한다.
- 기본 프로필에는 GHSA·CPE·bulk/SBOM이 없지만 [확장 프로필](EXTENDED.md)에서 제한된 읽기 전용 조회를 제공한다. 아직 없음: 일반 URL/OSV alias/fulltext 전용 조회, patch/fix 추출, shard fan-out, Agent DB 갱신, PoC/Exploit 실행, 실제 금융 타깃 검증, 기본 LLM의 자동 선택 E2E. 방법론 자료 연결의 최신 상태는 docs/capability/methodology-agent-integration-result.md 및 approved-deployment-result.md를 따른다.
