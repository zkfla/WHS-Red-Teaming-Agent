> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# 확장 오케스트레이터 통합 산출물 — 도구·MCP·정책 준비 영역 + 역할·자료·해석·자격 영역

- 기준: 브랜치 dev, 커밋 9eb70ed1f9fff1fbf15540731de87de89feaf7fe, 조사일 2026-10-04. 읽기 전용(코드 수정·테스트 실행·설치·Git 변경 없음).
- 수행: 오케스트레이터 1명 + 워커 2명(모델 Muse 1.3 Contributor, 추론 강도 xhigh). 워커 원본은 같은 디렉터리의 worker-a-capability-mcp-findings.md, worker-b-roles-methodology-findings.md. 본 문서는 워커 제안을 루트의 후속 방향으로 개정한 통합본이다.
- 담당 M: M01~M18, M25, M27, M28 (17개). 비담당: M19~M24, M26, M29, M30.
- 근거 문서: docs/agent/modularization-20261004/2026-10-04-WHS-modularity-coupling-audit.md, docs/agent/modularization-20261004/2026-10-03-WHS-closed-loop-implementation-plan.md, docs/agent/modularization-20261004/2026-10-03-WHS-validation-contract-audit.md, 사용자 제공 자료/dev-mcp-audit-20261004.md, 같은 폴더 dev-agent-audit-20261004.md.
- 인용한 파일:행은 오케스트레이터가 직접 열어 확인한 것만 쓴다. 실행 재현·테스트 통과는 주장하지 않는다.

## 1. 루트 방향과 결론

루트 방향(그대로 반영): (1) 고정 전역 어휘를 중앙에서 계속 수정하는 방식 금지 (2) 이름별 명명 조건 분기를 다른 파일로 옮기는 방식 금지 (3) 신규 설치 패키지마다 중앙 화이트리스트 수정 금지 (4) 승인된 패키지 entry point/명시 앱등록으로 선언 수집, pluggy는 이 수집에만 (5) 런타임 자격·재시도는 등록된 Protocol/callable 담당 (6) 이름·형식·범위·정책 검사 유지 (7) 조리법·준비조건은 제공자 소유로 이동 (8) 참고 자료에 실행부 강제 금지 (9) 참고/실행 스킬은 기존 참고 기여·계획·행동 제공 계약으로 연결(별도 거대엔진 금지).

결론:
1. 확장 단위는 제공자 선언 하나다. 설치된 승인 패키지의 entry point 또는 명시 앱등록이 선언을 제출하고, 조립부가 pluggy 수집 훅으로 모아 공통 구조·참조 검사로 결속한다. 선언에는 제공 기능, 참조(팩토리 import 경로), 버전, 적용 대상만 담는다.
2. 어휘(능력·동작)는 내장 기본 제공자 + 등록 선언의 합집합을 유효 어휘로 삼는다. 형식 검사(lower_snake_case), 중복·소유자 검사, 참조 해석 검사는 조립 검사에 남는다. 등록은 실행 허가가 아니며 계획·범위·정책·승인·원본 증거 검사는 그대로다.
3. 자격(역할·도구 가시성)과 재시도 의미는 등록된 Protocol/callable이 수행·선언하고, 중앙 엔진은 프롬프트·참고 후보·행동 컴파일·실행 직전 네 수명주기 지점에서 호출만 한다. 계획 시점과 실행 직전 재검사는 둘 다 유지한다(카탈로그 상태가 사이에 바뀔 수 있음).
4. 조리법(고정 실행)과 준비조건(예: 정적 분석 자산·도구 조건)은 제공자 소유로 이동한다. 중앙은 승인된 조리법 외 명령 차단, 입력 스키마·실제 대상 일치, 위험·정책 검사를 유지한다.
5. 참고 자료는 기존 참고 기여 계약(해시 고정 카탈로그 + 읽기 포트 + 맥락/갈무리)으로, 실행 스킬은 기존 역할 꾸러미·후보 제공자·계획 단계 계약으로 연결한다. 스킬 전용 엔진·실행기는 만들지 않는다.
6. 공통 계약(core)과 앱 설정·조립 경계 변경은 루트 소유 안건으로 분리 보고한다(9절). 내부 사설 접근 우회는 쓰지 않는다.

## 2. 교차 검토 정정·해소

- 정정 1(워커 B): M07 근거 행 engine.py:694가 어긋난다는 판단은 틀렸다. 실측 694행이 제공자 조회 줄이고 695행이 None 분기 시작이다. 감사 문서 인용과 일치한다.
- 정정 2(워커 B): M03의 승인 지점은 실측 1587 부근이라는 서술은 부분만 맞다. 1587행은 compile_actions 안의 activation 스냅샷 호출이고, 인라인 거절 비교는 1617~1625행 블록이다. 감사 문서의 1616 인용도 같은 블록이다. 드리프트가 아니라 같은 블록의 다른 줄이다.
- 정정 3(워커 B 미검증 2): 참고 후보 연산(engine.py:886 _reference_live_operations)이 도메인 자격을 안 본다는 의심은 결함이 아니다. 상위 루프(engine.py:683~685)가 analysis_role_visible로 역할을 먼저 거르고, 852 _reference_candidate_context → 886 호출 순서라 역할 자격이 선행 적용된다. 다만 실행 준비 가시성(execution_ready=False)을 별도로 보는 이중 확인은 의도된 현재 동작이다.
- 정정 4(워커 A): dev-agent-audit-20261004.md를 경로 공백 때문에 못 읽었다고 했으나 오케스트레이터가 전문을 대조했다. 4.2(역할은 고정 검사 목록보다 역할 설명·기준 제공), 4.3(실행 연결은 고정 조리법 전제), 6절(해석기는 정확한 도구 식별자별, MobSF는 별도 등록) 모두 본 문서 결론과 충돌하지 않는다.
- 확인(오케스트레이터): M15의 자산 인자 검사는 조건부다(control/scope.py:361~370). 인자에 asset_ref가 있고 대상 자산ID와 다를 때만 거절하며, 없으면 즉시 거절이 아니라 허용 자산 목록 대조로 간다. 범위 우회 취약점은 재현하지 않았다.
- 확인(오케스트레이터): M16의 오케스트레이션 판독은 별도 구현이 맞다(nodes/policy.py:48 denial_severity가 문자열 접두사를 직접 읽음). 정의는 공통 열거 하나만 쓴다(동일 파일 주석·상수 파생). M25의 배포 대조는 app/mcp_deployment.py:131 역할 부분집합 검사 + 환경·argv·허용 목록 대조가 맞다.

## 3. 최소 제공자 계약과 내장 제공자 이관 지도

공통 골격(루트 소유 제안): ExtensionDeclaration(id, version, kind, vocabularies[], tools[], profiles[], roles[], qualifications[], observations[], references[], surfaces[]) — 조립 시 수집·검사. 검사 항목: 선언 식별자·버전 유일성, Pydantic 스키마 적합, 참조(팩토리·해석기·자격 제공자) 해석 가능 여부, 어휘 형식·중복 소유 금지, 능력·동작 소유자 일치. 미충족은 시작 실패(필수) 또는 격리·표시(선택).

| 제공자 종류 | 최소 계약 | 내장 제공자 이관 파일(현행 → 등록 경로) | 소비자 경로(현행 유지) |
|---|---|---|---|
| 어휘(능력·동작) | CapabilityVocabularyDecl(capability, operations[], version). 형식은 lower_snake_case, 중복 소유 금지. | core/action_semantics.py의 frozenset 2종 → 내장 선언(위치 루트 결정). 미등록 이름은 계속 거절. | manifest._validate_identity(정본 대조), catalog.list_tools_for_action(접두 필터), engine.compile_actions(동작 바인딩), paths/writing.writing_schema(enum 주입), intake/models·프롬프트, app/cli/commands/validate(사전 점검) |
| MCP 서버·프로필 | ServerProviderDecl(profile, required_kind, required_transport, settings_schema, exact_allowlist, exact_bindings, connector_ref, default_target_types, discovery_check_ref) | capability/settings.py 프로필 분기(vdb :134, page_text :152), capability/bootstrap.py _create_adapter(:287)·_default_target_types(:270), capability/manifest.py 명세 검사 | app/bootstrap.py build_application → open_mcp_tool_gateway(조립) |
| 도구 신원 | 등록 시 부여되는 안정 신원(서버 구분 포함 형식은 루트 결정). naming.build_mcp_tool_id(:26)는 정책으로 유지. | 형식 확정 시 capability/adapters/mcp/naming.py·registry.py(:30 register, :59 replace_server_tools 충돌 거절 유지) | catalog·gateway·증거 식별자 연결(기존 승인·바인딩·저장 증거 호환은 별도 결정) |
| 대상 바인딩 | TargetBindingDecl(target_kinds[], identity_arg_map{}, strictness, path_rule) | orchestration/candidates/compiler.py 동작 분기(web_search :377, endpoint_discovery :389~398), target_binding.validate_network_arguments(:50 엄격도), control/scope.py(:352 check_scope, :361 자산 검사, :380 미해석 거절) | CandidateCompiler, engine.compile_actions, 실행 직전 재검사 |
| 조리법·재시도 | ExecutionRecipeDecl(allowed_tools[], recipe_source, retry_safe, retry_condition) | orchestration/execution_bridge.py(EXECUTION_OPERATIONS :23, bind_execution_arguments :210~227 고정 command_run), nodes/execute.py(_RETRY_SAFE_ACTIONS :40, _retry_safe :50, 게이트 :226) | engine.compile_actions(실행 동작), execute 노드(게이트 4조건 AND는 중앙 유지) |
| 역할 꾸러미 | RolePackage(role_id, definition, spec(스케줄 역할 필수), provider_ref, required_providers[], optional_providers[], default_include) | orchestration/agents/definition.py builtin_agent_definitions(:99), builtin.py 실행 명세 4종, providers.py default_provider_registry(:302), registry.py default_registry(:292), app/runtime_components.py _builtin_factory(:70) | AgentRegistry(스케줄·맥락), engine, runtime_components 조립. 정의·명세 능력 일치 검사(:57) 유지 |
| 자격 제공자 | QualificationProvider Protocol(async role_eligible, tool_eligible, asset_scope) + 선언(applies_to) | orchestration/analysis_activation.py(is_static_analysis_tool :26, analysis_activation :39) → 내장 자격 제공자, engine.py 호출 4지점(:539, :683, :1587+, :2752+) | engine 4지점이 제공자 호출로 소비. 스키마·대상·정책·승인 검사는 중앙 유지 |
| 해석·변화 | ObservationPackage(tool_ids[], mapper_ref, mapper_version, meaningful_change_keys[], projection_ref) | orchestration/observations/mapper.py(:176 register, :207 기본 목록) → 내장 패키지, orchestration/mobile_mcp_binding.py(:128 조건부 mobsf 등록) → 벤더 선언 | runtime_components 조립, nodes/update(map_result), attack_graph/observation.py(:35 키 목록, :95 내용 해시) 지문 계산 |
| 벤더 정책 | VendorPolicyDecl(vendor_id, managed_server_ids[], fallback_map{}, approval_ref) | orchestration/mobile_mcp_binding.py 값(:53 주·대체, :83 관리 집합, :117 wire) → 선언, 검사 로직(해시·스코프·핀·건강)은 공통 유지 | app/bootstrap.py(:526, :645) 래핑 호출, 카탈로그·게이트웨이 포트 |
| 참고 기여 | ReferenceContributionDecl(reference_id, path, sha256, domains[], source_ref, read_contract, lifecycle_refs, max_context_chars) | capability/methodology.py 카탈로그(Domain :25, Reference/ReferenceCatalog), app/methodology.py wire_reference_agent(:82 특정 도구 확인 → 읽기 계약 해석), orchestration/methodology.py 수명주기 | engine 맥락 조립(:565 부근), nodes/verify.py(:118 capture), context.RuntimeDeps.methodology_library(:63) |
| 표면(보고·실행기·배포) | SurfaceDecl(report_keywords[], server_roles[], deployment_roles[]) | orchestration/reporting.py(_WORKSTREAM_FAMILIES :33), capability/servers/__main__.py(역할 6종 :11), capability/deployment.py(DeploymentSpec.roles :66) | reporting 투영, app/mcp_deployment.py 대조(:131 부분집합 + 환경·argv·허용 목록) |

공통: 선언 스키마·공통 검사·수집 훅은 루트 소유(9절 제안). 조립부는 선언→설정 해석→제공자 객체 생성→기존 등록부 적재 순서로만 일하고, 각 검사는 위 소비자 경로에 그대로 남는다.

## 4. 이관 지도 — 도구·MCP·정책 준비 (M01, M06, M08, M13, M14, M15, M16, M17, M25)

- M01(P1, 실결함) — 현재: 정본 어휘(core/action_semantics.py:11 CANONICAL_CAPABILITIES, :12 CANONICAL_OPERATIONS) + 복사본 2(intake/models.py:28 튜플, app/cli/commands/validate.py:145 하드코딩 집합) + 파생 1(paths/writing.py:54 enum 주입). 루트 방향 반영 이관: 어휘를 내장 선언 + 등록 선언 합집합으로 만들고 복사본 2를 유효 어휘 소비로 전환. 형식·중복·소유자 검사는 조립 검사로 유지. 새 능력·동작은 선언된 제공자만 소유. 소비자: 사전 점검·초안 프롬프트·작성 스키마·명세 검사·후보 조회. 미검증: import 방향(초안 모델→core) 순환 여부는 루트 확인 필요.
- M06(P1, 실결함) — 현재: 동작별 대상값 바인딩 특례 2(compiler.py:377 web_search, :389~398 endpoint_discovery), 주소 대조 엄격도 차등(target_binding.py:50 page_read URL 전체일치), 고정 조리법 1(execution_bridge.py:210~227 analysis.command_run만). 이관: 바인딩 규칙·엄격도·조리법을 제공자 선언으로 이동. 중앙은 실제 대상 일치·스키마·승인·고정 인자 적용·조리법 외 명령 차단 유지. 모델 자유 명령 전환 금지. 미해소: 새 제공자가 page_read급 엄격도를 스스로 낮춰 선언하는 문제 → 중앙 하한(최소 엄격도) 필요 여부 루트 결정.
- M08(P2, 운영 제약) — 현재: 재시도 안전 집합 4건 + web GET/HEAD 무본문(execute.py:40, :50), 게이트 4조건 AND(:226). 이관: 재시도 의미를 제공자 선언(retry_safe + 명명 조건)으로 옮기고 게이트는 중앙 유지. LOW 단독 재시도 금지 원칙 유지. 미해소: 조건 표현은 명명 열거 우선(일반식은 과설계).
- M13(P1, 실결함) — 현재: 설정 해석(settings.py:134 vdb, :152 page_text 프로필 정확 일치), 어댑터 선택 if 사슬(bootstrap.py:287, :289 서버ID 특례 mobsf), 기본 대상 힌트(:270). 이관: 프로필 4점(설정 스키마·연결기·대상 의미·명세 검사)을 ServerProviderDecl로 묶고 조립부가 등록 해석. 기존 종류별 일반 경로는 기본 등록으로 호환. 발견은 승인 패키지 entry point + 명시 앱등록(중앙 화이트리스트 수정 없음).
- M14(P1, 실결함) — 현재: 식별자 공식 naming.py:26(서버 미포함) + 충돌 거절 registry.py:59 + 접두 필터 catalog.py:199 + 명세 검사 manifest.py:80. 이관: 안정 서버별 신원을 등록 시 부여(형식은 루트 결정). 충돌 거절·접두 필터·명세 검사 유지. 식별자 호환(승인 목록·바인딩·저장 증거)은 별도 결정 항목. 미검증: 정규화 충돌 실제 재현은 안 함.
- M15(P1, 실결함) — 현재: 대상 9종 합집합(core/contracts/target.py:86) + 호스트 신원 해석(scope.py:157) + 조건부 자산 검사(:361~370) + 미해석 거절(:380). 이관: 대상 종류별 범위 해석 등록 + 도구별 신원 인자 매핑 선언(identity_arg_map). 모르는 대상 기본 거부·asset_ref 불일치 거절 유지. 에이전트 id와 대상 계정(principal) 구분 유지. 공통 계약 변경분은 루트 조율.
- M16(P1, 실결함+동결 제약) — 현재: 심각도 꼬리표 운송(control/severity.py:38 to_tag) + 별도 판독(nodes/policy.py:48). 이관 1단계: 쓰기·읽기 함수 1쌍을 공통 계약에 올려 양쪽 import(최소안). 2단계: PolicyDecision 정식 필드 승격(동결 계약 변경, 버전·마이그레이션 결정 필요). 위험(호출 전)·후속 대응(거부 후)·취약점 심각도 축 분리 유지.
- M17(P1, 운영 제약) — 현재: 집행 신호가 자유 메타데이터(core/contracts/policy.py:18 ControlContext.metadata). 부재 의미가 정반대다: 병렬 제한 거절(roe.py:192 fail-closed), 속도 제한 구경로 허용(rate_limit.py:109 legacy). 이관: 비행 중 수 등 필수 신호를 정형 계약으로 승격하되 부재 의미는 현행 문문화. 값 제공은 호출부 책임. 미검증: 주 실행 경로의 실제 전달 전수(호출부 인벤토리)는 루트·오케스트레이션 측 확인 필요.
- M25(P2, 운영 제약) — 현재: 보고 갈래 5종(reporting.py:33), 공용 실행기 역할 6종(servers/__main__.py:11), 배포 역할 2종(deployment.py:66), 동기화 대조(mcp_deployment.py:131). 이관: 3목록을 각 제공자 선언으로 분리(통합 금지). 중앙은 키워드 합성·매칭 실패=미분류, digest·환경·argv·허용 목록 대조 유지. 보고 누락은 실행 불능이 아니다.

## 5. 이관 지도 — 역할·자료·해석·자격 (M02, M03, M04, M05, M07, M18, M27, M28)

- M02(P0, 실결함) — 현재: 능력치 이중 보유(builtin.py:28 주석 계약, definition.py 정의) + 등록 일치 검사(registry.py:57) + 기본 조립 2목록(registry.py:292, providers.py:302). 이관: RolePackage 단위로 정의·명세·제공자·기본 포함을 묶고, 능력치는 정의 소유에서 파생·검증. 직접 주입 등록 API 유지. 테스트(tests/unit/test_agents.py:446~470)는 기본 샘플 기대값이지 확장 제한이 아니다.
- M03(P0, 실결함) — 현재: 자격 계산 1곳(analysis_activation.py) + 엔진 인라인 소비 4곳(engine.py:539, :683, :1587+1617~1625, :2752+). 이관: 자격을 QualificationProvider Protocol로 등록, 엔진은 4지점 호출만. 계획+직전 재검사 유지. 자격 제공자는 가시성만 정하고 실행 승인을 대신하지 않는다. 플러그인 훅 아님(런타임 callable).
- M04(P0, 절반 실결함+절반 운영 제약) — 현재: 벤더 값 하드코딩(mobile_mcp_binding.py:53, :83, :117) + 3겹 승인 검사(해시 형식·스코프·호출 핀). 이관: 주·대체 관계·관리 집합만 VendorPolicyDecl로 이동, 검사 로직 유지. 비관리 서버 통과 현행 유지. 미해소: 승인 해시 운영자 발급·회전 절차는 코드에 없음(운영 문서 몫).
- M05(P0, 실결함) — 현재: 기본 해석 목록(mapper.py:207) + 조건부 등록(mobile_mcp_binding.py:128) 분산, 정확 도구 식별자 일치(:176), 미등록=빈 변경(차단 아님). 이관: 도구와 해석 제공자를 ObservationPackage 한 단위로 동봉(의무 아님, 없으면 구조화 반영 없음 명시). 검증(_validate_mutation)·실패 격리(last_failure) 유지. 같은 해석기 여러 도구 공유 허용.
- M07(P1, 실결함) — 현재: 문자열 제공자 연결(definition.py:114) + 수집 시점 스킵(engine.py:694~699 진단 후 continue). 이관: 시작 시 결속 검사(필수 실패·선택 격리+표시). 참고 후보 누락은 조사 종료가 아니다. 필수/선택 구분은 RolePackage 선언. 미해소: 실패 정책은 루트 결정.
- M18(P1, 실결함) — 현재: 계획 관련 키 허용 목록(observation.py:35) + 역할별 지문(registry.py:165 revision_fingerprint) — 목록 밖 사실은 무변화. 이관: ObservationPackage에 meaningful_change_keys/projection_ref로 변화 규칙 동봉, 지문 계산기가 합성 소비. 시각·증거번호 제외, 모르는 키 무시 유지. 메인 진행 지문(progress.py:14)은 별도 경로로 손대지 않는다.
- M27(P1, 실결함) — 현재: 분야 8값 어휘 이중(capability/methodology.py:25, orchestration/methodology.py:33) + 역할 관련성=분야∩능력(orchestration/methodology.py:208). 이관: 분야 어휘 1소유자(공통 계약, 루트 조율) + 관련성 매핑 1곳. 분야와 실행 능력 축 분리 유지(합치기 금지). 미해소: 관련성 규칙을 중앙 매핑표로 둘지 선언으로 둘지.
- M28(P1, 실결함) — 현재: 특정 읽기 도구 고정(app/methodology.py:82 analysis.read_file + asset_ref const) + 전담 역할 조립(:102) + 중앙 직접 호출(engine, verify.py:118). 이관: 읽기 계약(필요 스키마 선언, 도구 식별자 고정 해제)+수명주기 제공자 참조로 교체. 해시·문맥 일치·상한·capture/진행/발췌 로직 유지. 참고 자료에 실행 제공자 필드 없음(SKILL.md 포함, capability/methodology.py:1 독스트링 의도 유지).
## 6. 참고/실행 스킬 연결 방안 (별도 거대엔진 금지)

루트 지시대로 스킬 전용 엔진·실행기를 신설하지 않고 기존 계약 두 갈래로 연결한다.

- 참고 스킬(문서·방법론 묶음): 기존 참고 기여 계약에 그대로 얹는다. 등록 항목은 현행 ReferenceEntry(reference_id, path, sha256, title, domains, source_ref, review_caveat) + 읽기 계약(read_contract: asset_ref 바인딩 읽기 스키마) + 수명주기 참조(진행·발췌·갈무리 제공자). 실행 제공자 필드는 두지 않는다. 기존 동작(해시 고정·신뢰 표시 untrusted_reference·execution_permission=False·내용 상한)은 유지된다. 새 문서 1개 추가는 선언 항목 추가로 끝나고, 새 읽기 뒷단은 읽기 계약 구현 교체로 들어온다.
- 실행 스킬(도구·권한이 필요한 능력): 기존 역할 꾸러미 + 후보 제공자 + 계획 단계 계약으로 표현한다. 곧 (1) RolePackage로 역할·맥락·제공자 등록(기존 AgentSpec.playbook의 PlanStep 재사용), (2) 후보 제공자로 참고 후보 제시, (3) 실행은 기존 도구 바인딩·조리법 제공자·정책 검사를 그대로 통과. 스킬이 새 도구·동작·자격을 요구하면 해당 제공자 선언을 함께 제출해야 등록된다(없으면 등록 실패). 별도 권한 체계를 만들지 않고 기존 범위·정책·승인·증거 계약을 그대로 쓴다.
- 경계: 참고 자료 등록에 실행부를 강제하지 않는다. 실행 스킬과 참고 스킬은 같은 등록 경로에 섞지 않는다(선언 kind로 분리). 사용자 목표의 스킬 확장 범위(참고 전용인지 실행 포함인지)가 확정되기 전에는 실행 스킬 쪽 구현을 열지 않는다.

## 7. 통합 선언 초안 (Pydantic 스케치 — 도메인/도구 측 최소 제공 정보)

```python
class VocabularyDecl(BaseModel):          # 어휘. 루트/core 계약 연결
    capability: str                       # 형식 검사 + 중복 소유 금지
    operations: list[str]                 # 형식 검사 + 제공자 소유
    version: str

class ServerProviderDecl(BaseModel):      # MCP 서버/프로필
    provider_id: str
    profile: str | None = None
    required_kind: str | None = None
    required_transport: str | None = None
    settings_schema: dict | None = None
    exact_allowlist: list[str] | None = None
    exact_bindings: dict[str, list[str]] | None = None
    connector_ref: str | None = None      # import 경로 문자열, 조립부 해석
    default_target_types: list[str] = []
    discovery_check_ref: str | None = None

class TargetBindingDecl(BaseModel):       # 동작-대상 의미
    operations: list[str]
    target_kinds: list[str]
    identity_arg_map: dict[str, str] = {}
    strictness: Literal['standard', 'url_exact'] = 'standard'
    path_rule: str | None = None

class RecipeDecl(BaseModel):              # 조리법·재시도
    operations: list[str]
    allowed_tools: list[str] = []
    recipe_source_ref: str | None = None  # 운영자 설정 참조
    retry_safe: bool = False
    retry_condition: Literal['http_get_no_body', 'observation_only'] | None = None

class RolePackage(BaseModel):             # 역할
    role_id: str
    definition: AgentDefinition          # 능력 소유자
    spec: AgentSpec | None = None         # 스케줄 역할 필수, 능력은 정의에서 파생
    provider_ref: str                     # 후보 제공자 import 경로
    required_providers: list[str] = []
    optional_providers: list[str] = []
    default_include: bool = False

class QualificationDecl(BaseModel):       # M03/M04 자격. 구현은 Protocol(callable)
    provider_ref: str
    applies_to: dict[str, list[str]]      # capability/operation/server 목록
    version: str

class ObservationPackage(BaseModel):      # M05/M18
    tool_ids: list[str]
    mapper_ref: str
    mapper_version: str
    meaningful_change_keys: list[str] = []
    projection_ref: str | None = None

class VendorPolicyDecl(BaseModel):        # M04 값
    vendor_id: str
    managed_server_ids: list[str]
    fallback_map: dict[str, str] = {}
    approval_ref: str

class ReferenceContributionDecl(BaseModel):  # M27/M28. 실행부 없음
    reference_id: str
    path: str
    sha256: str
    title: str
    domains: list[str]
    source_ref: str
    review_caveat: str
    read_contract: ReadContractDecl
    lifecycle_refs: list[str] = []
    max_context_chars: int | None = None

class SurfaceDecl(BaseModel):             # M25 표면
    report_keywords: list[str] = []
    server_roles: list[str] = []
    deployment_roles: list[str] = []

class ExtensionDeclaration(BaseModel):    # 패키지/모듈당 1개
    declaration_id: str
    version: str
    vocabularies: list[VocabularyDecl] = []
    servers: list[ServerProviderDecl] = []
    bindings: list[TargetBindingDecl] = []
    recipes: list[RecipeDecl] = []
    roles: list[RolePackage] = []
    qualifications: list[QualificationDecl] = []
    observations: list[ObservationPackage] = []
    vendor_policies: list[VendorPolicyDecl] = []
    references: list[ReferenceContributionDecl] = []
    surfaces: list[SurfaceDecl] = []
```

필수/선택: provider_ref·factory_ref·mapper_ref 등 참조는 문자열 import 경로로 받고 조립 시 해석한다(구현 객체 직렬화 금지). 참고 자료 선언에는 실행 제공자 필드가 없다. 내장 기본 제공자는 같은 스키마의 첫 선언(패키지 내장)으로 이관한다.

## 8. pluggy / pydantic-settings 적용 범위

- pluggy: 용도는 조립 선언 수집 하나다. 훅 스펙 예: collect_extension_declarations() 로 list[ExtensionDeclaration] 수집. 수집원 2개: 승인된 설치 패키지의 entry point 그룹(설정으로 승인된 이름만) + 명시 앱등록(내장·직접 모듈). 런타임 훅(자격·재시도·해석 호출)은 쓰지 않는다. 그건 등록된 Protocol/callable이 담당한다(추적성·검사 위치 보존).
- pydantic-settings: 적용처는 설정 해석 경계 1곳(환경변수 접두어→모델)이다. 판단 기준은 유용성이며, 현행 사용 0건은 반대 근거로 쓰지 않는다. 도메인 선언 모델은 일반 Pydantic으로 두고 BaseSettings를 끌어오지 않는다. 앱/core 설정 경계는 루트 소유라 위치·범위는 루트가 확정한다.
- 금지선: 설정 라이브러리 도입으로 기존 환경변수 의미를 조용히 바꾸지 않는다(동일 필드·동일 기본값·동일 실패 메시지). 수집 훅 도입으로 승인 목록 없는 임의 자동 발견을 만들지 않는다(선택된 패키지 원칙).

## 9. 루트 확정 최소 계약 (통합, 중복 제거)

| # | 항목 | 선택지 | 추천 | 소유 |
|---|---|---|---|---|
| 1 | 확장 골격·수집 방식 | A entry point+명시 등록+pluggy 수집훅 / B 중앙 화이트리스트 상수 | A (패키지 설치·등록만으로 확장) | 루트 |
| 2 | 어휘 확장 규격 | A 내장+등록 합집합, 형식·소유 검사 / B 전역 frozenset 중앙 수정 | A (루트 방향) | 루트/core |
| 3 | 서버 제공자 선언 필드·위치 | A ServerProviderDecl / B 현행 프로필 분기 유지 | A | 루트(app 조립) |
| 4 | 도구 안정 신원 형식 | A 능력 접두 유지 + 서버 구분자 별도 필드 / B 식별자에 서버 포함 | 형식은 루트 결정, 기존 승인·바인딩·저장 증거 호환은 별도 결정 | 루트 |
| 5 | 신원 인자 선언 의무 | A 민감 대상(자산·장치) 필수 / B 전부 선택 | A + 대상 종류별 경계는 루트 | 루트/통제 |
| 6 | 조리법 승인 방식 | A 선언+운영자 설정 ON일 때만 발동 / B 선언만으로 발동 | A (고정 명령은 운영자 손에) | 루트 |
| 7 | 재시도 조건 표현 | A 명명 조건 열거 / B 일반 조건식 | A | 도메인+루트 |
| 8 | 자격 Protocol 위치·하한 | A core 계약 + 제공자 callable / B pluggy 훅 | A. 새 제공자가 엄격도를 낮춰 선언하는 문제는 중앙 하한 검토 | 루트 |
| 9 | 해석+변화 동봉 | A 한 ObservationPackage / B 각자 등록 | A(의무 아님, 없으면 구조화 반영 없음 명시) | 루트/도메인 |
| 10 | 벤더 정책 소유자 | A 확장 선언 / B 앱 설정 | A (승인 자산이 engagement 메타데이터로 다니는 현행과 맞물림) | 루트 |
| 11 | 분야 어휘 소유자 | A 공통 계약 / B capability 소유 | A | 루트 |
| 12 | 읽기 계약 모양 | A 스키마 선언 / B 도구 식별자 선언 | A (같은 의미 다른 읽기 도구 교체 가능) | 루트 |
| 13 | 결속 실패 정책 | A 필수=시작 실패, 선택=격리+표시 / B 전부 경고 | A 혼합 | 루트 |
| 14 | 후속 대응 전달 | 1단계 공용 판독기 / 2단계 판정 계약 필드 승격 | 1→2 순차(2는 동결 계약 변경) | 루트 |
| 15 | 집행 신호 정형화 | A ControlContext 정식 필드 / B 별도 구조체 | 위치는 루트, 부재 의미(병렬 거절·속도 구경로)는 현행 유지 | 루트 |
| 16 | 표면 목록 선언 | A 제공자 선언 / B 현행 상수 | A. 키워드 충돌 우선순위는 루트 확정 | 루트 |
| 17 | 실행 스킬 범위 | A 참고 연결만 / B 실행 스킬까지 | 사용자 목표 확정 후 결정(현 단계는 A 기준 설계) | 루트·사용자 |

## 10. 기존 규칙 유지 확인

- 검증 계약 감사 A01~A10: 동작 어휘 거절, 후보 컴파일 검사, 계획 범위, 실행 직전 재검사, 선점, 범위·행동 규칙, 위험·승인·회계, 도구 관문, 제한 재시도, 특수 능력 실행 조건 — 본 이관안은 이 검사들을 옮기거나 지우지 않는다. 제공자 선언은 후보 적격성만 넓히고 정책·승인·범위 판정은 중앙에 남긴다.
- 원본 증거 E01~E08: 원본 보존, 기대·의미 해석, 최종 검토 기록, 가설 상태, 취약점 승격, 인증 비교, 원본 읽기 한계, 해석기 등록·실패 구분 — 해석 등록이 선언화돼도 미등록=빈 변경(차단 아님)과 실패 격리를 유지한다. 완료 검사·승격 조건은 손대지 않는다.
- 폐쇄 루프 계획 6절 경계: 완료 검증에 매핑 결과 필수화 금지, progress.py 원본 의미·버전·승인 복원 유지, 그래프 쓰기 통로 단일화, 새 감사 기록의 지문 제외 — 본 이관안과 충돌 없음.
- 감사 문서 10절 기각 주장(모든 새 MCP가 모바일 수정, 해석기=실행 가능 조건, 자리표시자 표 만능, 다중 시점 검사=중복 안전장치 등)을 재주장하지 않는다.

## 11. 실결함 / 정상 계약 / 운영 제약 구분

| 구분 | 항목 | 이유 |
|---|---|---|
| 실결함 | M01 복사본 2, M06 중앙 바인딩·조리법, M13 중앙 선택 사슬, M14 서버 미포함 신원, M15 닫힌 대상·인자 계약, M16 이중 파서, M02 등록 분산, M03 인라인 배선, M04 벤더 값 하드코딩, M05 등록 분산, M07 늦은 스킵, M18 변화 규칙 통로 부재, M27 어휘 중복, M28 읽기 도구 고정 | 확장마다 중앙 수정·중복 진실·배선 반복을 강제. 사용자 목표 직결 |
| 정상 계약(유지) | 어휘 형식·기본 거부, 스키마·대상 일치, 범위·승인, 원본 증거 연결, 해석 정확 일치·실패 격리, 정의·명세 일치 검사, 배포 digest·환경 대조, 조리법 외 명령 차단, 계획+직전 재검사 | 안전·검증 계약. 모듈화는 이 검사를 면제하지 않는다 |
| 운영 제약 | M08 재시도 혜택 제외(안전측), M16 동결 계약(필드 승격은 2단계), M17 부재 의미 이원(의도), M25 P2 고정 목록(실행 불능 아님), M04 비관리 서버 통과(현행 보존), M05 미등록 차단 아님 | 비용·설계 선택. 바꾸려면 별도 결정 |

## 12. 미해소·미검증

1. 도구 신원 형식 확정과 기존 식별자(승인 목록·바인딩·저장 증거) 마이그레이션 정책 — 루트 결정 전 무형식.
2. 자격 제공자의 중앙 하한(새 제공자가 page_read급 엄격도·자산 검사를 스스로 낮추는 문제) 필요 여부.
3. 집행 신호(비행 중 수)의 주 실행 경로 전달 전수 인벤토리 — 오케스트레이션 호출부 확인 필요.
4. 승인 APK 해시의 운영자 발급·회전 절차 — 코드에 없음(운영 문서 몫).
5. entry point 수집의 신뢰 경계(승인된 패키지 이름의 공급원·무결성 검사) — 루트 설계 확정 필요. 임의 설치 패키지 자동 발견은 금지 전제.
6. 실행 스킬 요구 범위 확정 전까지 코드 구현 보류.
7. 배포 역할·프로필 확대 시 정책·세션·바인딩 검증이 따라 넓어져야 하는 범위 미산정.
8. 보고 키워드 충돌 우선순위, 동봉 해석의 버전 대조 절차(재개 시) 미정.
9. 실행 재현 없음: 테스트 미실행, 다중 서버 충돌·정규화 충돌 미재현, 실제 외부 MCP·LLM 미호출. 소스 정적 확인만 했다.
10. 공통 계약 변경(어휘 소유, 판정 필드, Protocol 위치, 읽기 계약)은 전부 루트 조율 제안 상태다 — 본 문서의 어떤 항목도 구현된 것이 아니다.

## 13. 조사 한계와 산출물

- 조사 방식: 감사 문서 근거 링크를 실제 소스에서 재확인(심볼·행), 워커 A/B 독립 조사 후 오케스트레이터 교차 검토·정정. 전체 파일 통독이 아니라 담당 M 항목의 등록·소비·검사 경로 중심 판독이다.
- 소스 수정·테스트 실행·설치·Git 변경 없음. 산출물 파일만 생성했다.
- 워커 원본: docs/agent/modularization-20261004/modularization-runtime/worker-a-capability-mcp-findings.md, worker-b-roles-methodology-findings.md. 루트 평가자 메모(같은 디렉터리 evaluator-initial-notes.md)와 충돌 시 본 문서가 최신 루트 방향 반영본이다.
- 본 문서: docs/agent/modularization-20261004/modularization-runtime/extension-orchestrator-findings.md. 이후 구현은 루트 후속 지시 대기.
