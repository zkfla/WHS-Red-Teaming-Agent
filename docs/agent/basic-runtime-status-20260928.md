# 오케스트레이션 팀 기본 동작 확인 보고서 — Attack Graph 담당

- 작성일: 2026-09-28
- 검증 ID: AG-GRAPH-BASIC-RUNTIME-20260928
- 기준 코드: 공유된 `dev` 커밋 `c042e8478555d192340f82fe113647a40cc53058`
- 확인 범위: 도구 결과가 Attack Graph에 반영되고 다음 행동으로 연결되는 기본 실행 흐름
- 방법: 해당 커밋을 별도 임시 디렉터리에 추출하여 검증. 현재 작업 폴더에서 진행 중인 미커밋 변경은 공유 코드의 완료 실적에서 제외.

## 1. 현재 결론

**공유된 dev 코드에서 제한된 Recon/Web 시나리오의 기본 실행 흐름은 실제 MCP 통신까지 연결되어 있습니다.** 이번 확인에서는 로컬 합성 서버를 대상으로 정찰 → Web 요청 → HTML 경로 발견 → 보안 헤더 관측 → 발견한 경로에 대한 추가 요청이 실행됐습니다.

Attack Graph는 도구 결과를 저장하고, 저장된 정보를 후속 후보 생성에 전달하는 역할을 수행했습니다. 다만 이번 실제 통신 검증의 LLM은 `FakeLLMClient`입니다. **실제 LLM의 판단부터 MCP 실행, 결과 해석, 재계획까지 안정적으로 동작한다는 전체 완료 판정은 아직 할 수 없습니다.**

## 2. 기본 동작은 어디까지 가능한가

| 단계 | 현재 확인 내용 | 확인 수준 |
|---|---|---|
| 상태를 보고 행동 후보 생성 | Recon/Web Provider가 그래프의 Host·Service·Endpoint와 관측 정보를 사용 | 코드·관련 테스트·합성 서버 실행 |
| 후보 검증 및 선택 | CandidateCompiler가 등록된 도구와 인자를 검증. 기본 HybridPlanner는 점수에 따라 선택하고 모호한 경우 LLM에 선택을 요청 | 구조 및 테스트 확인. 이번 실제 통신 실행은 fake LLM 사용 |
| Control 검사와 실제 MCP 호출 | 실제 Control Plane과 Capability Gateway를 거쳐 stdio MCP 서버 호출 | 합성 서버 실행 확인 |
| 결과를 그래프에 반영 | `ToolResult → Result Mapper → GraphMutation → Attack Graph`로 저장하고 Evidence ID 연결 | 실제 결과·그래프·증거 대조 |
| 결과를 보고 후속 행동 실행 | 처음 제공하지 않았던 계정 경로가 HTML에서 발견된 뒤 Web 요청 대상으로 사용됨 | 실제 이벤트 순서로 확인 |
| 최종 판단 및 종료 | 실행 제한·승인 대기 사유와 ReportProjected 이벤트 확인 | 전체 검사의 자연 종료 및 최종 보고서 내용 품질은 이번 검증 대상 밖 |

현재 기본 판단은 등록된 Recon/Web 후보 생성 규칙의 범위에서 동작합니다. 새로운 Tool이 등록됐다고 해서 그 Tool을 활용하는 후보 생성이나 결과 해석까지 자동으로 갖춰지는 구조는 아닙니다.

## 3. 이번에 직접 확인한 실행 결과

외부 네트워크가 없는 임시 Docker 컨테이너에서 공유 dev 소스와 저장소의 기본 `finance-mock/server.py`를 사용했습니다. 실제 HTTP 요청과 실제 MCP stdio 통신을 수행했으며, 기존 실행 중인 랩과 로컬의 취약 모드 수정본은 사용하지 않았습니다. CLI 입력을 거치지 않고 `build_application → run_engagement`를 호출한 검증입니다.

### A. 합성 랩에서 Web 실행을 허용한 경우

```text
recon.http_probe
  → Host / Service / Endpoint 저장
web.http_request
  → 첫 페이지 응답 관측
recon.html_inventory
  → /api/accounts/alice 및 /assets/test-sdk.js 발견·저장
web.http_security_posture
  → 응답 보안 설정 관측
web.http_request
  → 발견한 /api/accounts/alice 요청
```

- 실제 Tool 실행: **5회** — Recon 2회, Web 3회
- Tool 실행 실패 이벤트: **0건**
- 최종 그래프: Host 1개, Service 1개, Endpoint 3개, Hypothesis 5개
- Evidence: **5개**. 그래프에 연결된 서로 다른 Evidence 참조 5개가 모두 실제 저장 레코드와 일치
- 허용되지 않은 `/assets/test-sdk.js`: 발견 정보는 저장했지만 Web 요청은 생성·실행되지 않음
- Finding: **0개**. 이번 검증은 취약점 발견 성능이나 실제 금융 솔루션 우회를 증명하지 않음
- 종료 사유: `max_steps_reached`. 6회 루프 단계 제한에 도달한 결과이며 전체 대상 검사가 끝났다는 뜻이 아님

이 실행에서만 합성 랩 설정의 `auto_approve`를 활성화했습니다. 일반 환경에서 자동 승인을 권장하거나 검증한 결과는 아닙니다.

### B. 기본 승인 필요 정책을 유지한 경우

- `recon.http_probe` 1회 실행 후 Web 요청에서 `ApprovalRequired` 발생
- Web Tool은 실행되지 않았고 `approval_pending`으로 종료
- 현재 기본 흐름에서 승인 경계가 적용됨을 확인
- 같은 대화 화면에서 승인 후 계속 실행되는 UI 동작은 이번에 검증하지 않음

## 4. Attack Graph 담당 관점의 확인 사항

제 담당 영역의 핵심은 **실행 결과를 다음 판단에 사용할 수 있는 상태로 만드는 것**입니다.

- 정찰 결과의 서비스·Endpoint를 저장하여 Web 작업으로 넘어갈 근거를 제공합니다.
- HTML에서 발견한 경로를 기존 관측과 병합하고 후속 검사 후보가 읽을 수 있게 합니다.
- 관측 정보에 Evidence ID를 연결하여 어떤 실행 결과로 알게 된 정보인지 추적할 수 있게 합니다.
- 정책 차단·실행 시도 정보도 남겨 실행하지 못한 항목과 관측된 항목을 구분합니다.
- Mapper Registry가 도구별 결과 변환을 담당합니다. 등록되지 않은 도구는 도구별 상세 GraphMutation이 비어 있을 수 있으므로, 새 도구 연결 시 결과 매핑을 함께 확인해야 합니다.

이번 범위에서는 그래프 저장·결과 매핑·후속 행동 연결을 막는 실패가 발견되지 않았습니다. 이 결론은 아래 테스트와 합성 시나리오 범위에 한정됩니다.

## 5. 테스트 결과와 검증 한계

공유 dev 소스를 명시적으로 import하도록 분리한 환경에서 선택한 회귀 테스트 **499개가 통과**했습니다. 실패·건너뜀은 0개입니다.

| 범위 | 통과 수 |
|---|---:|
| Attack Graph 단위 테스트 | 258 |
| Result Mapper 단위 테스트 | 54 |
| 동적 후보·Recon/Web 후속 행동·HybridPlanner 단위 테스트 | 92 |
| Agent 루프·Recon→Web·인증 후속 관측·Finding·MCP 관련 통합 테스트 | 95 |

통합 테스트에는 가짜 Gateway·합성 응답을 사용한 사례와 실제 MCP 프로세스를 실행한 사례가 섞여 있습니다. 499개 전체를 실제 서버 E2E로 표현하지 않습니다. 별도의 Docker 실행 A/B 두 건이 이번 실제 MCP·HTTP 통신 근거입니다.

전체 저장소 테스트, 전체 lint/type 검사, 실제 외부 LLM, 사용자 자연어 CLI 입력, 로그인된 Chrome/chrome-use, 실제 금융 대상에 대한 검증은 이번 보고서의 완료 범위에 포함하지 않았습니다.

## 6. 우선 후속 확인

1. 동일한 제한된 합성 시나리오에서 실제 LLM을 연결하고, 선택 이유 및 fallback 여부와 실제 Tool 선택 결과를 기록합니다.
2. 실제 CLI에서 사용자가 입력한 목표로 같은 흐름이 실행되는지 확인합니다.
3. chrome-use가 연결되면 화면 관측 → 행동 후보 → Control 검사 → 실제 행동 → 결과 매핑 → 후속 후보까지 동일 기준으로 검증합니다.

현재 보고 가능한 상태는 **“Attack Graph가 결과를 보존하고 후속 행동으로 연결하는 제한된 기본 루프는 확인됐으며, 실제 LLM·CLI·브라우저를 포함한 종단 간 안정성 확인이 다음 우선 작업”**입니다.

## 근거

- [검증 방법 및 재현](evidence/basic-runtime-20260928/README.md)
- [실제 MCP 실행 결과 JSON](evidence/basic-runtime-20260928/smoke-results.json)
- [499개 테스트 JUnit 결과](evidence/basic-runtime-20260928/pytest-head.xml)
- [검증용 실행 스크립트](evidence/basic-runtime-20260928/report_smoke.py)
- 주요 코드: `orchestration/observations/mapper.py`, `orchestration/nodes/update.py`, `orchestration/nodes/observe.py`, `orchestration/agents/recon.py`, `orchestration/agents/web.py`, `orchestration/agents/planners.py` (모두 `src/redteam/` 아래, 위 기준 커밋)

