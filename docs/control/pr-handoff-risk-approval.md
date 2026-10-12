# [PR] Control: Risk 기반 승인 + Control Plane 우회 방지 검증

- 작성: Control 팀 (Track A) — 김도윤
- 브랜치: `dev-control` → `dev`
- 기준 커밋: `7044a36` (dev 최신 통합본) 위에 커밋 3개
- 대응 항목: PM 피드백 **문제 2**(모든 Action은 Control Plane 통과), **문제 3**(Risk/Approval 분류)

---

## 1. 한 줄 요약

**Tool Registry의 `risk_level`을 Control Plane이 읽어서, 위험도가 높은 Action은 사람 승인을 요구하도록 했습니다.**
위험도 값을 Control에 하드코딩하지 않고 Capability Registry를 단일 출처로 사용합니다.
Core Contract 변경은 없습니다.

## 2. 커밋 (3개)

| 커밋 | 내용 |
|---|---|
| `ae41895` | Registry `risk_level` 기반 승인 (`RegistryRiskResolver`, Policy 연동, bootstrap 배선) |
| `a76a7ab` | 위험도 조회 실패 시 fail-closed (승인 요구) |
| `c841d09` | 모든 Action 유형이 게이트를 거친다는 동작 테스트 |

## 3. 변경 파일

| 파일 | 변경 |
|---|---|
| `src/redteam/control/risk.py` (신규) | `RiskResolver` Protocol, `RegistryRiskResolver`, risk 비교 유틸, 기본 임계값 |
| `src/redteam/control/policy.py` | `evaluate_policy`에 `risk`/`risk_threshold` 추가 → 임계값 이상이면 `REQUIRE_APPROVAL` |
| `src/redteam/control/service.py` | `risk_resolver` 선택 주입, `_resolve_risk()`(fail-closed) |
| `src/redteam/app/bootstrap.py` | `RegistryRiskResolver(tool_gateway)` 배선 (`build_runtime_deps`, `build_application`) |
| `tests/unit/test_risk.py` (신규) | 11개 |
| `tests/unit/test_gate_universality.py` (신규) | 2개 |

## 4. 문제 3 — Risk / Approval 분류

### 4.1 요구사항 (PM)
Tool 종류가 늘어나면 Passive/Active/Intrusive에 따라 승인 정책이 달라져야 한다.
Tool Registry의 operation/risk metadata를 Control Plane이 해석할 수 있는지 점검하고, Contract 변경이 필요하면 Issue로 올린다.

### 4.2 점검 결과
- 기존에는 `risk_level`이 `ToolDescriptor`(도구 선택 이후)에만 있어서, 판단 시점의 Control Plane이 볼 수 없었습니다.
- 이후 dev에 추가된 **`ToolGateway.list_tools_for_action(capability, operation)`** (read-only)를 통하면 **Contract 변경 없이** 해당 Action에 바인딩된 도구들의 `risk_level`을 읽을 수 있습니다.

### 4.3 구현
```text
ActionProposal(capability, operation)
   → Policy 단계
   → ToolGateway.list_tools_for_action()  # read-only 조회, 실행 없음
   → 후보 도구 중 최고 risk_level
   → 임계값 이상이면 REQUIRE_APPROVAL   (matched_rules: "risk:<level>")
```

- 후보 도구 중 **가장 높은 risk**를 사용합니다. Action 승인 후 Orchestration이 어떤 후보를 선택할지 모르기 때문에 보수적으로 잡았습니다.
- 등록된 도구가 없으면(`risk=None`) 기존 동작 그대로입니다.
- 기존 `require_approval_for` 규칙은 그대로 동작하고, risk 기준은 그 위에 추가됩니다.
- Control이 하는 일은 read-only 메타데이터 조회뿐이며 Tool 실행 경로는 그대로 `Agent → Control → Capability`입니다.

### 4.4 조회 실패 처리 (fail-closed)
Registry 조회가 예외를 던지면 게이트 전체가 죽거나, 위험한 Action이 승인 없이 통과할 수 있습니다.
그래서 조회 실패 시 **로그를 남기고 `REQUIRE_APPROVAL`** 로 처리합니다 (`matched_rules: "risk:unresolved"`).
"도구 없음"(정상)과 "조회 실패"(오류)는 구분합니다.

## 5. 문제 2 — 모든 Action은 Control Plane 통과

기존에 있던 두 가지 보장에 동작 검증을 추가했습니다.

| 계층 | 테스트 | 보장 |
|---|---|---|
| 구조 | `test_control_plane_bypass` | Orchestration은 Control/Capability 구현 import 불가 |
| 기계 | `test_conversion` | non-ALLOW `PolicyDecision`은 `ToolInvocation`으로 변환 불가 |
| **동작 (신규)** | `test_gate_universality` | 다양한 Action 유형이 모두 `evaluate()`를 거쳐 판정을 받음 |

신규 테스트는 auth attempt / scanner / replay / active probe / mobile 유형과, 등록되지 않은 **처음 보는 capability·operation**까지 게이트가 균일하게 평가하는지 확인합니다. 유형별 예외 경로나 default-allow 우회가 없다는 근거입니다.

## 6. 검증

| 항목 | 결과 |
|---|---|
| `pytest -q` | **910 passed**, 2 skipped, 2 xfailed |
| `ruff check src tests` | 통과 |
| `mypy src/redteam` | 통과 (176 files) |
| 신규 테스트 | 13개 (risk 11, universality 2) |

## 7. Contract 변경 여부

**없음.** `core/contracts/**`, `core/ports/**`, `core/ids.py`, `core/enums.py`는 수정하지 않았습니다.
이미 존재하는 `ToolGateway.list_tools_for_action`, `ToolDescriptor.risk_level`, `RiskLevel`만 사용합니다.

## 8. 결정이 필요한 사항

### 8.1 승인 임계값 (PM 논의 필요)
현재 임계값은 코드의 **임시 기본값 `HIGH`** (`DEFAULT_APPROVAL_RISK_THRESHOLD`)입니다.
Engagement마다 다르게 설정하려면 `ApprovalPolicy`에 필드가 필요하고, 이는 Contract 변경이라 Issue로 올리겠습니다.
논의할 점:
- 임계값을 Engagement 설정으로 둘지, 전역 정책으로 둘지
- Passive/Active/Intrusive 3단계 분류를 기존 `RiskLevel`(LOW/MEDIUM/HIGH/CRITICAL)에 어떻게 매핑할지
- 같은 operation에 후보 도구가 여러 개일 때 "최고 risk 기준"이 적절한지

### 8.2 Raw Evidence / ArtifactStore (문제 1 관련 제안)
Control/Evidence 쪽 의견입니다.
- Raw Evidence는 `ToolResult` 전체가 아니라 **실제 관측 원본**(HTTP 응답, HAR, stdout, screenshot 등)으로 정의하고, `ToolResult`는 상태·요약·참조를 담는 envelope로 유지하는 방향을 제안합니다.
- 원본은 별도 저장소에 두고 `EvidenceRecord.artifact_ref` + `digest`로 연결합니다.
- 저장 책임은 원본이 생성되는 **Capability**가 맡고, `ToolResult`에는 참조와 digest만 실어 반환하는 방식을 제안합니다. 대용량 원본이 상위 계층을 통과하지 않아도 됩니다.
- 이를 위해 (a) `ArtifactStore` 포트, (b) `ToolResult`의 artifact 참조 필드가 필요해 Contract 변경 Issue가 필요합니다.

## 9. 이번 PR에 포함하지 않은 것

- **문제 4 (Audit 추적 체인 검증)**: Finding → Evidence → Action → Invocation → Hypothesis 조인 복원 테스트는 후속 작업입니다.
- 심각도 태그(`severity:*`)의 실제 라우팅은 Orchestration 담당이라 이번 범위 밖입니다.

## 10. 리뷰 요청

- `control/risk.py` 인터페이스(`RiskResolver`)와 fail-closed 정책이 적절한지
- 임계값 설계 방향 (8.1)
- Raw Evidence 저장 책임 분리 제안 (8.2)
