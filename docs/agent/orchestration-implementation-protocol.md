# ORCH-AGENT-FEEDBACK-20260921 — 실행 protocol

> 작업 ID: `ORCH-AGENT-FEEDBACK-20260921` / 실행 worktree:
> `C:\Users\qkrgu\.codex\worktrees\4261\WHS-Red-Teaming-Agent`
> 기준 commit: `7e49b1e` (= 4933698 + protocol `e213fff` + 이관된 3개 dirty fix)
> 결과 문서: `docs/agent/orchestration-implementation-result.md`

## 1. 기준 상태 확인 (게이트 A)

관리 체크아웃(`C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent`)의
미커밋 3개 파일을 **바이트 단위로 동일하게** 이관했다. blob 해시 일치로 확인:

| 파일 | 관리 체크아웃 blob | worktree blob |
|---|---|---|
| `src/redteam/orchestration/candidates/compiler.py` | `86d5df4` | `86d5df4` |
| `src/redteam/orchestration/nodes/plan.py` | `14b85f1` | `14b85f1` |
| `tests/unit/test_orchestration_candidates.py` | `3dcf8f6` | `3dcf8f6` |

이관 전 `plan.py`(4933698)는 `SyntaxError: '{' was never closed`(line 205)로
컴파일 자체가 불가능했다 → 이관 후 상태가 유일하게 실행 가능한 baseline이다.

베이스라인 측정(깨끗한 venv, `mcp 2.2.0`/`rich 14.3.4`):
`919 passed, 16 failed, 3 skipped, 2 xfailed`.

실패 16건 성격 분류:

- 12건: `OSError [WinError 1314]` — Windows 심볼릭 링크 권한 없음(환경).
  `Path.symlink_to`를 쓰는 테스트. 저장소 코드와 무관.
- 1건: `test_cli_smoke` — `validate.py:77`의 `✓`(U+2713)를 cp949 콘솔이
  인코딩하지 못해 `UnicodeEncodeError` → exit 2. 로케일 의존 기존 결함.
- 3건: 이벤트를 **정확한 목록으로 단언**하는 노후화된 테스트
  (`test_orchestration_nodes.py` 2건, `test_end_to_end.py` 1건).
  코드가 `CandidateGenerated`/`GraphMutationApplied`를 추가 발행했는데
  기대 목록이 갱신되지 않았다.

## 2. 구현 순서와 방법

각 항목은 **구현 → 단위/통합 테스트(양성 + 비날조 음성) → 커밋** 순서로 진행한다.

| 순서 | 항목 | 핵심 방법 |
|---|---|---|
| 1 | P0-2 동적 후보 생성 | `GraphCandidateContext`에 실제 catalog 기반 `available_operations`·planning-input fingerprint 추가, graph-driven provider seam 실사용 |
| 2 | P0-3 Web multi-step | `WebCandidateProvider.propose_graph`가 그래프 관찰값만으로 401/403 후속 후보 생성. 실제 `web.*` 도구 바인딩이 있을 때만 도구 특정 후보 생성 |
| 3 | P0-4 Hypothesis→Evidence→Finding | `orchestration/findings.py`의 `FindingPromoter`로 승격을 단일화하고 evidence 부족 시 거부 |
| 4 | P0-5 / P1-3 이벤트 lifecycle | `HYPOTHESIS_RESOLVED` 실제 해소 시 발행, `REPLAN_TRIGGERED`는 의미 있는 재계획에서만 발행 |
| 5 | P1-1 rejection diagnostics | reason 어휘 정본화 + 회귀 테스트 |
| 6 | P1-2 Result Mapper | `web.http_request` mapper 확장 + `web.http_compare_anonymous` mapper 추가 |
| 7 | P2 Analysis/Reporting | registry/wiring 정합 + reachability 테스트 |

## 3. 비날조 원칙 (검증 대상)

- `web.http_request`가 실제로 노출하는 것: `status_code`, `body_bytes`,
  `body_sha256`, `redirect`, `security_headers_present`(4개 bool),
  `body_omitted`, `target_ref`, `session_ref`.
- 노출하지 **않는** 것: `Location`, `WWW-Authenticate`, `Set-Cookie` 값,
  raw header, 응답 body. 이들은 Graph/Event에 절대 기록하지 않는다.
- 상태 코드 의미(401=인증 필요, 403=권한 거부)는 관측된 값에서 프로토콜 정의로
  유도되는 사실이며, 원문 헤더 증거와 구분되도록 `derived_from` 표식을 남긴다.
- 보안 헤더 부재는 관찰일 뿐 자동 취약점이 아니다 → Finding으로 승격하지 않는다.

## 4. 테스트·검증 게이트

- 게이트 B: 새 동작마다 양성/음성 테스트.
- 게이트 C: `python -m compileall src/redteam/orchestration`.
- 게이트 D: 오케스트레이션 단위/회귀 스위트.
- 게이트 E: `ruff check` / `ruff format --check` (변경 파일).
- 게이트 F: 전체 `pytest`. 기존 환경 실패는 위 §1의 증거로 구분하고
  전체 green이라 부르지 않는다.
- 게이트 G: 첨부 피드백 + 핸드오프 항목별 감사표.

## 5. 범위 밖 (수정하지 않음)

- `redteam.core` 계약, Control Plane, Capability 서버/도구 구현
  (`capability/servers/web_http.py`의 도구 계약은 읽기만 한다).
- 관리 체크아웃: 읽기만 한다.
- 외부/실제 대상 공격, 실제 MCP 서버 기동: 범위 밖. 로컬 fake/fixture만 사용.
- `app/cli/validate.py`의 cp949 인코딩 결함: 오케스트레이션 범위 밖 →
  결과 문서에 기존 결함으로 기록만 한다.

## 6. 파괴적 작업 금지

`reset --hard`, `checkout --`, `clean`, push, 다른 브랜치 수정은 하지 않는다.
기존 테스트를 삭제·약화·xfail 우회하지 않는다.
