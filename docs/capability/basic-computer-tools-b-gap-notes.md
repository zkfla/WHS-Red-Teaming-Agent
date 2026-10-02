# Execution MCP Agent 연결 갭 조사

조사 기준: 로컬 `capability` 브랜치, B 보강 커밋 `eaf96f7`까지. 이 문서는
코드를 바꾸기 전에 확인한 사실과 최소 연결 설계를 기록한다.

## 1. 현재 경로와 연결이 불완전한 이유

현재 실행 경로는 다음과 같다.

1. `capability.bootstrap`이 execution stdio MCP를 발견한다.
2. `ExecutionMCPAdapter`가 도구를 `analysis.<remote tool name>` ToolId와
   `analysis` capability로 Registry에 등록한다.
3. `InvestigationEngine.context()`가 등록된 Agent들의 capability를 합친 뒤 해당
   capability의 건강한 도구를 LLM 컨텍스트의 `tools`에 넣는다.
4. LLM이 Path의 `allowed_operations`와 `ActionIntent`를 만들면
   `InvestigationEngine.compile_actions()`가 Agent capability, Path envelope, ROE,
   Registry, target binding을 확인하고 `CandidateCompiler`로 넘긴다.
5. 이후 기존 Plan node가 Control Plane과 Gateway를 거쳐 실행하고 Evidence를 남긴다.

execution 도구는 `analysis` capability로 등록되고 `analysis-agent`도 `analysis`를
보유한다. 따라서 MCP 서버가 활성화되면 현재 코드만으로도 LLM 컨텍스트에
`command_run` 계열이 나타날 수 있다. 반면 다음 경계가 없다.

- 명시적인 Agent opt-in이 없어 기본 비활성이라는 요구를 만족하지 않는다.
- LLM이 `executable`과 `args`를 직접 구성할 수 있다. 서버 allowlist가 최종 차단은
  하지만 Agent 연결 단계가 고정 템플릿과 그래프 사실만 사용한다는 요구를 만족하지
  않는다.
- `ANALYSIS_AGENT.playbook`에는 execution 단계가 없다. 다만 현재 주 실행기는
  `CandidateProviderRegistry`의 playbook 후보를 실행 후보로 사용하지 않고, LLM이 만든
  Path/ActionIntent를 `compile_actions()`에서 컴파일한다. Playbook은 현재
  `reference_paths()`의 코드 참고 자료로만 사용된다. 따라서 playbook 한 줄 추가만으로는
  실제 Agent 실행 경로를 연결할 수 없다.

이 때문에 필요한 작업은 새 workflow가 아니라, 현재 LLM 주도 경로에 좁은 opt-in
필터와 결정적인 인자 바인딩을 추가하는 것이다.

## 2. 관련 코드별 확인 결과

| 영역 | 확인 결과 | 의미 |
| --- | --- | --- |
| `orchestration/agents/builtin.py` | Analysis playbook은 APK 정적 분석 4단계만 선언 | execution 정적 단계는 없음 |
| `orchestration/agents/registry.py` | 기본 Registry는 recon/web/mobile/analysis를 스케줄 | 새 Agent를 만들 필요 없음 |
| `orchestration/agents/providers.py` | mobile/analysis는 공용 playbook provider 사용 | 현재 LLM 주 실행 경로의 강제 연결 지점은 아님 |
| `orchestration/engine.py` | live catalog 도구를 Agent capability 기준으로 LLM에 노출 | execution은 `analysis`라 현재 자동 노출 가능 |
| `orchestration/engine.py` | ActionIntent를 Path, Agent capability, ROE, catalog, target에 대조 | opt-in 및 고정 인자 검사를 넣을 최소 경계 |
| `orchestration/seed.py` | Asset에 `asset_ref`, `apk_ref`, `target_ref`를 선언 사실로 기록 | workspace 상대 입력 경로 사실은 아직 없음 |
| `core/contracts/engagement.py` | `Engagement.metadata` 확장점이 이미 있음 | Core 계약 변경 없이 opt-in 설정 가능 |
| `core/contracts/engagement.py` | ROE에 `allowed_operations`가 있음 | `analysis.command_run`을 명시적으로 허용/제외 가능 |
| execution profile | 도구 이름과 operation binding을 정확히 고정 | MCP tool allowlist는 이미 fail-closed |

## 3. 연결 방법 비교

### 안 1: 기존 Analysis Agent의 LLM 경로에 opt-in 필터 추가 — 선택

- `Engagement.metadata.execution_agent.enabled`가 정확히 `true`일 때만
  `analysis.command_run`을 LLM 도구 공간에 노출한다.
- 지원 recipe는 코드에 고정한다. 첫 recipe는 `line_count`이며 `wc -l <path>`로
  컴파일한다.
- Asset별 workspace 상대 경로를 engagement metadata에서 검증하여 Attack Graph의
  선언 사실로 옮긴다.
- Action compile 시 모델이 보낸 executable/args를 신뢰하지 않고 recipe와 그래프
  사실에서 최종 arguments를 다시 만든다.
- 비활성 상태에서는 도구 필터 외의 컨텍스트, Agent registry, playbook, 후보 및 결과를
  바꾸지 않는다.

장점은 기존 Agent와 LLM 주도 Path/Action 파이프라인을 그대로 사용하고 새 capability나
새 Agent를 만들지 않는다는 점이다. 수정 지점도 도구 노출과 action compile 두 경계로
제한된다.

### 안 2: Analysis playbook에 선택적 실행 단계 추가

`ANALYSIS_AGENT.playbook`에 `command_run` 단계를 넣고 provider에서 설정에 따라
제거하는 방법이다. 현재 실행기는 playbook 후보를 주 실행 후보로 소비하지 않으므로
추가 wiring이 필요하다. 전역 상수인 AgentSpec을 engagement별로 복제해야 하며,
비활성 상태 동일성 검증도 더 어려워진다.

### 안 3: execution 전용 Agent 정의 추가

AgentSpec, AgentDefinition, provider, activation rule, registry, mission bootstrap 및
스케줄 테스트를 함께 바꿔야 한다. 새 capability 도입 금지와도 맞지 않고 기본 Agent
순서/가중치/완료 판정에 영향을 줄 수 있다. 이번 임시 최소 연결에는 과도하다.

## 4. 선택안의 설정과 불변 조건

제안하는 engagement metadata 예시는 다음과 같다. 이 예시는
`deploy/mcp/execution/`에만 배포 예제로 추가한다.

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

- `enabled`가 없거나 `false`면 execution 도구는 Agent 컨텍스트에서 제외된다.
- Asset key는 scope의 allowed asset과 일치해야 한다.
- `path`는 상대 POSIX 경로만 허용하며 절대 경로, `..`, 빈 구성요소를 거절한다.
- recipe는 코드에 정의된 이름만 허용한다. executable과 option은 설정이나 LLM 문자열을
  받지 않는다.
- 최종 `asset_ref`는 graph의 선언 사실에서, `path`는 graph에 옮긴 선언 사실에서,
  engagement/run identity는 runtime에서 가져온다.
- ROE는 별도로 `analysis.command_run`을 허용해야 한다. Control Plane이 최종 정책
  결정을 내리고 서버 allowlist가 최종 실행 방어선으로 남는다.

## 5. 예상 변경 범위

아래 줄 수는 구현 전 예상이며 실제 diff는 각 커밋에서 다시 기록한다.

| 파일 | 예상 변경 | 이유 |
| --- | ---: | --- |
| `orchestration/execution_bridge.py` | 신규 약 140줄 | opt-in 파싱, recipe, 도구 필터, 고정 인자 생성 |
| `orchestration/seed.py` | 기존 파일 약 +20줄 | 검증한 Asset recipe/path를 선언 graph 사실로 이동 |
| `orchestration/engine.py` | 기존 파일 약 +15/-3줄 | 도구 노출 필터와 compile 시 고정 인자 적용 |
| `deploy/mcp/execution/*` | B 파일 추가 | metadata/ROE/MCP allowlist 예시 |
| 새 unit/integration 테스트 | 신규 파일 | off 불변성, on 경로, 정책 거절, 실서버 E2E |

`settings.py`, `bootstrap.py`, `action_naming.py`, Core/Control 계약, Agent registry,
Agent definition은 수정하지 않는 안이다.

## 6. 기존 테스트와 analysis schedulable 불일치

변경 전 Windows 선택 묶음은 최초 병렬 타이밍 실행에서
`146 passed, 1 failed`였다. 실패한
`test_long_running_tool_renews_its_work_lease`는 단독 재실행에서 `1 passed`였고,
execution 연결 변경 전의 lease 타이밍 변동으로 확인했다.

`tests/unit/test_orchestration_dynamic_candidates.py` 전체는 변경 전
`17 passed`이다. 요청에 적힌
`test_analysis_and_reporting_are_declared_as_non_schedulable_seams`라는 이름은 현재
로컬 및 `origin/capability` 버전에 존재하지 않는다. 현재 테스트는
`test_only_reporting_is_declared_as_a_non_schedulable_seam`이며 기대값은
`{"reporting-agent"}`이다. Track A가 Analysis Agent를 실제 schedulable role로 만든
현재 설계와 일치하므로 이 파일은 수정하지 않는다.

별도로 `tests/integration/test_methodology_agent_loop.py`에는 아직 다음 오래된 기대가
남아 있다.

```diff
- assert deps.agents.definition("analysis-agent").schedulable is False
+ assert deps.agents.definition("analysis-agent").schedulable is True
```

WSL2 전체 회귀의 알려진 1건 실패가 이 줄이다. 담당 팀에는 위 diff를 제안하되 이번 B
작업에서는 기존 테스트를 수정하지 않는다. Windows에서 이 파일 전체를 실행하면 해당
assert 전에 CP949로 emoji를 쓰는 fixture 11건이 실패하므로 schedulable 불일치를
Windows 결과로 재판정할 수 없다.

## 7. execution 매니페스트 필드

- `output_schema`: MCP SDK의 `list_tools()` 결과에서 각 Pydantic 결과 모델의 JSON
  schema가 발견되고, adapter가 `ToolDescriptor.output_schema`에 복사한다.
  `validate_execution_descriptor()`가 pinned schema와 동일한지 확인하며 Gateway도 실제
  반환을 이 schema로 검증한다.
- `health_status`: `MCPServerAdapter.discover_tools()`가 discovery 전에
  `health_check()`를 실행하여 `HEALTHY` 또는 `UNHEALTHY`를 각 ToolDescriptor에 넣는다.
  Registry와 Gateway의 health refresh도 이 필드를 갱신한다.

따라서 두 필드는 현재 채워진다. 보강 테스트에서는 execution descriptor 검증이
`health_status=UNKNOWN`을 허용하지 않도록 B 매니페스트 검증을 좁히고, 실제 discovery가
`HEALTHY`를 반환하는 근거를 별도 새 테스트에 둔다.

## 8. 이번 연결이 증명하는 범위

새 테스트의 목표는 opt-in 상태에서 `command_run`이 LLM 컨텍스트와 Path/Action 후보에
노출되고, 기존 Control Plane 및 Gateway를 거쳐 실행되어 Evidence가 남는 경로까지다.
정해진 fake LLM 응답으로 scheduling 경계를 검증한다. 실제 LLM이 상황을 해석하여 이
도구를 자유 판단으로 선택하는지는 Agent 팀의 별도 종단간 검증 대상이다.
