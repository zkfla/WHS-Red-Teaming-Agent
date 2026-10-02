# Control Team 개발 계획 (Control Plane + Evidence)

> Control 팀(2인)이 무엇을, 어떻게 나눠서 만드는지 정리한 문서.
> 팀원은 이 문서 하나만 보면 자기 트랙을 시작할 수 있습니다.
> 브랜치: `dev-control` (base: `dev`)

---

## 0. 우리 팀이 맡은 것

| 모듈 | 역할 | 비유 |
|---|---|---|
| `src/redteam/control/` | Engagement Control Plane | 🛡️ 문지기 — "이 행동 해도 돼?" 판단 |
| `src/redteam/evidence/` | Evidence Store | 📝 기록원 — "무슨 일이 있었나" 저장 |

**중요 원칙:** 우리는 `core/contracts`, `core/ports`(공통 규격, Contract v0.1)를 통해서만 다른 팀과 연결한다. 공통 규격은 우리가 직접 못 바꾸고, 필요하면 PM에게 Issue로 요청한다.

---

## 1. 2인 분업

### 🅰️ Track A — Policy & Enforcement Pipeline (판단)
> "이 요청이 관문을 통과하는가"를 판단하는 로직

**담당 파일:** `control/service.py`(파이프라인), `scope.py`, `roe.py`, `policy.py`, `risk.py`, `severity.py`, `budget.py`, `rate_limit.py`

**통합 배선:** `app/bootstrap.py`

**작업 목록과 상태:**

| # | 작업 | 상태 |
|---|---|---|
| 1 | ROE 규칙 — 능력/작업 allow·deny(canonical action key), 시간대, 가용성, 동시 실행 제한 (`roe.py`) | ✅ 완료 |
| 2 | Scope — CIDR 대역 + 도메인 서픽스 + host 외 target 종류 (`scope.py`) | ✅ 완료 |
| 3 | Policy — 승인 게이트(capability / action key 매칭). 거절은 ROE 담당이라 Policy는 deny 하지 않음 (`policy.py`) | ✅ 완료 |
| 4 | Budget — 횟수 + 도구호출 + 경과시간 (`budget.py`) | ✅ 완료 |
| 5 | RateLimit — sliding window (`rate_limit.py`) | ✅ 완료 |
| 6 | Blocked Action Evidence 연결 (`service.py`, `bootstrap.py`) — `DENY`/`REQUIRE_APPROVAL`을 Track B `EvidenceService`로 기록, 동일 Evidence Store 공유 | ✅ 완료 |
| 7 | 거절 심각도 태그 (`severity.py`) | ✅ 완료 (소비는 Agent 팀, §3 참고) |
| 8 | **Risk 기반 승인** — Tool Registry `risk_level` 조회 (`risk.py`, `policy.py`) — §2.1 참고 | ✅ 완료 (임계값은 임시 기본값, §5 참고) |
| 9 | Action 유형 무관 게이트 통과 검증 (`test_gate_universality.py`) | ✅ 완료 |
| 10 | Finding → Evidence → Action → Invocation 추적 체인 검증 (PM 문제 4) | ⏳ 후속 |

### 🅱️ Track B — Governance, Audit & Evidence (기록·승인·증거)
> 판단이 내려진 "다음"과 "결과 저장"을 담당

**담당 파일:** `control/approval.py`, `control/audit.py`, `evidence/service.py`, `evidence/stores/*`와 해당 운영 CLI

**작업 목록과 상태:**

| # | 작업 | 상태 |
|---|---|---|
| 1 | Audit 영속 저장 및 정책 결정·CLI 승인/거절 결과 기록 (`audit.py`, `control/stores/filesystem.py`) | ✅ 완료 |
| 2 | Approval 승인·거절·만료 상태 전이와 사람 개입 CLI (`approval.py`, approvals CLI) | ✅ 완료 (만료 Audit 기록과 자동 실행은 후속) |
| 3 | Evidence 인덱스 조회, JSONL 줄 무결성 검증, Run 단위 retention (`evidence/stores/*`) | ✅ 완료 |
| 4 | `DENY`/`REQUIRE_APPROVAL` 차단 Action Evidence (`record_blocked_action`) | ✅ 완료 (재평가·재실행 중복 방지는 후속 TODO) |
| 5 | Track B 내부 `ArtifactBackend`와 파일 저장 — 독립 참조, 원본 SHA-256, 원자 공개, 경로·symlink 방어 | ✅ 완료 |
| 6 | 기존 `EvidenceStore` 포트 기반 Evidence ID·기대 문맥 연결 검증 | ✅ 완료 |
| 7 | Evidence retention 입력 보호 — 0·음수·비유한 기간의 삭제 전 거부 | ✅ 완료 |
| 8 | 실제 Tool 실행 → Artifact → Evidence 생성 및 Finding/Reporter 연결 | ⏳ PM의 공통 포트·전달 책임 결정 후 타 팀과 통합 |
| 9 | Audit → 실행 Tool·Invocation·생성 Evidence 연결 및 검증 | ⏳ 실행 후 연결 규격과 책임 계층 협의 필요 |

Scope DENY 기록 회귀 검증(2026-10-03, 기준 `dev-control` / `2dcdede`):

- 11개 요청·설정 사례의 PolicyDecision → Audit → 차단 Evidence 연결 검증 완료.
- `BLOCK_ACTION`과 `ABORT_RUN` 태그 보존, 파일 저장 후 재조회,
  Evidence 저장 실패 시 DENY·severity 유지 검증 완료.
- 관련 테스트 93개 통과.
- `ruff check src tests examples` 및 `mypy src/redteam`
  (192개 소스 파일) 통과.
- 운영 코드 수정 없음.

추가 전체 테스트는 작업자가 WSL의 프로젝트 전용 가상환경에서
실행한 결과를 반영했다.

- 실행 명령: `python -m pytest -q`
- 결과: `1213 passed, 4 skipped in 122.92s`
- skipped 4개의 사유는 미확인.
- 이번 문서 갱신에서는 검사를 재실행하지 않음.

위 검증은 판단과 기록 보존을 대상으로 한다.
실제 CLI·MCP 실행 및 Orchestration의 `severity:abort_run` 소비에 따른
즉시 Run 종료는 별도 통합 검증이 필요하다.

Raw Artifact와 Evidence 연결 검증의 상세 규약은
[`artifact-and-evidence-validation.md`](./artifact-and-evidence-validation.md)를 따른다.

승인·Audit와 Evidence 저장소의 동작·제약은
[`governance-and-evidence-store.md`](./governance-and-evidence-store.md),
차단 Action Evidence의 필드 규격은
[`blocked-action-evidence.md`](./blocked-action-evidence.md)를 따른다.


> ⚠️ **이벤트 발행은 우리 일이 아님!** `POLICY_EVALUATED/ACTION_APPROVED/ACTION_DENIED` 등은 이미 Agent 팀의 orchestration이 발행한다 (`orchestration/nodes/policy.py`). Control이 또 발행하면 이벤트가 중복돼 통합 테스트가 깨진다. → Control은 이벤트를 발행하지 않는다.

### 🤝 두 사람이 안 부딪히는 이유
이벤트 발행이 빠지면서 **`service.py`는 Track A 전용**이 됨. Track B는 audit/approval을 `__init__` 주입으로 갈아끼우고, evidence는 별도 폴더라 **두 사람이 같은 파일을 동시에 건드릴 일이 없다.**

### 🤝 Blocked Action Evidence 협업 지점

| 담당 | 작업 |
|---|---|
| Track B | `EvidenceService.record_blocked_action()` 구현, 테스트, 스펙 문서 관리 |
| Track A | `DefaultControlPlane`의 `DENY`·`REQUIRE_APPROVAL` 경로에 메서드 연결 |
| Track A | Evidence 기록 실패 시 로그를 남기고 기존 `PolicyDecision` 반환 |
| Track A | Bootstrap에서 Control과 Orchestration이 동일한 `EvidenceStore`를 사용하도록 구성 |

Track B는 `evidence/service.py`를 담당하고, Track A는 `control/service.py`와
`app/bootstrap.py`를 담당한다. 새 Event나 Core Contract는 추가하지 않는다.

---

## 2. 파이프라인 순서 (Control Plane)

```text
ActionProposal (에이전트의 요청서)
   │
   ▼  DefaultControlPlane.evaluate()
Scope → ROE → Policy → RateLimit → Budget → Approval
   │
   ▼
PolicyDecision  (ALLOW / DENY / REQUIRE_APPROVAL + 이유)
```

- **ALLOW만** ToolInvocation(실제 도구 실행 명령)으로 변환된다.
- 이건 `core/convert.build_tool_invocation()`이 기계적으로 강제한다 (ALLOW 아니면 예외). → **DENY난 요청은 물리적으로 공격 도구에 도달 불가.**

Control Plane 우회 방지는 세 겹으로 검증한다.

| 계층 | 테스트 | 보장 |
|---|---|---|
| 구조 | `test_control_plane_bypass` | Orchestration은 Control/Capability 구현을 import할 수 없다 |
| 기계 | `test_conversion` | non-ALLOW `PolicyDecision`은 `ToolInvocation`이 될 수 없다 |
| 동작 | `test_gate_universality` | auth / scanner / replay / active probe / mobile / 처음 보는 유형까지 모두 `evaluate()`를 거친다 |

### 2.1 Risk 기반 승인 (Policy 단계)

위험도가 높은 Action은 `require_approval_for` 목록에 없어도 사람 승인을 요구한다.
**위험도 값은 Control에 하드코딩하지 않고 Capability Tool Registry(`ToolDescriptor.risk_level`)를 단일 출처로 쓴다.**

```text
ActionProposal(capability, operation)
   │
   ▼  RegistryRiskResolver   (control/risk.py)
ToolGateway.list_tools_for_action()   ← read-only 조회, 실행 없음
   │
   ▼  후보 도구 중 최고 risk_level  (승인 후 어떤 후보가 선택될지 모르므로 보수적으로)
임계값 이상이면 REQUIRE_APPROVAL   (matched_rules: "risk:<level>")
```

- 등록된 도구가 없으면 risk 판단은 건너뛰고 기존 동작 그대로다.
- **조회가 예외를 던지면 사람에게 넘긴다 (manual escalation)** — 로그를 남기고 `REQUIRE_APPROVAL` (`matched_rules: "risk:unresolved"`). 엄밀히는 fail-closed(DENY와 동급)가 아니다: 사람이 승인하면 실행은 계속 가능하다. 위험도를 알 수 없는 Action이 *승인 없이* 통과하지 않게 하는 것이 목적이며, 게이트가 크래시하지도 않게 한다. 진짜 fail-closed(승인으로도 못 뚫는 DENY) 예시는 ROE `prohibited_techniques`가 설정됐을 때의 `UnsupportedRoePolicyError`다 (§4, `control/roe.py`).
- 이미 dev에 있는 `ToolGateway.list_tools_for_action`과 `RiskLevel`만 사용하므로 **Core Contract 변경은 없다.**
- 임계값은 코드의 임시 기본값(`DEFAULT_APPROVAL_RISK_THRESHOLD = HIGH`)이다. Engagement별 설정은 §5 참고.

정책 결정 이후의 기록 흐름은 다음과 같다.

```text
PolicyDecision 생성
   │
   ▼
Audit 기록
   │
   ▼
DENY / REQUIRE_APPROVAL이면 Evidence 기록 시도
   │
   ▼
PolicyDecision 반환
```

Evidence 기록은 정책 판단 이후의 부가 작업이다. 저장에 실패하더라도 이미
생성된 `PolicyDecision`의 verdict를 변경하거나 반환을 막지 않는다.

`ALLOW`된 Action의 실행 결과는 기존과 같이 Orchestration의 `verify_node`에서
Evidence로 기록한다.

---

## 3. 위반 잡았을 때 "멈춤"과 "그 다음" (핵심 설계)

**역할 분담:** 우리(Control)는 **"된다/안된다 + 이유"만 판단**한다. "그 다음 뭘 할지"(재시도/전환/중단)의 **실제 행동은 Agent 팀(orchestration)** 이 한다. 우리는 판단이 잘 전달되게 **verdict + 이유(+심각도)** 를 명확히 넘긴다.

### 종료/포기의 3단계

| 단계 | 뭘 멈추나 | 다음 행동 | 예시 (개인정보 조회 테스트 중) |
|---|---|---|---|
| **Action 차단** | 행동 하나 | 같은 목표, **다른 방법** 시도 | `/users/123/profile` 막힘 → URL 바꿔 재시도 |
| **REPLAN** | 목표 하나 | **다른 목표**로 전환 | 개인정보 포기 → **XSS 확인으로 전환** |
| **Run 종료** | 세션 전체 | **아무것도 안 함**, 사람 대기 | 심각 위반(DDoS 등) → 전부 중단 |

> "Run 종료"는 **이번 실행 1회를 멈추는 것**이지, 취약점 분석(Engagement) 전체를 영원히 끝내는 게 아니다. Engagement 종료는 사람이 결정한다.

계층: `Engagement(계약) > Run(실행 세션) > Loop(한 바퀴) > Action(도구 호출)`

### Track A 설계 포인트 — 심각도(severity)
DENY할 때 **왜 막혔는지에 따라 에이전트의 다음 행동이 달라져야** 한다:

| DENY 이유 | 에이전트가 해야 할 것 | 단계 |
|---|---|---|
| 이 URL만 범위 밖 | 다른 방법 시도 | Action 차단 |
| 이 목표(능력) 자체가 ROE 금지 | 목표 통째로 포기 → 전환 | REPLAN |
| 절대 금지 행위(DDoS 등) | 즉시 전체 중단 | Run 종료 |

**✅ 구현 완료 (방법 A — 규격 변경 없음):** `redteam.control.severity`
- `Severity` = `BLOCK_ACTION`(다른 행동) / `REPLAN_GOAL`(다른 목표) / `ABORT_RUN`(세션 종료)
- Control이 DENY 시 `PolicyDecision.matched_rules`에 `"severity:<level>"` 태그를 실어 보냄.
  Agent 라우터는 `severity_from_matched_rules(decision.matched_rules)`로 읽어 라우팅.
- 스테이지별 매핑: scope→BLOCK / roe(능력)→REPLAN / roe(작업)→BLOCK / roe(시간)→ABORT /
  policy→REPLAN / rate_limit→BLOCK / budget→ABORT.
- Scope의 잘못된 `denied_hosts`/`denied_domains` 설정은 예외적으로 `ABORT_RUN`이다.

**남은 것 (Control 범위 밖):**
- Agent 팀(orchestration `router`/`replan`)이 이 태그를 **실제 라우팅에 반영**해야 한다. 현재 `orchestration/`에는 `severity_from_matched_rules` 사용처가 없어서, 태그는 붙어 있지만 아직 소비되지 않는다.
- (선택) **방법 B**: `PolicyDecision.severity` 1급 필드 승격 → PM Issue. 지금은 태그 규약으로 충분.

---

## 4. 진행 현황 (2026-09-22 기준)

- 게이트 6단계(Scope → ROE → Policy → RateLimit → Budget → Approval)가 모두 실제 로직으로 동작한다.
- 차단된 시도(`DENY`/`REQUIRE_APPROVAL`)도 Evidence로 기록된다 (Track B `record_blocked_action` + Track A 배선).
- Risk 기반 승인은 Tool Registry의 `risk_level`을 읽어 동작한다 (§2.1).
- 검증: `pytest -q` 929 passed, 2 skipped, 2 xfailed; `ruff check src tests examples`·`mypy src/redteam` 통과.

**아직 열려 있는 것**
- 승인 임계값의 Engagement별 설정 (§5).
- Raw Evidence 공통 연결 — 공통 Artifact 포트, 저장 결과 Contract, ToolResult 전달 규격, 실제 Tool 실행 및 Finding/Reporter 연결 (§5). Track B 내부 `ArtifactBackend`는 완료.
- Audit → 실행 Tool·Invocation·생성 Evidence 연결 및 검증 (§1 Track B 작업 #9).
- ROE `prohibited_techniques`: 기법 분류 체계가 없어서, 설정되면 조용히 무시하지 않고 명시적으로 실패한다 (`UnsupportedRoePolicyError`).


---

## 5. PM Issue 후보 (Contract v0.1은 유지하고, 필요한 것만 Issue로 올린다)

| 항목 | 필요할 때 | 대안 (변경 없음) / 상태 |
|---|---|---|
| **`ApprovalPolicy`에 risk 승인 임계값 필드** | Engagement마다 "어느 risk부터 승인"을 다르게 하고 싶을 때 | 지금은 코드의 임시 global 기본값 `HIGH` (`control/risk.py:DEFAULT_APPROVAL_RISK_THRESHOLD`). **설계 논의 필요**: (1) 이 threshold를 `ApprovalPolicy` Contract 필드로 둘지, (2) 지금처럼 global 기본값으로 유지할지, (3) 그 둘을 병행해 Engagement가 override 안 하면 global 기본값을 쓰게 할지. 참고로만: 개념적으로 passive/active/intrusive 같은 등급을 논의하더라도 `RiskLevel`(LOW/MEDIUM/HIGH/CRITICAL) 하나를 단일 기준으로 유지하고 새 enum은 만들지 않는다. |
| **`ArtifactStore`/`ArtifactBackend` 공통 Port 추가** | Capability가 만든 Raw Artifact를 팀 경계 너머로 저장 요청할 표준 인터페이스가 필요할 때 | Track B 내부 `ArtifactBackend`(`redteam.evidence.artifacts`)는 완료했지만 Track B 내부 타입일 뿐 공통 Core Port가 아니다. Capability/Orchestration이 호출할 수 있는 공통 Port로 승격할지는 PM 결정 대기. |
| **`ToolResult`에 artifact reference/digest 전달 방식** | Tool 실행이 만든 Raw Artifact(HTTP 응답/HAR/stdout/screenshot)를 `ToolResult`를 통해 Control까지 흘려보내야 할 때 | 지금은 `ToolResult`에 그런 필드가 없다. `output` 전체를 Raw Evidence로 간주하지 않는다 — Raw Artifact는 Tool 실행에서 실제 관측한 원본 데이터만을 가리키는 더 좁은 개념. |
| **`EvidenceRecord` ↔ Artifact lineage 정의** | Finding/Reporter가 검증된 결과의 원본 Artifact까지 추적해야 할 때 | 지금은 `EvidenceRecord`에 Artifact 참조 필드가 없다. `redteam.evidence.validation.EvidenceLinkValidator`는 이미 있는 Evidence 문맥(engagement/run/action/invocation/hypothesis/target)만 검증하며, Artifact lineage는 다루지 않는다. |
| `PolicyVerdict`에 `ABORT`/`severity` | 심각도로 Run 종료 구분 | reasons/matched_rules 규약 사용 (권장) |
| `ControlPlane` 포트에 `resolve_approval` | 오케스트레이션이 승인 해소 | control 모듈 내부 + CLI로 해소 |
| `EvidenceStore` 포트에 조회 메서드 | hypothesis별 조회 등 | concrete store에만 추가 |
| `EvidenceRecord.digest` 원본 재검증 규격 | 원본 payload 보존·검증이 필요할 때 | 현재는 저장 JSONL 줄의 해시만 인덱스와 대조. 원본 보존 방식은 PM과 결정 |
| Evidence Target 저장 규격 | Finding의 Target 연결 검증 시 | 공통 Target dict만 비교하고 표시 문자열은 검증 불가 처리 |
| **`EngagementScope` 전용 포트/origin/경로 필드 승격** | Scope를 host 중심이 아니라 port·origin·path 단위로 1급 표현해야 할 때 (예: `allowed_ports`, origin/path 제한) | **결정됨(2026-10-02): 하지 않음.** 포트 제한은 기존 `allowed_hosts`/`denied_hosts`의 `host:port` 문자열 규약으로 지원하는 것을 **최종 방식으로 확정**했다 (`control/scope.py`, Contract 변경 0, 타 팀 영향 0). 전용 필드 승격은 지금 하지 않으며, 추후 host 중심 모델을 벗어나야 할 실제 요구가 생길 때에만 PM Issue로 재검토한다. 그때도 `scope.py`의 `_split_host_port`/`_resolve_target` 입력만 교체하면 되도록 로직을 분리해 둠. |

**해결됨:** `ActionProposal.risk_level` 추가 요청은 필요 없어졌다. dev에 추가된 read-only `ToolGateway.list_tools_for_action`으로 Registry의 `risk_level`을 Contract 변경 없이 읽을 수 있다 (§2.1).

---

## 6. 용어 사전

**판단 결과(verdict):**
- **ALLOW** = 허용 (도구 실행됨)
- **DENY** = 거절 (도구 실행 안 됨)
- **REQUIRE_APPROVAL** = 사람 승인 필요 (멈추고 대기)

**개념:**
- **ROE** = 교전 규칙 (언제/어떻게 공격해도 되는지)
- **Scope** = 범위 (어느 대상까지 공격 가능한지)
- **Engagement** = 이번 모의해킹 전체 설정(범위+규칙+예산)
- **Run** = 그 설정으로 한 번 돌리는 분석 세션
- **Action** = 도구 호출 하나
- **ActionProposal** = 에이전트가 내미는 요청서
- **PolicyDecision** = 문지기가 돌려주는 답변서
- **Control Plane** = 문지기 (우리 팀)
- **Evidence** = 증거 기록 / **Audit** = 판단 로그
- **Orchestration** = 에이전트 작업 흐름 관리 (Agent 팀)
- **REPLAN** = 거절 후 다른 행동을 새로 짜는 단계

---

## 7. 작업 방식

- **채팅 = 작업실(자유), 파일 = 확정본(공유).** 정해진 결론은 이 문서에 반영.
- `PLAN.md`에는 담당 범위와 진행 상태를 기록하고, 함수 시그니처·필드 매핑·검증 및 실패 처리 규칙은 기능별 스펙 문서에 기록한다.
- 공통 규격(`core/**`)은 직접 수정 금지 → PM Issue.
- 새 기능엔 테스트 작성. 작업 후 `pytest -q` 전체 통과 확인.
