> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# B 영역 공개 API/소비자 노트 (루트 조립용)

행번호는 공유 트리에서 이동하므로 파일·심볼 기준. B 소유 파일만 근거로 한다.

## (a) 범용 자격 레지스트리 소비 계약

### 타입 — `src/redteam/orchestration/eligibility.py` (루트 레지스트리는 이 파일만 import)

```python
class EligibilityProvider(Protocol):
    @property
    def provider_id(self) -> str: ...
    @property
    def applies_to(self) -> EligibilityScope: ...
    async def role_eligible(self, role_id: str, context: EligibilityContext) -> EligibilityDecision: ...
    async def tool_eligible(self, tool: Any, context: EligibilityContext) -> EligibilityDecision: ...
    async def asset_scope(self, context: EligibilityContext) -> tuple[str, ...]: ...
```

```python
@dataclass(frozen=True)
class EligibilityScope:
    capabilities: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    server_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str

@dataclass(frozen=True)
class EligibilityContext:
    graph: Any = None
    catalog: ToolCatalog | None = None
    execution_ready: bool = False
```

- 3개 메서드 전부 async (실시간 그래프 조회·계획/실행 사이 건강 변경 가능).
- 가시성만 판단하고 실행 허가를 대신하지 않는다.
- N1 패치 노트 import 갱신: `analysis_activation` 대신 `orchestration/eligibility` 에서 `EligibilityContext, EligibilityProvider` 등을 가져온다. 도메인 모듈 import 불필요.

### 내장 제공자 — `analysis_activation.py` (그대로 둠)

- `StaticAnalysisEligibilityProvider`, 식별자 `eligibility:static-analysis`.
- 적용 대상: capabilities=`("analysis",)`, operations=`("static_analysis",)`.
- 기존 함수 위임 (apk_ref 비어있지 않은 자산, analysis+static_analysis+HEALTHY+asset 대상).
- 호환 재노출: 기존 5개 이름(`ELIGIBILITY_PROVIDER_KIND, EligibilityContext, EligibilityDecision, EligibilityProvider, EligibilityScope`)은 `analysis_activation` 에서도 import 가능하다.

### 기여 형태

- kind `eligibility_provider`, contribution_id = provider_id, version `"1"`.
- factory 시그니처: `factory(ctx) -> 새 제공자` (문맥 인자 무시, 호출마다 새 객체).
- requires: 없음 `()`.
- 선언 수집: `static_analysis_eligibility_contribution()` / `builtin_extension_declaration()` (extension `builtin.analysis-eligibility`).

### 루트 레지스트리 → 엔진 4지점 최소 호출 (의사코드)

```python
providers: list[EligibilityProvider]  # 루트가 선언 수집해 보유 (예: RuntimeDeps 신규 의존성)
ctx = EligibilityContext(graph=service, catalog=catalog, execution_ready=...)
role_ok = all((await p.role_eligible(role_id, ctx)).eligible for p in providers)
tool_ok = all((await p.tool_eligible(tool, ctx)).eligible for p in providers)
scopes = {p.provider_id: await p.asset_scope(ctx) for p in providers}
```

- 4지점: 프롬프트 가시성(역할+도구), 참고 후보 수집, 행동 컴파일(승인), 실행 직전 재검사.
- 실행 직전 재검사는 생략 금지 (TOCTOU — 동일 규칙·다른 시점).
- 필수/선택: 선택 제약. 미등록이면 제약 없음(가시). 누락이 실행 차단이 아니다.

## (b) 모바일/도메인 조리법·조건 이관 소비 계약

```python
@dataclass(frozen=True)
class VendorPolicy:
    vendor_id: str
    managed_server_ids: frozenset[str]
    fallback_map: dict[str, str]  # 관리 서버 -> 주 피어 서버
    approval_ref: str = "mobsf_agent"
    version: str = "1"
```

- 내장값: `builtin-mobile`, 관리={`mobsf`, `analysis-local`, `mobile-local`}, 대체={`analysis-local`→`apktool`, `mobile-local`→`mobile-next`}.
- 기여: kind `vendor_policy`, contribution_id = vendor_id, version = 정책 버전, factory `(ctx) -> 정책값` (무시), requires 없음.
- 선언 수집: `mobile_vendor_policy_contributions()` / `builtin_extension_declaration()` (extension `builtin.mobile-vendors`).

### 루트가 호출할 함수·인자

```python
policies = [c.factory(ctx) for c in assembly.contributions("vendor_policy")]  # 비면 생략 가능
deps = wire_mobile_mcp(deps, engagement, vendor_policies=policies)
gate = MobileMCPEligibility(catalog, gateway, engagement, vendor_policies=policies)
```

- 새 키워드 인자 `vendor_policies: Iterable[VendorPolicy] | None = None` (생략 시 내장 기본값 — 기존 호출과 호환).
- call-site: `app/bootstrap.py` 모바일 배선 2곳 (심볼 `wire_mobile_mcp`). engine 직접 호출 없음.
- 필수/선택: 선택. 비어 있으면 내장 기본값, 시작 실패 조건 없음.

### 조건부 mobsf 관측 등록 경로

- `wire_mobile_mcp` 내부에서 mobsf 서버 도구 존재 시에만 `register_contribution(result_mappers, mobsf_observation_package())` 호출.
- 루트는 `analysis.mobsf_report_summary` 를 무조건 등록하면 안 된다 (조건부 — 선택).
- 승인 해시 3겹·건강 게이트·비관리 통과는 공용 로직 유지, 루트가 건드릴 것 없음.

## (c) 참고 자료 읽기 계약 소비

```python
@dataclass(frozen=True)
class MethodologyReadContract:  # capability 측 (제공)
    capability: str = "analysis"
    operation: str = "file_read"
    asset_binding: str = "asset_ref_const"
    encoding: str = "utf-8"
```

- 기여: kind `methodology_read_contract`, contribution_id `analysis.file_read:asset_ref_const`, factory `(ctx) -> 계약값`, requires 없음.
- orchestration 측 `ReferenceReadContract` / `ReferenceLifecycleRefs` 는 전부 기본값 (엔트리 생성 코드 무수정).
- `reference_entry_contribution(entry)`: kind `reference_bundle`, id = 참조 식별자, version = sha 앞 16자리(가안).

### 앱 조립 대조 순서 (기존 특정 도구 고정 대조 대체)

1. 전 엔트리 동일 `read_contract` 확인 (다르면 거부).
2. `capability`/`operation` 을 서빙하는 건강한 도구 탐색 (식별자 고정 없음).
3. `asset_binding` 규칙 검사 (`asset_ref_const` 면 기존과 동일한 input_schema `asset_ref.const == 번들 asset` 대조).
4. 실패 시 기존 오류로 시작 실패 (필수 — 기존 `methodology_workspace_read_tool_required` 의미 유지).

- call-site: `app/methodology.py` 의 `wire_reference_agent` 내 읽기 도구 대조부.
- 실행 제공자 필드 없음 (강제 금지 유지).

## (d) 지문 변화 키 소비

- 조회: `ResultMapperRegistry.meaningful_change_keys(tool_id)`, 전체 `all_meaningful_change_keys() -> dict[str, tuple[str, ...]]`.
- N4 최소 패치 요약: `attack_graph/observation.py` 의 `_planning_content(node, extra_keys=())` 에 extra 합성 + `build_observation(..., extra_content_keys=())` 전달. extra는 위 전체 조회 합성. 모르는 키 무시·휘발 제외 유지. `progress.py` 는 서명 변경 없이 `content_digests` 소비라 수정 불필요.
- 필수/선택: M18 효과 실현에 필요 (미적용 시 현행 동작 그대로 — 안전 기본값).

## (e) 소비자 call-site와 필수/선택

| 항목 | 파일·심볼 | 필수/선택 |
|---|---|---|
| 자격 제공자 목록 보유 | 루트 RuntimeDeps 신규 의존성 (예: `eligibility_providers`) | 선택 (없으면 제약 없음) |
| 엔진 4지점 호출 | `engine.py` 가시성/후보/컴파일/실행직전 (N1) | 선택 제약이나 재검사 생략 금지 |
| 벤더 정책 전달 | `app/bootstrap.py` — `wire_mobile_mcp` 2곳 | 선택 |
| 해석 기여 연결 | 루트 조립 — 아래 두 경로 중 택1 | 조건부는 (b) 경로만 |
| 읽기 계약 대조 | `app/methodology.py` — `wire_reference_agent` | 필수 (시작 실패 유지) |
| 지문 키 합성 | `attack_graph/observation.py` (N4) | M18 효과에 필요 |
| 역할 꾸러미 결속 검사 | 앱 조립 시작 전 — `check_role_package_bindings` (N6) | 필수 누락=시작 실패, 선택=격리+표시 |

## 해석 기여 두 소비 경로

### (a) factory만 사용 (키 기록 없음)

```python
callable_mapper = contribution.factory(ctx)  # ResultMapperFn (런타임 kind 계약)
registry.register(contribution_id, callable_mapper)  # contribution_id == tool id (단일 번들)
```

- 중복 거절·정확 일치는 기존 `register` 규칙 그대로.

### (b) 선언 조회 후 전체 등록 (지문 키까지 기록)

```python
decl = load_mapper_contribution(contribution_id)  # 또는 mapper_declarations_by_contribution_id()
register_contribution(registry, decl)  # 등록 + meaningful_change_keys 기록
```

- 어느 쪽이든 해석 부재가 도구 실행 차단이 아니라는 의미 유지 (`map_result` 빈 변경 반환).

## reporting

- 미편집. reporting 소비 변경 없음 (별도 워커 담당).
