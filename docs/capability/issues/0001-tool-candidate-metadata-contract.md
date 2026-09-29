# [Proposal] `ToolDescriptor` / `ToolGateway` Core Port를 후보 metadata까지 노출할지

- 제안: Capability 팀 (mobile/analysis MCP 담당)
- 대상 영역: **Core Contract (PM 담당)** — `core/contracts/tool.py`, `core/ports/tool_gateway.py`
- 상태: 초안 (PM 결정 대기)
- 관련: `redteam.capability.catalog.ToolCandidateMetadata`/`MCPToolGateway.list_candidate_metadata()`,
  `redteam.capability.mobile_analysis_manifest`, `deploy/mcp/README.md` "Mobile/Analysis capability"

---

## 배경

Capability는 이미 mobile-next/apktool/local fallback 각 후보의 다음 정보를
`MCPToolGateway.list_candidate_metadata()`로 갖고 있다:

- `tool_id`, `capability`, `operation`
- `required_arguments` / `optional_arguments` (MCP `input_schema`에서 파생)
- `target_type` (Capability manifest, 현재 값은 전부 `"asset"`)
- `risk_level`
- `output_schema` — 외부 MCP가 advertise하면 그대로, 안 하면 **추정하지 않고 `None`**

하지만 이 view는 `redteam.capability.catalog.ToolCandidateMetadata`(Capability 내부
Pydantic 모델)일 뿐, `core.contracts.tool.ToolDescriptor`(Core, frozen)에는
`required_arguments`/`optional_arguments`/`target_type`/`output_schema` 필드가 없다.
`core.ports.tool_gateway.ToolGateway` Protocol도 `list_candidate_metadata()`를 요구하지
않는다.

## 문제

Orchestration은 지금 `ToolGateway.list_tools_for_action()`(exists, Core Port)으로
`ToolDescriptor` 목록만 받는다 — `tool_id`/`capability_tags`/`risk_level`/
`health_status`/`input_schema`(raw MCP JSON Schema)뿐이다. Agent가 dynamic candidate
generation(예: "이 APK엔 어떤 인자가 필요한가"를 스스로 판단)을 하려면 지금은
`input_schema`를 직접 파싱해야 하고, `target_type`/구조화된 `output_schema`는 아예
못 받는다.

**Capability가 편하다고 Contract를 확장해 달라는 제안이 아니다.** 다음 질문에
Agent Team/PM이 먼저 답해야 확장 여부와 형태가 정해진다:

1. Agent의 dynamic candidate generation에 실제로 필요한 최소 metadata는 무엇인가
   (`required_arguments`만으로 충분한가, `output_schema`까지 필요한가)?
2. `ToolDescriptor`를 확장할지, 별도 `ToolMetadata`/`CapabilityDescriptor` 조회
   Port를 새로 만들지?
3. `target_type`/`output_schema`를 공통 Contract로 승격할지, 아니면 Capability
   내부에만 남기고 Agent는 여전히 `input_schema`만 볼지?
4. required/optional argument schema를 Agent에 얼마나 노출할지 — 전체 JSON Schema
   그대로인지, 이름 목록만인지?

## 후보 안

| 안 | 방식 | 장점 | 단점 |
|---|---|---|---|
| A | `ToolDescriptor`에 optional 필드 추가 (`target_type: str \| None`, `output_schema: dict \| None`) | 기존 `list_tools_for_action()` 호출부 변경 없음(추가 필드는 additive) | `ToolDescriptor`가 점점 무거워짐; `required/optional_arguments`는 여전히 `input_schema` 파싱 필요 |
| B | 별도 read-only Core Port(`ToolCatalog`류)를 신설해 `list_candidate_metadata()`류 메서드 제공 | `ToolDescriptor`는 얇게 유지, 필요한 팀만 새 Port 사용 | Port가 늘어남; Capability 내부 `ToolCandidateMetadata`와 형태가 겹침 |
| C | 지금처럼 Capability 내부에만 두고, 필요해지면 그때 확장 | Contract 변경 없음, 가장 보수적 | Agent가 dynamic candidate generation을 하려면 결국 막힘 |

Capability 팀은 특정 안을 권장하지 않는다 — Agent가 실제로 무엇을 소비해야
하는지가 먼저 정해져야 한다(위 질문 1).

## 영향받는 모듈

- **Core** (PM): `core/contracts/tool.py`(`ToolDescriptor`) 또는 `core/ports/tool_gateway.py`
  변경 여부 결정.
- **Agent/Orchestration**: dynamic candidate generation 로직이 이 metadata를
  실제로 소비하게 될 첫 팀.
- **Capability**: 이미 `list_candidate_metadata()`로 필요한 값을 전부 갖고 있음 —
  Contract가 정해지면 그대로 노출만 하면 됨, 추가 구현 불필요.

## 호환성 영향

안 A/B 모두 additive(기존 필드/메서드 제거 없음) — 기존 테스트에 영향 없음.
안 C는 아무 변경도 없음.

## 대안

1. 지금처럼 Capability 내부(`list_candidate_metadata()`)에만 두고 Agent는 계속
   `input_schema`를 직접 해석 — Contract 변경 없이 당장은 버틸 수 있음.
2. 본 제안 — Agent의 실제 소비 요구가 명확해지면 안 A 또는 B로 확정.

## 요청

이번 작업 범위는 아님 — Agent Team/PM이 "dynamic candidate generation에 실제
필요한 최소 metadata"를 먼저 정의해 주면, 그에 맞는 안(A/B/C)을 Capability가
후속 PR로 구현한다.

---

## 함께 적어두는 후속 Integration Issue 후보 (이번 작업 범위 아님, 다른 팀 소유)

Capability 단독으로 해결할 수 없어서 별도 Issue로만 남긴다 — 이번 PR에서 구현하지 않음.

### Agent → Control → Capability mobile/analysis E2E validation

현재 Capability 쪽은 아래를 전부 만족한다 (이번 작업에서 재확인함):

- canonical `capability`/`operation` 노출 (`docs/contracts/actions.md` 명명 규칙 준수)
- schema discovery + manifest 대조 검증 (`validate_configured_discovery`, 등록 단계에서 드리프트 시 실패)
- `ToolDescriptor` 등록 (`MCPToolRegistry`)
- Gateway invocation (`MCPToolGateway.invoke` — `analysis.get_manifest` 실제 호출 성공 확인)
- risk metadata (`ToolDescriptor.risk_level`, Control의 `RegistryRiskResolver`가 바로 읽을 수 있는 형태)
- target kind (manifest의 `target_type="asset"`, `AssetTarget` 스코프와 대응)
- deterministic tool selection (`orchestration.candidates.tool_selection.select_tool` 대상 — Capability가 후보를 여러 개 등록해도 선택은 Orchestration 책임)

하지만 **Agent가 실제 `ActionProposal`을 만들어 Control을 거쳐 이 Capability까지
도달하는 전체 경로는 아직 한 번도 실행된 적이 없다.** 예상 시나리오:

```text
APK target seed
  -> Agent ActionProposal (capability=analysis, operation=static_analysis)
  -> Control approval
  -> Capability Gateway
  -> apktool MCP
  -> ToolResult
  -> Evidence / Observation
```

Mobile:

```text
online emulator
  -> ActionProposal (capability=mobile, operation=app_launch)
  -> Control
  -> Capability Gateway
  -> mobile-next MCP
```

Agent/Orchestration 소유 — 이번 작업에서 구현하지 않음.

### Android emulator 기반 mobile runtime integration test

`mobile.app_launch`는 online emulator/device가 없어서 실제 실행이 아직
검증되지 않았다. `tests/integration/test_mobile_mcp.py`는 이미 opt-in
(`REDTEAM_TEST_REAL_ADB_SERIAL` 류 환경변수 없으면 skip) 구조를 갖추고 있어서,
CI/로컬에 실제 online 기기가 준비되면 그 환경변수만 채우면 된다 — 새 테스트
인프라가 필요한 게 아니라 **기기 준비 자체**가 남은 일이다.

### MobSF integration

이번 작업 범위에서 명시적으로 제외됨. 별도 결정 필요.

### External MCP output schema 전략

mobile-next/apktool 후보 tool 상당수는 outputSchema를 advertise하지 않는다
(`mobile_analysis_manifest.py`의 `output_schema=None`). 지금은 이를 절대
추정하지 않고 `None`으로 유지하는 정책만 지킨다. Tool 출력을 구조화해서
해석해야 하는 Result Mapper가 필요해지면(`orchestration.observations`류),
Agent 팀/Observation 계층과 별도로 논의해야 한다 — 이번 작업은 현재
available output metadata를 문서화하는 데까지만 한다
(`deploy/mcp/README.md` "Mobile/analysis discovery conformance").
