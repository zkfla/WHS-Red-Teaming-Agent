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

**담당 파일:** `control/service.py`(파이프라인), `scope.py`, `roe.py`, `policy.py`, `budget.py`, `rate_limit.py`

**통합 배선:** `app/bootstrap.py`

**작업 순서 (쉬운 것 → 어려운 것):**
1. **ROE 규칙 채우기** `roe.py` ← 시작점 (뼈대는 이미 있음)
2. Scope 업그레이드 `scope.py` — 글자 비교 → 진짜 IP 대역(CIDR) 계산 + 도메인 서픽스 + 전 target 종류
3. Policy 강화 `policy.py` — "무조건 거절" deny 규칙 추가
4. Budget 세분화 `budget.py` — 횟수 + 시간 + 도구호출 제한
5. RateLimit 개선 `rate_limit.py` — sliding window
6. Blocked Action Evidence 연결 `control/service.py`, `app/bootstrap.py`
   — `DENY`/`REQUIRE_APPROVAL` 결정 시 Track B의 EvidenceService 호출
   — Control과 Orchestration이 동일한 Evidence Store를 사용하도록 배선

### 🅱️ Track B — Governance, Audit & Evidence (기록·승인·증거)
> 판단이 내려진 "다음"과 "결과 저장"을 담당

**담당 파일:** `control/approval.py`, `control/audit.py`, `evidence/service.py`, `evidence/stores/*`와 해당 운영 CLI

**작업:**
- **Audit(감사기록)** `audit.py` — 지금 메모리에만 있음 → 파일 등 durable 저장
- **Approval(승인)** `approval.py` — 지금 요청만 됨 → 승인/거절/만료 + 사람 개입(CLI) 경로
- **Evidence 저장소** `evidence/stores/*` — 조회 빠르게(인덱스/DB), digest 검증, 보관정책(retention)
- **EvidenceService** — `DENY`/`REQUIRE_APPROVAL`로 실행되지 않은 Action도 Evidence로 저장
  - `record_blocked_action(proposal, decision)` 구현 및 단위 테스트
  - 재평가·재실행 시 중복 Evidence 방지는 후속 TODO

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

**표현 방법 2가지:**
- **방법 A (규격 변경 없음, 권장 시작점):** `PolicyDecision.reasons`/`matched_rules`에 심각도 표시(예: `"roe:hard_violation"`) → Agent 라우터가 보고 판단.
- **방법 B (PM Issue 필요):** `PolicyVerdict`에 `ABORT` 추가 또는 `severity` 필드 추가. 더 깔끔하지만 공통 규격 변경.

지금 스켈레톤 상태: DENY/REQUIRE_APPROVAL 모두 REPLAN → replan_node가 한 바퀴 후 무조건 종료 (아직 3단계 구분 없음).

---

## 4. 이미 완료 (M0 — 뼈대/seam)

- `control/roe.py` 생성: `check_roe(action, engagement) -> (bool, list[str])` (현재 pass-through 껍데기).
- `DefaultControlPlane.evaluate()`에서 Scope 다음 · Policy 앞에 ROE 단계로 연결 (DENY 시 `matched_rules=["roe"]`).
- `tests/unit/test_roe.py` 추가 (4개, DefaultControlPlane 파이프라인 첫 테스트 포함).
- M0 당시 전체 61 tests pass, ruff/mypy clean, 기존 동작 불변.


---

## 5. PM Issue 후보 (지금은 전부 "대안=변경없음"으로 진행 가능, Contract v0.1 유지)

| 항목 | 필요할 때 | 대안 (변경 없음) |
|---|---|---|
| `PolicyVerdict`에 `ABORT`/`severity` | 심각도로 Run 종료 구분 | reasons/matched_rules 규약 사용 (권장) |
| `ControlPlane` 포트에 `resolve_approval` | 오케스트레이션이 승인 해소 | control 모듈 내부 + CLI로 해소 |
| `ActionProposal.risk_level` | risk 기반 정책 | Config 매핑으로 risk 유도 (권장) |
| `EvidenceStore` 포트에 조회 메서드 | hypothesis별 조회 등 | concrete store에만 추가 |
| `EvidenceRecord.digest` 원본 재검증 규격 | 원본 payload 보존·검증이 필요할 때 | 현재는 저장 JSONL 줄의 해시만 인덱스와 대조. 원본 보존 방식은 PM과 결정 |

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
