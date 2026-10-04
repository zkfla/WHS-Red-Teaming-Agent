> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# 확장 오케스트레이터 최종 통합 요약 — 구현 워커 A/B

- 대상 계획: `docs/agent/modularization-20261004/2026-10-04-WHS-modularization-implementation-plan.md` (개정2)
- 브랜치 `dev`, base `9eb70ed1`. pytest·ruff·mypy·commit·push 없음(루트 최종 단계 전까지 금지 준수).
- 워커 A(역량 영역, McClintock) 15파일 / 워커 B(역할·자격·관측·참고 영역, Jason) 12파일. 둘 다 완료 후 종료 처리.
- 이 오케스트레이터 직접 검증: A+B 소유 27파일 `py_compile` EXIT=0, `orchestration/` 안 실제 `redteam.capability` import 0건(문서 주석 내 경로 표기 3건만).

## 1. 워커 A 변경 파일 (15)

| 파일 | 요약 |
| --- | --- |
| `capability/registration.py` | 단일 `mcp_profile` 꾸러미 kind + 프로필 7종(vdb_readonly, vdb_readonly_extended, page_text_readonly, execution, web_search_tavily, workspace_read, workspace_write) + 동작 결속/재시도/레시피/역할 기여 선언 |
| `capability/settings.py` | 환경 읽기 코어 경유, 꾸러미 주입 인자(`bundles=`) 추가 |
| `capability/bootstrap.py` | 꾸러미 배선 관통, 재시도 안전·대상 유형·탐색 결속을 등록 선언 기반으로 |
| `capability/action_binding.py` | 코어 계약 별칭 + 내장 결속표 + 내장 재시도 정책 + 얇은 판정 함수 |
| `capability/execution_recipes.py` | 코어 별칭 + 내장 line_count 레시피 + 주입 조회 |
| `capability/server_roles.py` (신규) | 런처/배포 역할 표 + 역할 종류 선언 |
| `capability/deployment.py` | 역할 출처를 역할 모듈로 전환, 배포 순수이동 보존 |
| `capability/servers/__main__.py` | 선택지를 런처 역할 표에서 |
| `capability/adapters/mcp/naming.py` | 가역 퍼센트 이스케이프 + 3구간 정식 식별자 빌더/파서 |
| `capability/catalog.py` | 정식 우선 해석(유일 대응 별칭만, 충돌 거절) + 실행 가능 쌍 목록 신규 |
| `capability/manifest.py` | 3구간 식별자 수용 + 네임스페이스 검증 |
| `capability/registry.py` | 정식 우선 해석 경로 |
| `capability/adapters/mcp/example.py` | 재시도 안전을 선언 기반으로 |
| `orchestration/target_binding.py` | 선택 주입(결속표), 코어만 import |
| `orchestration/execution_bridge.py` | 선택 주입(레시피), 코어만 import |

## 2. 워커 B 변경 파일 (12)

| 파일 | 요약 |
| --- | --- |
| `orchestration/agents/packages.py` (신규) | 역할 꾸러미 + 결속 검사 + 기여 변환 |
| `orchestration/agents/registry.py` | 역할 꾸러미 등록 경로 |
| `orchestration/agents/providers.py` | 제공자 등록 정합 |
| `orchestration/agents/__init__.py` | 공개 노출 |
| `orchestration/eligibility.py` (신규) | 공통 자격 5종 이동 + 자격 레지스트리 + 기본 레지스트리 |
| `orchestration/analysis_activation.py` | 재노출 전환, 내장 정적 분석 제공자·기여 함수 유지 |
| `orchestration/mobile_mcp_binding.py` | 벤더 정책 + 배선 인자(`vendor_policies=`) |
| `orchestration/observations/packages.py` (신규) | 관측 기여 꾸러미 |
| `orchestration/observations/mapper.py` | 해석 기여 + 지문 키 조회 + 지연 로드 |
| `orchestration/observations/__init__.py` | 공개 노출 |
| `orchestration/methodology.py` | 참고 읽기 계약/수명주기 참조 + 도메인 어휘 코어 위임 |
| `capability/methodology.py` | 방법론 읽기 계약 + 도메인 어휘 코어 위임 |

## 3. 통합 API 확정본 (kind 기준)

- kind: A = `mcp_profile`, `capability.action_binding`, `capability.retry_policy`, `capability.execution_recipe`, `capability.server_role` / B = `agent_role`, `candidate_provider`, `eligibility_provider`, `vendor_policy`, `result_mapper`, `methodology_read_contract`, `reference_bundle` / 루트 런타임 = `agent_definition`, `agent_spec`, `candidate_provider`, `result_mapper`, `planning`.
- A 핵심(실물 확인): 프로필 꾸러미 = profile/apply_settings/build_adapter/bind_discovery/default_target_types + `builtin_profile_bundles()` + `from_environment(server_id, env=, bundles=)` + 게이트웨이 build/open `profile_bundles=` 관통. 식별자 = `escape/unescape_tool_segment`(가역), `build/parse_canonical_mcp_tool_id`(3구간), `is_canonical_mcp_tool_id`, `legacy_alias_for_tool_id`(2구간 호환). 카탈로그 = `resolve()`(정식 우선) + `executable_pairs()`. 역할 = `launcher_role_names`/`deployment_role_names`/`role_spec`/`role_operations`. 실행 = `is_execution_operation(recipes=)` + 레시피 인자 조립. 결속 = `should_bind_target_arguments`/`should_bind_known_path`/`is_retry_safe_action(policy=)`. 오케스트레이션 소비는 선택 주입, None이면 기존 동작.
- B 핵심(실물 확인): `RolePackage`(+missing_required)/`check_role_package_bindings`/`role_package_contributions`. 자격 = Scope/Decision/Context/Provider/Registry + `default_eligibility_registry()`, `analysis_activation` 재노출 유지. 벤더 = `VendorPolicy` + `builtin_mobile_vendor_policies()` + `wire_mobile_mcp(..., vendor_policies=)`. 관측 = `register_contribution`/`mapper_contribution`(factory가 해석 callable 반환)/`mapper_declarations_by_contribution_id`/`load_mapper_contribution`/`meaningful_change_keys`/`all_meaningful_change_keys`. 참고 = `MethodologyReadContract` + 기여, `ReferenceReadContract`/`ReferenceLifecycleRefs` + 엔트리 선택 필드(기본값).
- 재시도/결속 계약 본체는 코어 공용 계약 모듈(`core/provider_contracts.py`)이고 A는 별칭 import만(로컬 중복 정의 제거).
- 도메인 어휘 단일화 실물 확인: `capability/methodology.py`·`orchestration/methodology.py` 모두 `core/reference.py`의 `ReferenceDomain`을 import(N5 통보분 반영 상태).

## 4. 루트 패치 상태 (2026-10-04 루트 통보 + 실물 확인)

A(공유 파일) — P1~P4 루트 소비 완료:
- P1 `orchestration/candidates/compiler.py` — 결속표 주입 수용·사용 완료(engine.py:1872가 의존성 결속표 전달).
- P2 `orchestration/nodes/execute.py` — 고정 동작 목록 제거, 서술자 재시도 안전 or 재시도 정책 판정으로 전환 완료(execute.py:235).
- P3 `orchestration/engine.py` — 레시피 주입 전 구간 전달 + 레시피 기반 동작 판정 완료(577/608/960/1691/1715/2824/2840).
- P4 `app/bootstrap.py` + `app/mcp_deployment.py` — 신선 꾸러미 지도 조립 + 중복 명시 오류 + 게이트웨이 관통 완료(bootstrap.py:189-198/302/554/560), 역할 출처 역할 표 전환 완료(mcp_deployment.py:20/76).
- P5 `reporting.py` — 노출만, 편집 금지(별도 워커 M25).
- 쌍 목록: 해석 등록부가 카탈로그 단일 해석으로 기존 별칭을 정식 식별자에 결속(bind_catalog) 완료 — 앱 조립 2곳 호출(bootstrap.py:303/568), 모호 별칭은 건너뜀. `core/action_semantics.py` 쪽 최종 정리는 루트 최종 단계.

B(공유 파일):
- N2 `app/bootstrap.py` — 모바일 배선 2곳에 벤더 정책 전달.
- N3 `app/methodology.py` — 읽기 도구 대조를 참고 읽기 계약 기반으로.
- N6 시작 전 역할 꾸러미 결속 검사(`check_role_package_bindings`, 필수 누락=시작 실패, 선택=격리+표시).
- N1 import 경로 1줄 — 자격 타입을 `orchestration/eligibility`에서.
- 루트 적용 통보분(재확인 권장): N1 본체 4지점, N4 지문 키, N5 도메인 어휘.

## 5. 영향 M표 (워커 보고서 근거)

- A: M06 완료(내장 결속표+선택 주입, 루트 소비 확인), M08 완료(내장 레시피+선택 주입, 루트 소비 확인), M14/R04 재개정 — A 보고의 '완료'는 어댑터 탐색이 정식 3구간 식별자를 실제 생성하지 않던 결손이 있었음(§7 루트 직접 보완으로 코드 경로 완결). M25 제공자 측 완료(app 소비는 P4, 루트 소비 확인), M30 유지(실측 import 0), M09 보존.
- B: M02·M07 코드 완료(N6 잔여), M03 코드 완료(N1 잔여), M04 코드 완료(N2 잔여), M05 완료(의미 변화 키 24+1건), M18 선언 완료(N4 잔여/통보), M27 N5 통보, M28 자료 완료(N3 잔여), P2 실행 스킬은 자료 구조 문서만.
- M13·M15는 A·B 보고서에 영향 언급 없음(다른 워커/루트 범위).

## 6. 미검증·한계·경합

- 실행 검증 0건(py_compile만). pytest/ruff/mypy 금지 준수.
- 참조 문자열 25건, 지연 불러오기 순환 회피, 해석 factory 동작은 실행 시 확인 필요(B).
- 공유 트리 경합: engine/app/reporting/control 동시 수정 중 — 패치 노트 행번호 적용 전 재확인(A).
- 스트리밍 HTTP 어댑터의 재시도 안전 기본값은 탐색 시 덮어쓰여 비권위(A).
- 참고 꾸러미 버전 규칙(해시 앞 16자)은 가안, R03 바인딩 규칙 루트 결정 필요(B).
- 자격 규약의 코어 이동 여부 루트 결정 필요(B).
- `reporting.py` 미편집 확인.

## 7. 루트 직접 보완·통합 (2026-10-04 통보, 실물 확인)

- 레지스트리 해석 정식 경로: 질의가 정식 3구간이면 물리 3요소(서버/원격/역량 태그) 정확 일치만 채택, 없으면 None — 잘못된 서버로의 대체 제거; 중복 결속은 거절(registry.py:53-64).
- 일반 스트리밍 HTTP 어댑터 탐색: 정식 3구간 식별자를 실제 생성(streamable_http.py:55). 고정 식별자를 쓰는 명시 어댑터(예: example.py의 recon.ping)는 기존 정확 제공자 검사 유지.
- 해석 등록부 카탈로그 결속: 기존 별칭을 카탈로그 단일 해석으로 정식 식별자에 연결, 모호하면 건너뜀(출력 추측 없음) — mapper.py:183, 앱 조립 2곳 호출(bootstrap.py:303/568).
- A 소비 4종(P1~P4) 실물 확인 완료(§4).
- M14 재개정 근거: A 보고 시점에는 정식 식별자 빌더/파서/수용만 있고 어댑터 생성·해석 경로가 따라오지 않아 "DONE"은 과대 주장이었음. 위 보완으로 코드 경로 완결.
- uv.lock 출처(루트 문의): 저장소 루트 `uv.lock`은 A/B 워커 산출물이 아님. 별도 채팅의 서브에이전트(세션 01a10348-3e95..., 부모 01a10329-...)가 2026-10-04 04:52:30(KST) `uv sync`를 실행해 생성. A/B 세션에는 생성 명령 기록 없음(git status 관찰 기록만). `docs/uv.lock`이라는 파일은 존재하지 않음.
