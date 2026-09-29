# 오케스트레이션 팀 기본 동작 확인 보고서 — Attack Graph 담당

- 작성일: 2026-09-28
- 검증 ID: AG-GRAPH-BASIC-RUNTIME-20260928
- 기준 코드: 공유된 `dev` 커밋 `c042e8478555d192340f82fe113647a40cc53058`
- 확인 범위: 도구 결과가 Attack Graph에 반영되고 다음 행동으로 연결되는 기본 실행 흐름
- 방법: 해당 커밋을 별도 폴더에서 실행해 이후 작업 중인 코드와 구분했습니다.

## 1. 현재 상태 및 결론

**공유된 dev 코드에서 Recon/Web의 기본 흐름은 실제 MCP 호출까지 이어지는 것을 확인했습니다.** 로컬 테스트 서버를 대상으로 정찰을 시작해 첫 웹 요청을 보내고, HTML에서 새 경로를 찾은 뒤 보안 헤더를 확인하고 그 경로에 다시 요청하는 데까지 진행됐습니다.

Attack Graph도 도구가 찾아낸 정보를 저장하고 다음 행동 후보를 만드는 데 쓰였습니다. 이번 실행에는 테스트용 `FakeLLMClient`를 사용했기 때문에, **실제 LLM이 판단하고 도구를 고르는 과정까지 안정적으로 확인됐다고 말할 수는 없습니다.**

## 2. 기본 동작은 어디까지 가능한가

| 단계 | 현재 확인 내용 | 확인 수준 |
|---|---|---|
| 상태를 보고 행동 후보 생성 | Recon/Web Provider가 그래프의 Host·Service·Endpoint와 관측 정보를 사용 | 코드·관련 테스트·테스트용 은행 사이트 실행 |
| 후보 검증 및 선택 | CandidateCompiler가 등록된 도구와 인자를 검증. 기본 HybridPlanner는 점수에 따라 선택하고 모호한 경우 LLM에 선택을 요청 | 구조 및 테스트 확인. 이번 실제 통신 실행은 fake LLM 사용 |
| Control 검사와 실제 MCP 호출 | 실제 Control Plane과 Capability Gateway를 거쳐 stdio MCP 서버 호출 | 테스트용 은행 사이트에서 실행 확인 |
| 결과를 그래프에 반영 | `ToolResult → Result Mapper → GraphMutation → Attack Graph`로 저장하고 Evidence ID 연결 | 실제 결과·그래프·증거 대조 |
| 결과를 보고 후속 행동 실행 | 처음 제공하지 않았던 계정 경로가 HTML에서 발견된 뒤 Web 요청 대상으로 사용됨 | 실제 이벤트 순서로 확인 |
| 최종 판단 및 종료 | 실행 제한·승인 대기 사유와 ReportProjected 이벤트 확인 | 전체 검사의 자연 종료 및 최종 보고서 내용 품질은 이번 검증 대상 밖 |

현재는 Recon/Web에 마련된 후보 생성 규칙 안에서 다음 행동을 고릅니다. 새 Tool을 등록하더라도 그 Tool의 실행 후보와 결과 해석이 자동으로 추가되지는 않습니다.

## 3. 이번에 직접 확인한 실행 결과

저장소에 포함된 테스트용 은행 사이트(`finance-mock/server.py`)를 Docker에서 띄우고, 에이전트가 그 사이트에 요청을 보내도록 했습니다. HTTP 요청과 MCP 통신은 실제로 일어났습니다. 외부 사이트에는 접속하지 않았고, CLI 화면 대신 `build_application → run_engagement`를 직접 호출했습니다.

### A. 테스트용 은행 사이트에서 Web 실행을 허용한 경우

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

위 실행에서는 흐름을 끝까지 확인하기 위해 이 테스트에서만 `auto_approve`를 켰습니다. 다른 대상에서의 자동 승인 동작을 확인한 결과는 아닙니다.

### B. 기본 승인 필요 정책을 유지한 경우

- `recon.http_probe` 1회 실행 후 Web 요청에서 `ApprovalRequired` 발생
- Web Tool은 실행되지 않았고 `approval_pending`으로 종료
- 현재 기본 흐름에서 승인 경계가 적용됨을 확인
- 같은 대화 화면에서 승인 후 계속 실행되는 UI 동작은 이번에 검증하지 않음

## 4. Attack Graph 담당 관점의 확인 사항

제가 맡은 Attack Graph에서는 **도구가 알아낸 내용을 저장하고, 다음 행동을 고를 때 다시 쓸 수 있게 하는지**를 중점적으로 봤습니다.

- 정찰 결과의 서비스·Endpoint를 저장하여 Web 작업으로 넘어갈 근거를 제공합니다.
- HTML에서 발견한 경로를 기존 관측과 병합하고 후속 검사 후보가 읽을 수 있게 합니다.
- 관측 정보에 Evidence ID를 연결하여 어떤 실행 결과로 알게 된 정보인지 추적할 수 있게 합니다.
- 정책 차단·실행 시도 정보도 남겨 실행하지 못한 항목과 관측된 항목을 구분합니다.
- Mapper Registry가 도구별 결과 변환을 담당합니다. 등록되지 않은 도구는 도구별 상세 GraphMutation이 비어 있을 수 있으므로, 새 도구 연결 시 결과 매핑을 함께 확인해야 합니다.

이번 테스트에서는 그래프 저장이나 결과 매핑 때문에 다음 행동이 끊기는 문제는 없었습니다. 실제 대상과 다른 도구에서도 같은지는 추가로 확인해야 합니다.

## 5. 테스트 결과와 검증 한계

공유된 dev 커밋을 작업 중인 코드와 분리해 테스트했습니다. 선택한 회귀 테스트 **499개가 모두 통과**했고, 실패하거나 건너뛴 테스트는 없었습니다.

| 범위 | 통과 수 |
|---|---:|
| Attack Graph 단위 테스트 | 258 |
| Result Mapper 단위 테스트 | 54 |
| 동적 후보·Recon/Web 후속 행동·HybridPlanner 단위 테스트 | 92 |
| Agent 루프·Recon→Web·인증 후속 관측·Finding·MCP 관련 통합 테스트 | 95 |

499개 중에는 실제 MCP 대신 미리 준비한 도구 응답을 사용하는 테스트도 있습니다. 따라서 499개 전체가 실제 MCP를 호출한 결과는 아닙니다. 실제 MCP 호출과 HTTP 요청이 이어지는지는 위의 Docker 실행 두 건으로 따로 확인했습니다.

이번에는 전체 저장소 테스트나 lint/type 검사까지 돌리지는 않았습니다. 실제 LLM, 자연어 CLI 입력, 로그인된 Chrome/chrome-use, 실제 금융 대상도 아직 확인하지 못했습니다.

## 6. 우선 후속 확인

1. 같은 테스트용 은행 사이트에 실제 LLM을 연결하고, 어떤 도구를 골랐는지와 선택 이유, fallback 여부를 기록합니다.
2. 실제 CLI에서 사용자가 입력한 목표로 같은 흐름이 실행되는지 확인합니다.
3. chrome-use가 연결되면 화면 관측 → 행동 후보 → Control 검사 → 실제 행동 → 결과 매핑 → 후속 후보까지 동일 기준으로 검증합니다.

현재까지는 **Attack Graph에 결과가 쌓이고 그 결과로 후속 행동이 실행되는 기본 흐름**을 확인했습니다. 다음에는 실제 LLM·CLI·브라우저를 포함해 같은 흐름이 이어지는지 확인해야 합니다.

## 근거

- [검증 방법 및 재현](evidence/basic-runtime-20260928/README.md)
- [실제 MCP 실행 결과 JSON](evidence/basic-runtime-20260928/smoke-results.json)
- [499개 테스트 JUnit 결과](evidence/basic-runtime-20260928/pytest-head.xml)
- [검증용 실행 스크립트](evidence/basic-runtime-20260928/report_smoke.py)
- 주요 코드: `orchestration/observations/mapper.py`, `orchestration/nodes/update.py`, `orchestration/nodes/observe.py`, `orchestration/agents/recon.py`, `orchestration/agents/web.py`, `orchestration/agents/planners.py` (모두 `src/redteam/` 아래, 위 기준 커밋)
