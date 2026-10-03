# Execution MCP Agent 최소 연결 변경 노트

수신: 오케스트레이션 담당 박현태 님

작성일: 2026-10-02

상태: 로컬 `capability` 브랜치에만 커밋, push하지 않음

## 목적과 범위

기존 Analysis Agent가 execution MCP의 `command_run`을 선택 가능한 후보로 보고,
선택됐을 때 기존 Control Plane과 Gateway를 거쳐 실행할 수 있는 최소 연결을 추가했다.
새 Agent, 새 capability, 새 workflow는 만들지 않았다. 기본값은 꺼짐이며 Agent 팀이
workflow를 다시 설계할 때 이 bridge를 교체할 수 있다.

이 변경이 검증하는 범위는 고정된 Agent 응답을 사용한 후보 노출, 스케줄, 정책 경유,
MCP 실행과 Evidence 기록까지다. 실제 LLM이 상황에 따라 도구를 자유 판단으로 선택하는지
여부는 Agent 팀의 별도 종단 검증이 필요하다.

## 변경 파일

| 파일 | `origin/capability...HEAD` 누적 diff | 이유 |
| --- | ---: | --- |
| `src/redteam/orchestration/execution_bridge.py` | `+209/-0` | opt-in schema, Asset recipe 검증, 도구 필터, graph-bound 고정 인자 |
| `src/redteam/orchestration/seed.py` | `+3/-0` | 검증한 recipe/path를 기존 Asset 선언 사실에 추가 |
| `src/redteam/orchestration/engine.py` | `+50/-6` | context 도구 필터와 execution operation 전용 인자 바인딩 |
| `tests/unit/test_execution_agent_bridge.py` | `+198/-0` | 기본 off 불변, 설정 검증, 고정 인자, sync operation 한정 |
| `tests/integration/test_execution_agent_e2e.py` | `+425/-0` | Agent loop→Control→Registry/Gateway→MCP→Evidence와 거절 경로 |

공통 `settings.py`, `bootstrap.py`, `action_naming.py`, Agent registry와 builtin Agent 정의는
이번 최소 연결에서 수정하지 않았다. 기존 테스트 파일도 수정하지 않았다.

## 설정과 실행 경계

`Engagement.metadata.execution_agent`가 없거나 `enabled`가 정확히 `true`가 아니면 모든
execution operation을 Agent의 live tool context에서 숨긴다. 활성화할 때는 scope에 이미
있는 Asset마다 다음 두 사실을 operator 설정으로 공급해야 한다.

```json
{
  "execution_agent": {
    "enabled": true,
    "assets": {
      "workspace-test": {
        "recipe": "line_count",
        "path": "input.txt"
      }
    }
  }
}
```

현재 지원 recipe는 `line_count` 하나다. bridge는 모델이 제시한 executable과 args를
사용하지 않고 graph의 `asset_ref`, `execution_recipe`, `execution_workspace_path`와
runtime의 engagement/run identity에서 다음 인자를 다시 만든다.

```text
wc -l <validated-relative-workspace-path>
```

Agent에는 동기 `command_run`만 노출한다. `command_job_start/status/cancel`은 서버와
Gateway에서 사용할 수 있지만 이 임시 Agent bridge에서는 숨긴다. ROE에
`analysis.command_run`이 없으면 Control Plane이 거절하고, 서버 executable/option
allowlist가 마지막 실행 방어선으로 남는다.

## 기존 줄 변경의 전후와 이유

초기 Agent 연결 커밋에서 `engine.py`의 기존 8줄이 변경 또는 이동됐다.

1. healthy 도구 조건에 `agent_tool_visible()`을 더해 opt-in 전에는 execution 도구를 숨겼다.
2. target node 조회 세 줄을 execution 인자 생성 전에 사용할 수 있도록 앞당겼다.
3. catalog 존재·tool 조회 세 줄을 같은 이유로 앞당기고 조회 결과를 재사용했다.
4. 기존 `catalog.get(...) is None` 검사를 `tool = ...; tool is None` 형태로 바꿨다.

첫 WSL2 전체 회귀에서 비-execution action도 catalog 검사를 operator exclusion보다 먼저
받아 기존 endpoint suppression 테스트가 새로 실패했다. 후속 커밋에서는 조기 node/tool
조회를 `EXECUTION_OPERATIONS`에만 한정했다. 비-execution action은 기존 순서인 operator
exclusion → path/principal envelope → node → catalog 검사를 다시 따른다. execution action만
graph-bound arguments가 operator/policy 검사 전에 확정된다.

## 기본 비활성 동작 불변 증거

커밋 `1282ab4`의 부모 worktree와 현재 HEAD에서 같은 기존 테스트를 실행했다.

```text
수정 전: 146 passed, 1 deselected in 9.86s
수정 후: 146 passed, 1 deselected in 9.64s
lease timing 테스트 단독: 수정 전 1 passed / 수정 후 1 passed
```

대상은 agents, candidate compiler, dynamic candidates, orchestration nodes, seed, Agent loop의
기존 테스트다. 이 기존 테스트 파일들의 `origin/capability...HEAD` diff는 0이다.

endpoint suppression 회귀 복원 후에는 이 묶음에 해당 파일 전체를 더해
`150 passed, 1 deselected`를 확인했다. Agent bridge/E2E와 문제였던 기존 테스트를 묶은
검증은 `12 passed, 1 skipped`였고 Ruff도 통과했다.

## 활성 경로 증거

- Windows fake Docker: bridge/E2E `11 passed, 1 skipped`
- WSL2 실제 Docker: Agent loop→실제 execution MCP→Evidence `1 passed`
- ROE에서 operation 제외: Control Plane DENY, Docker 시작 0
- 허용하지 않은 executable: 서버의 `execution_executable_not_allowed`, Docker 시작 0
- `ecde5dd` 병합 뒤 최종 WSL2 전체 회귀: `2230 passed, 47 skipped, 0 failed`
- 기준선 passed nodeid 2172개 누락 0, 비활성 후보 snapshot hash 동일

`test_methodology_agent_loop.py` 충돌은 incoming의 built-in definition 동등성 검증을
유지하고 Track A의 런타임 activation reason assertion만 추가하는 방식으로 해결했다.
scripted Agent의 적대적
인자가 고정 recipe로 교체되는 실제 Docker 경로는 통과했다. 실 provider 테스트 진입점도
추가했지만 현재 환경에는 provider/model/API key가 없어 실제 자유 선택은 미검증이다.

## 팀 합의가 필요한 항목

- `command_run` 및 `command_job_*` canonical operation 이름
- execution 도구가 임시로 사용하는 `analysis` capability 유지 여부
- Agent workflow 재설계 시 metadata bridge를 유지·대체할 방식
- 실제 LLM 자유 선택; 비동기 job poll/cancel 실동작은 검증 완료
