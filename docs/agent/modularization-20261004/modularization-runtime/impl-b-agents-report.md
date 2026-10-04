> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# B 구현 보고 — 전문 역할·c자격·도메인 조리법·mapper 기여·참고 자료 단일 제공기여

개요: P1-1 역할 꾸러미, P1-2 자격 제공자, P1-3 관측 기여, P1-4 참고 자료 자료화 완료. P2 실행 스킬은 코드 없이 최소 자료 구조 문서만 작성. 수정 파일 12개(신규 3개 포함, §7 후속 반영 포함). pytest 금지 준수, py_compile 12개 전부 통과.

---

## 1. 실제 변경 파일 (경로+요약+동작 보존 근거)

### 1.1 agents/packages.py (신규, M02/M07)

역할 1등록 단위인 자료와 내장 5역할 목록, 제공자 결속 검사 공개 API, 구성 루트 선언 변환을 담는다. 능력치는 정의 쪽에만 적고 명세 쪽은 파생·검증만 한다. 순환 참조 방지를 위해 전담 제공자는 팩토리 내부에서 지연 불러오기를 한다. 공용 플레이북 제공자는 기존처럼 단일 공유 인스턴스를 쓴다.

### 1.2 agents/registry.py (수정, M02)

default 조립 함수만 내장 꾸러미 목록 소비로 교체. 보존 근거: 등록 순서가 기존과 동일하고, 정의먼저·명세나중에 순서로 등록하므로 능력치 일치 검사가 양방향 동일하게 수행된다. 스케줄 순서와 리포팅 분리점이 그대로다.

### 1.3 agents/providers.py (수정, M02)

default 제공자 조립만 꾸러미 팩토리로 교체. 보존 근거: 구성이 기존과 동일하다.

### 1.4 agents/__init__.py (수정, M02/M07)

꾸러미 공개 API 노출만 추가. 기존 노출 제거 없음.

### 1.5 analysis_activation.py (수정, M03)

기존 함수는 한 줄도 안 고치고 뒤에 자격 규약과 내장 구현을 추가했다. 내장 구현은 기존 함수에 위임하므로 의미가 어긋날 수 없다. 계획 시점+실행 직전 이중 검사 유지는 엔진 패치 노트로 이관한다.

### 1.6 mobile_mcp_binding.py (수정, M04)

벤더 값을 자료 선언으로 분리하고 공용 검사 로직은 그대로 둔다. 기본 정책 값이 기존 하드코딩과 동일한 생성자 기본값이므로 기존 호출자는 의미가 변하지 않는다. 승인 해시 3겹 검사·건강 게이트·비관리 서버 통과가 그대로다. 조건부 해석 등록은 꾸러미 기여 호출로 흡수했다.

### 1.7 observations/packages.py (신규, M05/M18)

내장 관측 꾸러미 24개+조건부 1개. 의미 변화 키는 실제 해석 함수의 그래프 기록을 읽고 확인한 것만 넣었다. 흐발성은 제외했다.

### 1.8 observations/mapper.py (수정, M05/M18)

기여 자료·조합 경로 추가. 기존 등록·정확 일치·중복 거절·실패 격리·빈 변경 반환은 그대로다. 기본 등록부는 동일 해석함수·동일 식별자·동일 개수로 조립된다. 해석 부재가 실행 차단이 아니라는 의미는 주석과 구조로 유지한다.

### 1.9 observations/__init__.py (수정, M05/M18)

기여 API 노출만 추가. 기존 노출 제거 없음.

### 1.10 orchestration/methodology.py (수정, M27/M28)

읽기 계약·수명주기 참조 자료 추가만 했다. 새 필드 2개는 기본값이 있어 앱 조립의 기존 생성 코드가 그대로 동작한다. 실행 제공자 필드 없음.

### 1.11 capability/methodology.py (수정, M27/M28)

제공 측 읽기 계약 자료+기여 형태 추가. 분야 어휘 줄은 손대지 않았다(core 신설은 N5 패치 노트). 프로비전/검증 로직 변경 없음.

### 검증

py_compile 12개 파일 전부 통과(EXIT 0). pytest 금지 준수로 미실행.

---

## 2. 새 공개 API (시그니처·의미·소비자)

### 2.1 역할 꾸러미 (agents/packages.py)

- builtin_role_packages() -> tuple — 내장 5역할 번들. 소비자: 기본 등록부 조립, 구성 루트의 선언 수집.
- register_role_package(agents, providers, package) -> 번들 — 목적별 등록부 2곳에 등록(제공자, 정의, 명세 순). 소비자: 구성 루트가 선택된 확장의 기여를 연결할 때 호출.
- resolve_role_package(package, providers, *, known_provider_ids=None) -> RoleBindingReport — 필수/선택 참조 해석과 누락 목록 반환(생성 없음). 소비자: 시작 시 결속 검사.
- check_role_package_bindings(packages, providers, *, known_provider_ids=None) -> tuple — 전체 번들 결속 검사. 필수 누락=시작 실패, 선택 누락=격리+표시(N6).
- role_package_contributions(package) -> tuple 2건(agent_role + candidate_provider) — requires에 필수/선택 참조 표시. 소비자: 구성 루트.
- builtin_extension_declaration() -> ExtensionDeclaration (builtin.roles, version 1). 소비자: 구성 루트가 앱에서 등록할 때.
- 자료: RolePackage(역할식별자, 정의, 명세|분리점용 None, 제공자식별자, 팩토리, 필수/선택참조, 기본포함), RoleBindingReport.missing_required/missing_optional, AGENT_ROLE_KIND, CANDIDATE_PROVIDER_KIND.
- 팩토리 시그니처: provider_factory 는 문맥 없이 호출마다 새 객체. 구성 선언의 factory 에 감싸서 넣을 때는 문맥 인자를 무시한다.

### 2.2 자격 제공자 (analysis_activation.py)

- EligibilityProvider Protocol — provider_id, applies_to 속성 + async role_eligible, async tool_eligible, async asset_scope (셋 다 async: 범위는 실시간 그래프를 읽고 건강은 계획·실행 사이에 바뀖 수 있으므로). 가시성만 판단하고 실행 허가를 대신하지 않는다. 소비자: 중앙 엔진 4지점(N1).
- EligibilityScope(역량들, 동작들, 서버들) — 적용 대상 선언. 내장값: analysis/static_analysis.
- EligibilityDecision(적격, 사유) / EligibilityContext(그래프, 카탈로그, 실행준비) — 일회성 입력, 저장 안 함.
- StaticAnalysisEligibilityProvider (식별자 eligibility:static-analysis) — 기존 함수 위임 구현.
- static_analysis_eligibility_contribution() / builtin_extension_declaration() (builtin.analysis-eligibility). 소비자: 구성 루트.
- 위치 결정 필요: 아직 오케스트레이션 측에 있음. core 이동은 루트 결정 필요.

### 2.3 벤더 정책 (mobile_mcp_binding.py)

- VendorPolicy(벤더식별자, 관리서버집합, 대체짝, 승인참조=mobsf_agent, 버전) — 값만 담는다.
- builtin_mobile_vendor_policies() -> tuple — 소비자: 생성자/배선 기본값, 구성 루트의 선택 전달값 없을 때 대체.
- MobileMCPEligibility(..., *, vendor_policies=None) / wire_mobile_mcp(..., *, vendor_policies=None) — 키워드 추가만 있고 기존 위치 호출과 호환된다.
- mobile_vendor_policy_contributions() / builtin_extension_declaration() (builtin.mobile-vendors, kind=vendor_policy). 팩토리는 문맥을 무시하고 정책 값을 반환한다.

### 2.4 관측 기여 (observations/mapper.py, observations/packages.py)

- MapperContribution(도구식별자들, 해석참조, 해석버전, 의미키=(), 투영참조=None) — 참조 형식 dotted.module:attr (조립 시에만 해석). 키 목록과 투영 참조는 둘 중 최대 1개.
- resolve_contribution_mapper(contribution) — 불러오기 해석, 실패면 예외.
- register_contribution(registry, contribution) — 꾸러미를 목적별 등록부에 연결할 때 구성 루트가 호출할 공개 함수. 중복 거절은 기존과 동일 예외.
- mapper_contribution(contribution) (kind=result_mapper, factory는 해석 함수 callable 반환 — 후속 정합 반영) / `mapper_declarations_by_contribution_id()` / `load_mapper_contribution(id)` (선언 조회, 경로 b용) / builtin_extension_declaration() (builtin.observations, 조건부 포함 25건). contribution_id는 단일 도구 번들의 도구 식별자.
- 등록부 조회: meaningful_change_keys(tool_id), all_meaningful_change_keys() — 지문 소비자용(신규).
- builtin_observation_packages() (24건) / mobsf_observation_package() (조건부 1건).

### 2.5 참고 자료 (capability/methodology.py, orchestration/methodology.py)

- MethodologyReadContract + methodology_read_contract_contribution() (kind=methodology_read_contract). 소비자: 앱 조립의 읽기 도구 대조(N3).
- ReferenceReadContract / ReferenceLifecycleRefs (propose_ref 등, 전부 기본값) — 엔트리의 신규 선택 필드.
- reference_entry_contribution(entry) (kind=reference_bundle, 버전=sha 앞 16자리—가안, 루트 확정 필요).

### 2.6 kind 이름·식별자 규칙 초안

- agent_role / 역할 식별자 — 버전=정의 계약 버전.
- candidate_provider / 제공자 식별자 — 버전=같은 번들의 계약 버전.
- eligibility_provider / eligibility:도메인명 — 버전 1.
- vendor_policy / 벤더 식별자 — 버전=정책 버전 필드.
- result_mapper / 도구 식별자 (공유 시 정렬 조인) — 버전=해석 버전 필드.
- methodology_read_contract / 역할명.동작명:바인딩명 — 버전 1.
- reference_bundle / 참고 식별자 — 버전=sha 앞 16자리(가안).
- 팩토리 규칙: 문맥 인자 무시, 매 호출 새 객체(무상태 공유 예외는 문서화). requires는 필수 기본+선택 표시.

---

## 3. 기존 목적별 등록부 공개기여 호출 API 노트

- 순서 역할 보관자 register(spec) — 명세 등록+중복 및 능력치 일치 검사. 기여 경로 register_role_package() 가 정의 먼저·명세 나중에 호출하므로 동일 검사에서 실패한다. 직접 등록도 계속 가능.
- 순서 역할 보관자 register_definition(definition) — 정의 등록찡이다.
- 후보 제공자 등록부 register(provider_id, provider) / get(provider_id) — 빈 식별자 거부·중복 거절. 기여 경로는 팩토리 결과물 등록이다.
- 결과 해석 등록부 register(tool_id, mapper) — 정확 식별자, 중복 거절, 와일드카드 없음. 기여 경로 register_contribution() + 지문 조회 2종(신규).
- 실행 구성 등록부 ComponentRegistry.register (루트 소유) — 기본 팩토리의 등록부 3호출은 이미 꾸러미 기반이므로 수정 없이 연결된다. 선택 확장의 기여는 안의 공개 함수를 사용한다.

---

## 4. 루트 적용용 패치 노트 (직접 수정 금지 구역)

행번호는 작성 시점(공유 트리에서 타 워커 수정 진행 중)기준. 적용 전 재확인 필요.

### N1. engine.py — 자격 판단 4지점 제공자 호출로 교체 (M03)

N1-0 불러오기 (27행 부근, 후속 반영: 공통 타입은 eligibility.py에서 가져온다. 범용 자격 레지스트리는 analysis_activation이 아닌 orchestration/eligibility.py만 import하면 된다). 기존 심볼은 교체 확인 후 삭제하고 아래를 추가:

```python
from redteam.orchestration.eligibility import (
    EligibilityContext,
    EligibilityProvider,
)
from redteam.orchestration.analysis_activation import StaticAnalysisEligibilityProvider
```

N1-1 프롬프트 가시성 (583행 부근). 전:

```python
analysis_status = await analysis_activation(service, self.deps.tool_catalog)
visible_agents = [
    agent
    for agent in self.deps.agents.all()
    if analysis_role_visible(
        agent, analysis_status, execution_ready=bool(execution_options)
    )
]
```

후안: 역할 적격을 등록된 제공자에게 묻는다 (도메인 외 역할은 제공자가 참여 안 함으로 순수 유지):

```python
eligibility = EligibilityContext(
    graph=service, catalog=self.deps.tool_catalog, execution_ready=bool(execution_options)
)
visible_agents = [
    agent
    for agent in self.deps.agents.all()
    if all(
        (await provider.role_eligible(str(agent.agent_id), eligibility)).eligible
        for provider in self.deps.eligibility_providers
    )
]
```

도구 가시성 루프의 analysis_tool_visible 호출은 동일 문맥의 tool_eligible 결과와 AND로 교체한다. deps.eligibility_providers 는 루트가 RuntimeDeps에 선언 수집해 주입할 신규 의존성이다.

N1-2 참고 후보 (727행 부근). 전의 activation 계산+가시성 분기를 N1-1과 동일한 역할 적격 질의로 교체한다.

N1-3 행동 컴파일=승인 (1632행 부근 계산 + 1663행 부근 비교). 동작 비교는 제공자의 적용 대상 선언으로, 범위도구 비교는 asset_scope()·tool_eligible() 로 교체한다. 거부 사유는 결정의 reason 을 사용한다. 계획 시점 검사다.

N1-4 실행 직전 (2798행 부근). N1-3과 동일하게 교체하되 이 재검사는 삭제하지 않는다(건강이 사이에 바뀖 수 있으므로 동일 규칙이라도 시점이 다른 검사다).

### N2. app/bootstrap.py — 모바일 베선에 벤더 정책 전달 (M04)

call-site: 558행 (deps 전용) 및 677행 (engagement 동반). 후안: wire_mobile_mcp 호출에 vendor_policies=조립된목록 추가. 조립된 목록은 ExtensionAssembly.contributions("vendor_policy") 의 팩토리 호출 결과이다. 선택 제공이므로 비어 있으면 내장 기본값이 쓰인다. 시작 실패 조건 없음. runtime_components 조건부 해석 등록은 패치 불필요: 기본 3호출은 이미 꾸러미 기반이고 조건부 1건은 배선이 꾸러미로 등록한다. 조립부가 result_mapper 기여를 연결할 때는 register_contribution 을 호출한다(조건부는 건너뛴다).

### N3. app/methodology.py — 읽기 계약 기반 대조 (M28)

call-site: wire_reference_agent 내 읽기 도구 대조부. 전의 특정 식별자 고정 대조를 후에는 참고 자료의 읽기 계약으로 바꾼다: 엔트리 계약 동일성 확인 후 역량/동작에 맞는 건강한 도구 탐색 + asset 바인딩 규칙에 따른 검사. load 내 엔트리 생성부는 수정 불필요(신규 필드 기본값). 수명주기 참조는 자료로만 둔다.

### N4. attack_graph/observation.py + orchestration/progress.py — 선언 키 지문 반영 (M18, 최소안)

현재 선언 키는 등록부에만 기록되고 지문 계산은 아직 고정 목록만 본다. 관측 모듈 추가만으로 반영한다: _planning_content(node, extra_keys=()) 에 추가 키 합성 + build_observation(..., extra_content_keys=()) 전달. extra는 all_meaningful_change_keys() 합성이다. 모르는 키 무시·휘발성 제외·시각증거번호 금지 유지. progress.py 는 서명 변경 없이 content_digests 소비므로 수정 불필요하다 (단 extra는 역할 스코프 없이 반영되므로 역할별 스코프는 후속 과제).

### N5. core 신설 모듈 — 도메인 어휘 이중 정의 제거 (M27)

새 모듈 초안 (루트 작성): core/methodology_domains.py 에 MethodologyDomain 8값 정의. capability/methodology.py 전의 Domain 정의를 후에는 공용 불러오기로, orchestration/methodology.py 전의 인라인 8값을 후에는 tuple[MethodologyDomain, ...] 로. 분야와 실행 능력은 별제 축이므로 합치지 않는다.

### N6. 역할 꾸러미 결속 검사 연결 (M07)

call-site: 앱 조립부, 실행 시작 전. 전에는 없던 검사다. 후안: check_role_package_bindings 로 전체 확인하고 필수 누락은 시작 실패, 선택 누락은 격리+표시한다.

```python
from redteam.orchestration.agents.packages import check_role_package_bindings

reports = check_role_package_bindings(selected_packages, provider_registry)
missing = [item for report in reports for item in report.missing_required]
if missing:
    raise ExtensionAssemblyError(f"missing required providers: {missing}")
```

---

## 5. 미완·미검증·경합 위험

1. 실행 검증 없음. pytest 금지 + Ruff/mypy 미실행. 참조 문자열 25개와 지연 불러오기 순환 회피는 실행 시 확인 필요.
2. 공유 트리 경합. engine.py 행번호가 작업 중 밀렸다. 패치 노트 행번호는 작성 시점 기준이므로 적용 전 재확인 필요. 타 워커가 observations/recon_http.py 를 수정 중이므로 함수명 변경 시 내 문자열 참조도 함께 고치여야 한다.
3. M18 소비 미반영. 선언 키는 등록부에만 기록되고 지문 계산은 아직 고정 목록만 본다.
4. 의미 키 부분 미검증: 세션 목록/재실행/변형은 빈 목록이다(관측 컨텍스트·관계로 이미 반영되거나 빈 변경이므로).
5. reference_bundle 버전 규칙 미확정: sha 앞 16자리는 가안이다. R03 바인딩 규칙은 루트 결정 필요.
6. 자격 규약 위치는 아직 오케스트레이션 측이다. core 이동 여부는 루트 결정 필요.
7. 금지항목 준수: 새 범용 DI/스킬 엔진/전역 고정 enum/중앙 이름별 분기 추가 없음. pluggy/pydantic-settings 불러오기 없음.
8. 루트 확장 골격 재확인 완료: 3파일 변경 없음을 확인했다. 안의 변환 함수가 이 형식과 맞물린다.

---

## 6. 영향 M번호 · P2

- M02, M07: 코드 완료. N6 패치 노트 남음.
- M03: 코드 완료. N1 패치 노트(4지점) 남음.
- M04: 코드 완료. N2 패치 노트 남음.
- M05: 코드 완료(24+1건). 조립부 연결은 안의 함수로.
- M18: 선언 완료. N4 소비 패치 노트 남음.
- M27: N5 패치 노트만 (core 루트 소유).
- M28: 자료 완료. N3 패치 노트 남음.
- P2 실행 스킬: 코드 없음. 최소 자료 구조만 문서화: 스킬식별자, 필수역량/동작, 권한선언, 맥락기여, 증거계약. 전제조건은 역할·도구·맥락·증거 계약 전부 만족 시등록. 참고자료와 별도 경로. 직접 게이트웨이 우회 금지.

---

## 부록: 의미 변화 키 근거

- decode_apk, get_manifest, http_compare_anonymous: 빈 목록 (이미 공유 허용 목록의 키만 쓴므로).
- device_logs: mobile_runtime_crash_observed / crash_list: mobile_crash_observed / crash_detail: mobile_runtime_crash_observed (attrs 확인됨).
- mobsf_report_summary: mobsf_static_summary, mobsf_scan_ref (facts 확인됨).
- http_probe, http_request: security_headers_present (facts 확인됨).
- html/openapi_inventory: path_allowlisted (attrs 확인됨).
- dns_resolve: dns_resolved / tls_inspect: tls_verified / oidc_inventory: oidc_metadata_observed / port_scan: port_scan_complete (attrs 확인됨).
- http_compare_sessions, proxy_compare_sessions: session_comparison_observed (attrs 확인됨).
- http_security_posture: security_posture_observed (attrs 확인됨).
- browser_navigate: browser_navigated (attrs 확인됨).
- proxy_list_flows, proxy_get_flow, zap_capture_snapshot: proxy_observed (ENDPOINT attrs 확인됨).
- nuclei_response_check: passive_check_observed (attrs 확인됨).
- session_list, proxy_replay_flow, proxy_mutate_json: 빈 목록 (관측 컨텍스트·관계로 반영되거나 빈 변경이므로).

---

## 7. 후속 반영 (루트 요청 2건)

### 7.1 공통 자격 타입 분리
- 신규 `src/redteam/orchestration/eligibility.py`: 공통 타입 5종을 analysis_activation에서 그대로 이동. 동작·이름 변경 없음.
- `analysis_activation.py`는 해당 모듈에서 가져와 재노출(기존 이름으로 import 가능)하고, 내장 정적 분석 제공자 구현·기여 함수는 그대로 둔다. 기존 도메인 함수는 한 줄도 안 고쳤다.
- 루트 범용 자격 레지스트리는 `orchestration/eligibility.py`만 import하면 된다 (N1-0 갱신됨).

### 7.2 해석 기여 factory 정합
- `mapper_contribution()`의 factory는 이제 선언 객체가 아니라 `resolve_contribution_mapper()` 결과 callable을 반환한다 (런타임 kind 계약과 일치).
- contribution_id는 현재 전부 단일 도구 번들이므로 도구 식별자 (다중 도구 번들이 생기면 정렬 조인 — 한 곳에 규칙 집중).
- 공개 조회 추가: `mapper_declarations_by_contribution_id(packages=None)` / `load_mapper_contribution(id)` (모르는 id는 ValueError).
- 두 소비 경로: (a) factory만 사용 — `register(tool_id, factory(ctx))`, 키 기록 없음, 중복/정확 일치 규칙 유지. (b) 선언 조회 — `register_contribution(registry, decl)`, 지문 키까지 기록. 어느 쪽이든 해석 부재는 실행 차단이 아니다.
- reporting.py 미편집 (별도 워커 담당, 소비 언급 없음).

### 검증

py_compile 신규·수정 4파일(eligibility, analysis_activation, mapper, observations/__init__) 통과. pytest 금지 준수로 미실행. 상세 소비자 노트는 `impl-b-core-api-notes.md`에 별도 공유.
