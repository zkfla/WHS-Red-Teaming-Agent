# [Proposal] ActionProposal.capability / operation 채우는 규칙 정의 요청

- 제안: Control 팀 (ROE 담당) - 김도윤
- 대상 영역: **Naming Convention (PM 담당, instruction.md:137)**
- 상태: ✅ **확정** (PM, `origin/dev d4b6887` — `docs/contracts/actions.md` "Action Naming Convention")
- 관련: [PLAN.md](../PLAN.md) §3(ROE), `docs/contracts/actions.md`,
  Capability 팀 작업 (origin/dev `f6308bf`, PR #2)

---

## ✅ 확정된 결정 (PM)

A안 기반 + 수정으로 확정. 아래가 최종 규칙 (`docs/contracts/actions.md`):

- `capability` = namespace(`recon`/`web`/`mobile`/`analysis`), lowercase, operation 미포함.
  → `denied/allowed_capabilities`와 **exact match**.
- `operation` = tool과 독립적인 canonical action name, `lower_snake_case`.
  MCP `tool_name`과 같을 필요 없음.
- **Canonical Action Key = `<capability>.<operation>`** (예: `recon.port_scan`).
  → `denied/allowed_operations`는 이 **키와 exact match**. (우리 초안의 "operation=tool_name"에서 변경됨)
- **prefix/wildcard 매칭 없음.** 전체 차단은 `denied_capabilities: ["web"]`,
  개별 차단은 `denied_operations: ["web.file_upload"]`.

**Control 반영 완료:** `control/roe.py`가 canonical action key(`<capability>.<operation>`)로
operation을 exact match하도록 수정, prefix 매칭 미도입. 테스트 갱신·통과.

---

## 배경 — 두 가지 "이름"은 서로 다른 계층이다

1. **ToolId (Capability 팀)** — 실제 MCP 도구 식별자.
   Capability 팀이 `namespace.tool_name` 규칙으로 이미 정해 둠
   (`capability/adapters/mcp/naming.py`의 `build_mcp_tool_id`):

   | 어댑터 | `tool_namespace` | `capability_tags` |
   |---|---|---|
   | Recon | `recon` | `("recon",)` |
   | Web API | `web` | `("web", "api")` |
   | Mobile | `mobile` | `("mobile",)` |
   | Analysis | `analysis` | `("analysis",)` |

   → ToolId 예: `recon.port_scan` (소문자, 점 구분, 정규화 강제).

2. **ActionProposal.capability / operation (Agent 팀이 작성)** — 판단 **이전**
   단계에서 에이전트가 "무엇을 하려는지"를 선언하는 값. Control Plane이
   ROE/Scope로 판단하는 **대상**이다. 실제 ToolId는 ALLOW 이후 실행 단계에서
   붙는다 (`core.convert.build_tool_invocation(proposal, decision, *, tool_id=...)`).

**두 값은 단계도 의미도 다르다** — ToolId는 "무엇으로 실행하는가"(수단),
capability/operation은 "무엇을 하려는가"(의도).

## 현재 Contract

`ActionProposal.capability` / `operation`은 자유 문자열(`str`)이며,
**무엇을 어떤 형식으로 채우는지 규칙이 없다** (`docs/contracts/actions.md`).

## 문제

Control 팀은 ROE에서 `denied_capabilities` / `denied_operations` /
`allowed_*`를 문자열 **정확 매칭**으로 강제한다 (`control/roe.py`, 구현 완료).
그러나 에이전트가 `capability`/`operation`에 무엇을 넣을지 규칙이 없어서,
**ROE 금지/허용 목록에 무엇을 적어야 매칭되는지 알 수 없다.**

핵심 위험 — 팀 간 조용한 불일치:

- Control은 `capability="recon"` 가정 → 금지 목록 작성
- Agent는 `capability="recon.port_scan"` 로 작성
- → 문자열 불일치로 **ROE가 아무것도 못 막는데 에러도 없음.**

## 필요한 결정 (스키마 변경 아님, 명명 규칙 정의)

`ActionProposal.capability` / `operation`을 채우는 규칙, 그리고 그것이
Capability 팀의 `namespace.tool_name`과 어떻게 대응되는지를 확정·문서화.

### 후보 안

| 안 | capability | operation | ROE 작성 예 |
|---|---|---|---|
| **A (권장)** | 도구 namespace (`recon`, `web`) | 도구 tool_name (`port_scan`) | `denied_capabilities: ["web"]`, `denied_operations: ["port_scan"]` |
| B | 전체 ToolId (`recon.port_scan`) | 자유/보조 | `denied_capabilities: ["recon.port_scan"]` |
| C | `capability_tags` 중 하나 | tool_name | 태그 기반 매칭 |

- **A**를 권장: namespace 단위 금지(`web` 전체 차단)와 개별 동작 금지(`port_scan`)를
  둘 다 자연스럽게 표현 가능. Capability 팀의 `tool_namespace`/`tool_name` 분리와도 일치.
- A 채택 시 Control은 ROE에 **namespace 접두 매칭**(예: `web` → `web.*`)을 추가 구현.

## 영향을 받는 모듈

- **Agent** (`orchestration`): 요청서에 capability/operation을 채우는 주체 → 규칙 준수 필요.
- **Control** (`control/roe`): 규칙이 정해지면 ROE 목록 매칭이 신뢰 가능해짐. (제안 주체)
- **Capability** (`capability/adapters`): 이미 `namespace.tool_name` 사용 중 — 규칙의 기준점.
- **PM 문서**: `docs/contracts/actions.md`에 규칙 추가.

## 호환성 영향

- **스키마/코드 타입 변경 없음** (`capability`/`operation`은 `str` 유지) → 기존 테스트 불변.
- Capability 팀의 기존 `namespace.tool_name` 규칙과 충돌 없음(오히려 정렬).

## Control 팀이 독립적으로 진행하는 부분 (PM 결정 불필요)

- `ToolDescriptor.risk_level`(LOW~CRITICAL)을 ROE 축으로 활용 — "CRITICAL 도구 금지"처럼
  이름 나열 없이 위험도 기반 차단. (이미 계약에 존재하는 필드)
- namespace 접두 매칭 로직 — 규칙(A안) 확정 후 반영.

## 대안

1. 규칙 없이 자유 문자열 유지 → 거부(오탈자/불일치로 게이트 무력화).
2. Control 내부 가정으로만 사용 → 거부(Agent가 안 따르면 매칭 실패).
3. 본 제안(권장) → PM이 A안 기준 규칙을 문서화, 전 팀 공유.

## 요청

`ActionProposal.capability`/`operation` 작성 규칙(권장: **A안**)을 확정·문서화해 주시길
요청합니다. 확정되면 Control은 (1) ROE namespace 접두 매칭 추가, (2) 예제
engagement YAML에 표준 ROE 샘플 추가를 진행하겠습니다.
