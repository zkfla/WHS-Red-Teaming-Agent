# dev 병합 후 팀별 연결 검토 (2026-10-01)

## 검토 기준

- 현재 작업 브랜치: `dev-orchestration`, 기반 커밋 `f7407b6`.
- 로컬 `origin/dev`도 같은 커밋이다. 로컬 `dev` 브랜치는 이전 커밋이므로 이 보고서는 이전 로컬 `dev` 체크아웃을 대상으로 하지 않는다.
- 현재 턴의 보완은 작업 트리에만 있다. 아직 commit/push하지 않았다.
- 사용자 지시로 수정 범위를 오케스트레이션 내부와 그 검증 테스트·팀 문서로 제한했다. 공통 `app/bootstrap.py`에 추가했던 종료 이벤트 필드 3개는 되돌렸다. CLI/Intake/Control/Capability/Core 소스는 수정하지 않는다. 다른 팀 항목은 재현 결과와 전달 제안이다.
- 금융 시나리오는 범위에 유지한다. 아래 금융 검증은 로컬 합성 대상이며 금융기관 시스템이나 규제 준수 검증을 의미하지 않는다.

## 현태님이 제기한 항목별 판정

| 항목 | 현재 확인 결과 | 담당 및 다음 작업 |
| --- | --- | --- |
| 자연어 단계 완료 조건 | 일부 남음. `PathStep`에 문자열만 오면 `kind=unknown`으로 변환되어 성공 불가. LLM이 지원되는 kind와 자연어 description으로 계획을 생성해야 한다. 정상 `complete_goal`은 원래 Mission 기준 각각을 모델이 평가하고 동일 run/path의 실제 Evidence로 검증한다. 반면 blocked/중단 정산의 `finalize_mission`은 여전히 `verified evidence for ...`만 지원한다. | **오케스트레이션**: 원문 보존 및 구조화된 조건 생성의 실제 모델 회귀, terminal 정산과 정상 완료의 항목별 평가를 통일. unknown을 임의로 성공 처리해서는 안 된다. |
| 포트가 포함된 대상의 범위 비교 | 재현됨. scope `127.0.0.1:5000`은 `EndpointTarget(host=127.0.0.1, port=5000)`과 매칭되지 않는다. | **Control**: target과 allow/deny의 host·port 표현을 합의하고 정규화. **CLI/초기 설정**도 같은 표현을 생성. 단순히 포트를 버려 모든 포트를 허용하는 수정은 부적절하다. IPv6와 denied 항목도 포함해 검증해야 한다. |
| MCP 세부 허용 경로 동기화 | 남음. `_write_recon_policy`는 첫 allowed host와 `/`만 기록한다. 현재 Intake draft에는 MCP allowed_paths/session reference를 저장할 필드가 없다. `REDTEAM_MCP_SERVERS`가 있으면 자동 설정 접점도 건너뛴다. | **CLI/Intake + Capability**: 승인된 target_ref·origin·경로·네트워크·session reference를 정책에 연결하고 변경 시 정책 버전/동기화 확인. 발견한 경로를 자동으로 허용 범위에 추가해서는 안 된다. |
| IDOR용 사용자 세션 자동 생성 | 임의 사용자/자격 증명 생성은 구현되어 있지 않다. 제공된 opaque reference를 발견하고 실제 비교에 사용하는 경로는 이번 실제 MCP E2E에서 검증했다. 등록됐지만 credential이 없거나 인증되지 않으면 blocked이다. | **초기 설정/CLI**: 사용할 두 테스트 세션을 연결. **Capability**: 실제 인증/세션 도구 제공. **오케스트레이션**: 실제 결과를 근거로 경로 변경·비교·blocked 결정. 신규 계정 생성까지 원하면 허용된 가입/테스트 계정 발급 도구와 별도의 ROE가 필요하다. |
| 종료 후 일반 질문이 새 목표가 됨 | 예전처럼 모든 문장을 바로 실행하지 않는다. 현재 `_session_loop`는 모델의 answer/clarify/guidance를 표시하고 execute일 때만 새 Mission을 시작한다. 이 분기는 단위 테스트로 검증됨. 실제 모델이 질문을 execute로 오분류하는지까지는 이번에 검증하지 않았다. | **CLI**: 실제 모델로 “왜 종료됐어?”, “어떤 결과야?”, “IDOR가 뭐야?”는 answer, “추가로 점검해줘”는 execute가 되는 회귀 확인. 답변용 세션 문맥에 Mission의 기준별 결과도 전달해야 한다. |
| 종료 상태·결과 전달 / 종료 시점 사용자 지시 귀속 | 오케스트레이션 내부의 completed/failed/cancelled Mission 변경 지시 거부를 보완했다. 원래 Mission/Run에 REJECTED audit 기록을 남긴다. Mission 상태/기준별 결과는 terminal report에 있으나 RunFinished에는 전달되지 않는다. 공통 bootstrap 변경은 되돌렸다. `OperatorConsole.aclose`가 소비 작업을 취소하며 입력 대기열을 idle 세션으로 넘기지 않는 문제도 남는다. | **PM/CLI 담당**: 기존 Mission projection을 아래 제안대로 전달·렌더링하고 세션 문맥에 저장. **오케스트레이션**: 자신이 받은 지시의 원래 identity 유지 및 종료 후 변경 거부. **CLI**: 종료 경계 입력 접수·대기열 전달/거부 및 회귀 테스트. |

## 이번 작업 트리 보완

1. `OperatorIntentService._mutate`에서 종료된 Mission의 조사 변경 지시를 거부한다. 상태/설명 질문은 계속 가능하다. blocked Mission의 재시도·재계획 동작은 이 변경의 대상이 아니다.
2. 공통 bootstrap의 `RunFinished.payload` 변경은 되돌렸다. 아래는 **PM/CLI 담당에게 전달할 제안**이며 현재 구현에 추가하지 않았다. 오케스트레이션의 기존 terminal Mission projection을 사용하도록 연결하면 된다.

```text
mission_id
mission_status
mission  # TerminalReportProjection.mission과 동일한 JSON projection
         # objective, status, tasks, success_criteria(met/reason/evidence_count), ...
```

3. `tests/integration/test_principal_mcp_transport_e2e.py` 추가:
   - production `build_application`, Control, MCP gateway 및 stdio subprocess를 사용한다.
   - `web.session_list` → step 완료 → path v1에서 v2로 재계획 → `web.http_compare_sessions`를 실행한다.
   - 실제 로컬 HTTP 대상에 두 번 GET하고, SQLite 경로/Task/Mission과 filesystem Evidence 및 종료 보고서를 다시 읽는다.
   - 두 개의 한국어 Mission 성공 기준에 실제 Evidence가 연결되는지 확인한다.
   - 200/403은 비교 목표 완료, 401/401은 blocked, 두 번째 bearer credential 누락은 실패 증거와 blocked이다.
   - MCP 서버는 credential 누락을 만난 시점에 실패한다. 비교 전에 첫 요청이 이미 실행될 수 있으며 테스트는 이 실제 동작도 검증한다.
   - raw synthetic bearer 값은 Evidence/보고서에 저장되지 않고, HTTP 실행만으로 Finding을 만들지 않는다.
   - 기존 종료 이벤트의 종료 이유·실행 수와 실제 Runtime 결과가 일치하는지 검증한다. Mission의 기준별 결과는 저장된 보고서에서 검증한다.
4. 종료된 Mission의 늦은 지시 거부/원래 identity 유지/상태 질문 허용 회귀 테스트 10개 추가.

## 확인한 재현

```text
Control: allowed_hosts=[127.0.0.1:5000], EndpointTarget(host=127.0.0.1, port=5000)
=> allowed=False, target ['127.0.0.1'] is not within engagement scope

terminal 자연어 criterion: '제공된 두 세션의 접근 결과를 비교한다'
=> met=False, criterion has no supported evaluator

보완 전 완료 Mission에 '추가 확인을 계속해줘'
=> applied=True, guidance_added_for_next_decision, mission_status=completed
보완 후
=> REJECTED(reason=mission_closed), 기존 Mission 그대로 유지, 원래 Mission/Run의 감사 기록 유지
```

## 검증 및 한계

- 병합 커밋 기준 기본 전체 테스트: **1,689 passed / 7 skipped**.
- 보완 후 관련 회귀: **116 passed**; Ruff 통과, mypy **399 files 통과**.
- 실제 로컬 TCP 허용 환경에서 principal MCP E2E 3개 + 기존 streamable HTTP discovery/invocation + finance TLS: **5 passed**. streamable HTTP는 gateway 수준 테스트이며 principal Mission 전체 흐름은 stdio로 검증했다.
- 공통 bootstrap 변경을 되돌리기 전 전체 회귀: **1,704 passed / 5 skipped (59.74s)**. Skip은 외부 apktool/jadx/실기기/후보 Mobile MCP 환경 미제공이다.
- 소유 범위 정리 후 오케스트레이션 지시 처리·실제 MCP E2E·기존 종료 이벤트 회귀: **84 passed (3.49s)**. Ruff 및 diff 공백 검사 통과. 공통 bootstrap의 diff는 없다.
- LLM만 명시적으로 scripted client를 주입했다. 실제 LLM API/OAuth 네트워크 호출 및 판단 품질, 실제 금융 대상, 외부 Mobile MCP는 이번 검증 범위에 포함되지 않는다.

## 우선순위

1. **우리 팀 구현**: 자연어 조건의 blocked/중단 정산과 정상 완료 평가 통일, 오케스트레이션에 접수된 종료 경계 지시 처리.
2. Control·CLI·Capability: target authority와 MCP 정책의 경로/세션 설정을 함께 합의하고 연결.
3. CLI: 기존 Mission 결과 전달·렌더링·세션 문맥 보존·질문 분류 실제 모델 회귀. 우리 팀은 직접 수정하지 않는다.
4. 위 연결 후 실제 LLM을 사용하는 금융 합성 대상 데모와 발표용 실행 기록 확보.

현재 결과로 2·3단계 전체가 완료됐다고 판정하지 않는다. 실제 전송과 제공된 세션 활용은 확인됐으며, 위 미완료 연결·종료 정산 항목을 해결한 뒤 완료 판정을 해야 한다.
