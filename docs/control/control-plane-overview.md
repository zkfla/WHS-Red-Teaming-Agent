# Engagement Control Plane — 동작 개요 & 건의사항

> Control 팀(Track A) 문지기(Control Plane)가 무엇을, 어떤 순서로 판단하는지
> 정리한 문서. 앞부분은 **동작 이해용 설명**, 뒷부분은 **PM·Agent 팀 건의사항**.
> 관련: [`PLAN.md`](./PLAN.md), `docs/contracts/actions.md`,
> [`blocked-action-evidence.md`](./blocked-action-evidence.md)

---

## 1. 한 줄 요약

**문지기.** 에이전트의 모든 공격 행동은 실행 전에 이 관문을 **반드시** 통과해야 한다.
에이전트는 도구를 직접 호출하지 못한다.

---

## 2. 전체 흐름

```text
에이전트: "이거 해도 돼요?"  (요청서 = ActionProposal)
      │
      ▼
문지기(Control Plane): 판단  →  ALLOW / DENY / REQUIRE_APPROVAL
      │
      ▼ (ALLOW일 때만)
오케스트레이션이 허가받은 요청을 실제 도구 실행 명령(ToolInvocation)으로 변환해 실행
      │
      ▼
도구가 대상(Target)에 실행됨
```

**핵심 보장:** ALLOW가 아닌 결정은 실행 명령으로 변환 자체가 안 된다
(`core.convert.build_tool_invocation`이 기계적으로 막음). 즉 **거절된 요청은
물리적으로 도구에 도달할 수 없다.** 프롬프트가 아니라 구조로 강제한다.

---

## 3. 판단 파이프라인 (6단계, 중간에 걸리면 즉시 중단)

항상 6단계를 다 도는 게 아니라, **순서대로 가다가 거절되면 그 자리에서 멈춘다**
(early-exit).

```text
1 Scope ─통과→ 2 ROE ─통과→ 3 Policy ─통과→ 4 RateLimit ─통과→ 5 Budget ─통과→ 6 Approval → 결과
   │             │             │                │                │
  거절          거절          거절             거절             거절
   ▼
  즉시 DENY (뒤 단계는 실행 안 함)
```

- Scope에서 걸리면 → 1단계만 돌고 DENY, 2~6은 보지 않음
- 전부 통과하면 → ALLOW
- 6 Approval은 **조건부**: 3 Policy가 "승인 필요"라고 할 때만 작동

---

## 4. 각 단계 상세

### 1️⃣ Scope — "어디를 공격해도 되나"
- **뭘 보나:** 요청서의 대상(host / IP / 도메인)
- **통과 조건:** 허용 목록(`allowed_hosts` / `allowed_domains` / `allowed_networks`)에
  있고, 거절 목록엔 없어야 함. 허용 목록이 아예 없으면 **기본 거절**(안전)
- **판단 방식:** IP는 CIDR 대역 계산, 도메인은 서픽스 매칭
  (`api.example.com`은 `example.com`에 포함, `notexample.com`은 아님)
- **예시:** 대상 `10.1.2.3` + 허용 대역 `10.0.0.0/8` → ✅ / `evil.com` → ❌

### 2️⃣ ROE — "어떻게·언제 공격해도 되나"
- **뭘 보나:** 능력(`recon`/`web`/`mobile`/`analysis`), 작업, 현재 시각
- **통과 조건:**
  - 능력이 `denied_capabilities`에 없어야 함 (있으면 목표 자체가 금지)
  - 작업이 `denied_operations`에 없어야 함 — **canonical action key
    `<capability>.<operation>`** 기준 (예: `recon.port_scan`)
  - 허용 목록이 설정돼 있으면 거기 포함돼야 함
  - 허용 시간대(`allowed_time_windows`, `start_at`/`end_at`) 안이어야 함
- **예시:** `recon.port_scan`이 금지목록에 없고 지금 09~18시 → ✅ / `impact.ddos` 금지 → ❌

### 3️⃣ Policy — "사람 승인이 필요한 행동인가"
- **뭘 보나:** `require_approval_for` 목록 (능력 또는 canonical action key로 매칭)
- **결과:** 통과(ALLOW) 또는 "승인 필요(REQUIRE_APPROVAL)". **여기선 거절하지 않음**
  (거절은 ROE 담당, Policy는 승인 게이트 전담)
- **예시:** `mobile` 능력이 승인 목록에 있음 → "승인 필요"

### 4️⃣ RateLimit — "너무 빨리 하는 건 아닌가"
- **뭘 보나:** 최근 60초 동안 실행한 횟수 (밀리는 창 = sliding window)
- **통과 조건:** 분당 한도(`max_actions_per_minute`) 미만
- **예시:** 분당 30 한도, 최근 60초에 이미 30개 → ❌

### 5️⃣ Budget — "전체 예산을 넘었나"
- **뭘 보나:** 지금까지 쓴 횟수 / 도구호출 수 / 경과 시간
- **통과 조건:** `max_actions` · `max_tool_calls` · `max_duration_seconds`를
  모두 넘지 않아야 함 (더 빡센 쪽이 적용)
- **예시:** 최대 5회인데 이미 5회 함 → ❌

### 6️⃣ Approval — "사람 확인 받기" (3번이 승인 필요라 했을 때만)
- **하는 일:** 승인 요청 생성 → "승인대기" + 승인ID 발급 → 사람이 CLI로 승인/거절
- **예외:** `auto_approve`가 켜져 있으면 자동으로 ALLOW

---

## 5. 결과: 판정(verdict) 3가지

| 판정 | 뜻 |
|---|---|
| ✅ **ALLOW** | 허용 → 실행됨 |
| 🚫 **DENY** | 거절 → 실행 안 됨 |
| ⏸️ **REQUIRE_APPROVAL** | 사람 승인 대기 |

---

## 6. 심각도(severity) — DENY에 붙는 별도 꼬리표, 3단계

DENY는 "왜 막혔는지"에 따라 에이전트가 다르게 반응해야 한다. 그래서 DENY 결정에
심각도 태그를 함께 실어 보낸다. (판정과 별개의 정보)

| 심각도 | 에이전트가 해야 할 것 |
|---|---|
| 🟡 **BLOCK_ACTION** | 이 행동만 막힘 → 다른 방법 시도 |
| 🟠 **REPLAN_GOAL** | 이 목표 자체가 안 됨 → 다른 목표로 전환 |
| 🔴 **ABORT_RUN** | 더 못 감 → 세션 종료 |

**단계별 매핑:**

| 거절 단계 | 심각도 |
|---|---|
| Scope (범위 밖) | 🟡 BLOCK |
| ROE - 능력 금지 | 🟠 REPLAN |
| ROE - 작업 금지 | 🟡 BLOCK |
| ROE - 시간대 밖 | 🔴 ABORT |
| RateLimit 초과 | 🟡 BLOCK |
| Budget 소진 | 🔴 ABORT |

전달 방식: 심각도는 `PolicyDecision.matched_rules`에 `"severity:<level>"` 태그로
실린다 (`PolicyDecision`에 severity 필드가 없어 규격 변경 없이 처리 —
`redteam.control.severity`). 현재는 **DENY에만** 붙는다.

---

## 7. 차단된 시도도 증거로 남는다

DENY / REQUIRE_APPROVAL은 실행되지 않으므로 실행 결과 Evidence가 없다. 그래서
Control Plane이 이 시도들을 **`source_type=SYSTEM`** Evidence로 별도 기록한다
(best-effort — 기록 실패가 판단 결과를 바꾸지 않음). 덕분에 최종 리포트에
"시도했지만 정책이 차단함"이 남는다. 상세: [`blocked-action-evidence.md`](./blocked-action-evidence.md).

---

## 8. 현재 구현 상태 (요약)

| 단계 | 상태 |
|---|---|
| Scope | ✅ CIDR 대역 + 도메인 서픽스 |
| ROE | ✅ 능력/작업/시간대 · ⏳ 가용성/기법/동시실행(아래 건의) |
| Policy | ✅ canonical key 승인 게이트 · ⏳ 위험도 기반(아래 건의) |
| RateLimit | ✅ sliding window · ⏳ per-agent/concurrent(아래 건의) |
| Budget | ✅ 횟수/도구호출/시간 |
| Approval | ✅ 승인 요청 + 사람 개입 CLI(Track B) |
| 심각도 신호 | ✅ 생성 · ⏳ 실제 라우팅(Agent, 아래 건의) |
| 차단 Evidence | ✅ 기록 + 배선 |

---

# 건의사항

남은 작업은 대부분 **혼자 못 하고 다른 팀 결정이 필요**하다. 이유는 공통적으로
**"판단하고 싶은데 필요한 정보/권한이 우리 쪽에 없다"** 이다.

## 9. PM 건의 — 요청서(ActionProposal)에 신호 부족

문지기가 아래를 판단하고 싶지만, **에이전트 요청서에 판단 근거가 없다.** 요청서
양식은 공통 규격(Contract v0.1)이라 우리가 못 바꾼다 → **PM 결정 필요.**

| # | 하고 싶은 것 | 왜 못 하나 | 제안 |
|---|---|---|---|
| ① | 가용성 영향 행동 금지 (`availability_impact_allowed`) | 요청서에 "이 행동이 가용성에 영향 주나?" 플래그 없음 | 요청서에 플래그 추가 **또는** capability/operation → 영향 여부 config 매핑 |
| ② | 특정 기법 금지 (`prohibited_techniques`) | 요청서에 "technique" 항목 없음 | 요청서에 technique 필드 추가 **또는** operation과 기법 매핑 테이블 |
| ③ | 위험도 기반 승인/차단 | `risk_level`은 도구 선택 후(ToolDescriptor)에만 있고 요청 단계엔 없음 | 요청서에 예상 risk 추가 **또는** capability→risk config 매핑 |

> 세 건 모두 "요청서 필드 추가" 또는 "config 매핑" 중 택1. 스키마를 안 바꾸는
> config 매핑 쪽이 Contract v0.1을 유지해 더 가볍다. **PM이 방향 결정** 후 반영.

## 10. Agent 팀 건의 — 판단 이후의 "행동"은 Agent 몫

문지기는 "판단"까지만 한다. 그 판단을 실제 행동으로 옮기는 건 오케스트레이션이다.

| # | 건의 | 내용 |
|---|---|---|
| ④ | 심각도대로 라우팅 | Control이 DENY에 심각도 태그(🟡BLOCK/🟠REPLAN/🔴ABORT)를 붙인다. 라우터가 `severity_from_matched_rules(decision.matched_rules)`로 읽어 **재시도 / 목표전환 / 세션종료**로 나눠 처리해달라 (현재는 DENY를 모두 동일 처리) |
| ⑤ | 실행 중 개수 피드백 | "동시에 N개까지만"(ROE `max_parallel_actions`, RateLimit `max_concurrent_actions`) 제한을 하려면 **지금 몇 개가 실행 중인지**가 필요하다. 실행 시작/종료를 Control이 알 수 있게 해달라 |

---

## 요약

- **PM** → 요청서에 신호 3개(가용성·기법·위험도) 추가 또는 config 매핑 허용 → **이슈 1건**
- **Agent** → ④심각도 태그 라우팅 반영 + ⑤실행 중 개수 피드백 → **협의 2건**
- 이 결정들이 들어오면 Control이 바로 해당 기능을 완성할 수 있다.
