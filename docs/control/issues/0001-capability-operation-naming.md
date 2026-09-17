# ActionProposal capability / operation naming

- 제안: Control 팀 (ROE 담당)
- 상태: **PM 확정 — Agent 반영 완료, Control 후속 수정 필요**
- 기준 계약: [`docs/contracts/actions.md`](../../contracts/actions.md)

## 확정 규칙

`ActionProposal.capability`은 `recon`, `web`, `mobile`, `analysis` 중 하나인
canonical namespace다. `operation`은 실제 도구 이름이 아니라 계약에 등록된
tool-independent intent다. ROE가 비교하는 operation key는 다음과 같다.

```text
<capability>.<operation>
```

예: `capability="recon"`, `operation="port_scan"`의 ROE key는
`recon.port_scan`이다. 실행 수단인 `ToolId`(`recon.nmap_scan`)와는 별도 값이다.

## Agent / Orchestration 반영

기본 실행 step은 아래처럼 정규화됐다.

| 역할 | capability | operation | preferred ToolId |
|---|---|---|---|
| Recon | `recon` | `service_discovery` | `recon.ping` |
| Web/API | `web` | `http_request` | `web.http_inspect` |
| Mobile | `mobile` | `static_analysis` | `mobile.artifact_analyze` |

오케스트레이션은 비규격 이름을 `PlanStep` 생성 시 거부하고, graph attempt도
slash 조합이 아닌 canonical action key로 기록한다. capability tag만 같은 임의
Tool로 fallback하지 않으며 ALLOW 후 실행 직전에 binding과 health를 재검증한다.

## Control 후속 수정 필요

`dev-control`의 현재 `check_roe()`는 `action.operation`을
`allowed_operations`/`denied_operations`와 직접 비교한다. 확정 계약은
`f"{action.capability}.{action.operation}"`을 비교하도록 요구하므로 현재 구현은
`denied_operations: ["recon.port_scan"]`을 조용히 우회한다.

Control 팀은 다음을 수정해야 한다.

1. operation allow/deny 비교값을 canonical action key로 변경
2. 테스트 fixture를 canonical capability/operation으로 변경
3. `recon.port_scan` deny와 allow-list의 fail-closed 회귀 테스트 추가

이 수정은 Control 팀 소유 코드에서 진행하며 Agent가 operation을 ToolId에 맞춰
왜곡해서 우회하지 않는다.
