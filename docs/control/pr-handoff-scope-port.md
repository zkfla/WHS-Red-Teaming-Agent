# PR 핸드오프 — Scope 포트 수정 후 기록 회귀 검증 (Track B)

작성일: 2026-10-02
작성: Track A (Control — 정책 판단·범위 검사)
대상: Track B (Control — 승인·감사·증거 관리)

## 0. 한 줄 요약

Track A가 Scope 비교를 포트 인식(`host:port`)으로 고쳤다. 이로 인해 **새로운 Scope DENY 케이스**가 생겼고, 이 거절들이 **Audit·차단 Evidence에 제대로 기록되는지 회귀 검증**하는 것이 Track B의 일이다. Scope 비교 로직 자체는 건드리지 않는다.

## 1. Track A가 한 일 (전제)

- 커밋: `062b183` fix(control): port-aware scope matching for host:port targets (브랜치 `dev-control`)
- 파일: `src/redteam/control/scope.py` (+ `docs/control/PLAN.md` §5 결정 기록)
- 방식: `EngagementScope`의 기존 `allowed_hosts`/`denied_hosts`(`list[str]`)에 `host:port` 문자열 규약을 허용. **`core/contracts` 변경 없음.** 전용 포트 필드 승격은 하지 않기로 확정 (PLAN.md §5).
- 테스트: `b79853b` test(control): ... — Track A가 작성한 scope 단위 테스트. **이 테스트 커밋은 dev로 올리지 않는다** (아래 §5 참고).

### 새로 생긴 Scope DENY 사유

| 케이스 | 거절 사유(reason) 예 |
|---|---|
| 포트 불일치 | 허용 `127.0.0.1:8080`인데 `127.0.0.1:9090` 요청 |
| bare-host 거절이 포트 요청까지 차단 | 거절 `127.0.0.1` → `127.0.0.1:8080` 요청도 DENY |
| 포트 추정 불가 | 허용이 `host:port`인데 Target에 포트 없음 → `"port cannot be inferred"` |
| 주소 파싱 실패 | malformed host/url/service → `"target rejected: malformed ..."` |
| endpoint 포트 충돌 | host 문자열 포트 ≠ `EndpointTarget.port` → `"...conflict..."` |
| 범위 밖/깨진 포트 | `99999`, `notaport` 등 |

이 모든 DENY는 severity `BLOCK_ACTION`이다.

## 2. 검증 대상 코드 경로

`src/redteam/control/service.py`:

```
evaluate()
  └─ check_scope(...)            # scope DENY 발생 (새 케이스 포함)
      └─ _finalize(..., verdict=DENY, reasons=scope_reasons, ["scope"], severity=BLOCK_ACTION)
            ├─ _audit.record(proposal, decision)              # 감사 기록
            └─ _record_blocked_action(proposal, decision)     # 차단 Evidence (evidence_service 있을 때, best-effort)
```

- severity는 `PolicyDecision`에 전용 필드가 없어 `matched_rules`에 `to_tag(severity)` 태그로 실린다 (`redteam.control.severity`).
- `_record_blocked_action`은 best-effort: Evidence 실패가 verdict/리턴을 바꾸면 안 된다 (`docs/control/blocked-action-evidence.md §10.2`).

## 3. Track B가 할 일

1. **감사(Audit) 기록 검증** — §1의 새 DENY 각각에 대해 `AuditLog` 레코드가 생성되고 `verdict=DENY`·`reasons`(거절 사유 문자열)·`BLOCK_ACTION` 태그가 보존되는지.
2. **차단 Evidence 검증** — `evidence_service` 연결 시 각 새 DENY가 `record_blocked_action`으로 Evidence에 남는지, Evidence 기록 실패가 verdict/리턴을 안 바꾸는지(best-effort).
3. **상관(correlation) 체인** — `proposal → decision → Audit/Evidence`가 `action_id`로 올바르게 연결되는지.
4. **회귀 테스트 확장** — 기존 B 담당 스위트에 새 scope DENY 케이스 추가:
   - `tests/unit/test_blocked_action_wiring.py`
   - `tests/unit/test_evidence_store.py`
   - `tests/unit/test_severity.py`
5. **결함 발견 시에만** `service.py`의 기록 경로를 수정한다. **`scope.py`는 건드리지 않는다.**

## 4. Track B가 하지 않는 것 (범위 밖)

- Scope 비교 로직 구현/수정 (Track A)
- MCP 설정 생성, 포트 규격(전용 필드 등) 결정 (설정·PM)
- 자연어 완료 조건, IDOR 세션 자동 생성 등 — 이번 Control 수정 범위 밖

## 5. 커밋 규칙 & PR 전략 (중요)

**dev에는 테스트를 올리지 않는다.** 그래서:

- **코드 수정과 테스트를 반드시 별도 커밋으로 분리할 것.** (예: `fix(control): ...` 커밋과 `test(control): ...` 커밋을 따로)
  - 그래야 dev PR 만들 때 **비-테스트 커밋만 cherry-pick**하고 테스트 커밋은 뺄 수 있다.
- 모든 작업은 `dev-control` 브랜치 위에서 (거기에 Track A의 `062b183` fix가 있음).
- 테스트/검증은 `dev-control`에 남아 로컬 회귀 안전망이 된다.

### 작업 단위(A+B) 완료 후 dev PR

```bash
git fetch origin
git switch -c pr/control-scope origin/dev
git cherry-pick 062b183          # A의 fix (+ B의 코드 커밋들, 테스트 커밋은 제외)
git push -u origin pr/control-scope
gh pr create --base dev --head pr/control-scope --repo zkfla/WHS-Red-Teaming-Agent
```

커밋이 코드/테스트로 잘 분리돼 있으면 "비-테스트 커밋만 cherry-pick"이 기계적으로 된다. 섞였을 경우 폴백:

```bash
git diff origin/dev..dev-control -- . ':(exclude)tests/**' | git apply --index
```

## 6. 브랜치 현황

| 브랜치 | 상태 |
|---|---|
| `dev-control` | 원격 푸시 완료. fix(`062b183`) + test(`b79853b`). B 작업도 여기에 쌓는다. |
| `dev` | 손 안 댐. 작업 단위 완성 후 테스트 제외 PR로 올림. |
