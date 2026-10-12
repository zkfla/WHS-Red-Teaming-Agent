# 최종본 출하 — 타 팀 협업 필요 항목 (Capability / Agent / PM)

작성일: 2026-10-02 (재작성)
기준: **최신 `dev` HEAD `6978596`** 전수 재확인
작성: Agent 팀 (이전 Control)

> 범위: **Control 팀 항목(ROE·Policy 집행, 승인 알림, selected-tool risk, Scope 하드닝 등)은 제외** — Agent 팀(이전 Control)이 직접 처리/추적. 이 문서는 Capability / Agent(Orchestration) / PM·공통계약 대상.

> 재확인 결과 **이전 판본에서 지적했던 상당수가 최신 dev에 이미 반영됨**(§3). 실제로 남은 협업 항목은 아래 3건뿐이다.

---

## 1. 최신 dev 기준 — 아직 열려 있는 항목

| # | 항목 | 위치 | 소유 | 왜 문제 (최종본 관점) |
|---|---|---|---|---|
| D2 | **MCP 서버 목록이 비면 `ExampleMCPAdapter` 자동 부착** | `capability/bootstrap.py:95` | **Capability** | `app.yaml`이 `mcp_servers: []`라, 서버 설정 없이 `redteam run`하면 in-process 예제 도구로 "도는 것처럼" 보임. 의도된 설계지만 최종본 기본값으론 silent fake 위험. |
| S1 | **API discovery seed 없음** (`seed_endpoints`/`bootstrap_paths`) | `core/contracts/engagement.py` (필드 부재) | **PM·공통계약** | 루트 404 API/SPA에서 Agent가 탐색 시작점이 없어 `no_actionable_plan`로 조기 종료 가능. operator-approved seed 주입 필요. |
| A1 | **`EvidenceRecord.artifact_ref`가 main 경로에서 `None`** | `core/convert.py:100`, `evidence/service.py:115` | **PM(포트)·Capability(생산)** | 백엔드(`evidence/artifacts/FilesystemArtifactBackend`)는 있으나 `ToolResult→Artifact→Evidence` 연결이 비어, 원본(HTTP 응답/스캐너 출력/캡처) 재현 체인이 약함. (Evidence 소비 측은 Control이 별도 처리) |

추가(공동): **도구 end-to-end 검증 매트릭스**. 최신 dev에 e2e 통합 테스트가 늘었지만(`test_engine_live_run_flow`, `test_llm_candidate_generation_loop`, `test_principal_mcp_transport_e2e`, `test_merged_agent_runtime` 등), TLS/OpenAPI·OIDC/session comparison/proxy replay/browser/active recon/mobile 일부는 **도구별 1-바퀴 검증 확인 필요**: `후보생성 → Compiler → Control 판정 → MCP 호출 → output schema → Evidence → Mapper → Graph update → Replan`.

---

## 2. 팀별 협업 요청

### Capability 팀
- **D2 기본값 결정.** `--demo`/`mode: demo`에서만 `ExampleMCPAdapter` 허용하고, 일반 실행에서 `mcp_servers`가 비면 **명확히 실패**(silent fake 제거).
  - ⚠️ 이 동작에 CLI·통합/단위 테스트가 다수 의존 → **Capability가 테스트까지 함께 수정해야 안전.** 타 팀 단독 변경 금지.
  - 저위험 즉시 대안: 폴백 시 **경고 로그 한 줄**(동작 불변, 함정만 제거).
- **A1(생산 측):** Tool 실행 산출물을 `ArtifactBackend`에 저장하고 그 ref를 `ToolResult`로 흘려 `artifact_ref`가 채워지게.
- 도구 e2e 매트릭스 확대(위 공동 항목).

### Agent / Orchestration 팀 (우리)
- **대부분 이미 dev에 반영됨**(§3) — 특히 LLM candidate wiring은 완료. **신규 주작업 없음.**
- 남은 몫: **S1 seed endpoint의 소비 로직**(계약이 PM에서 확정되면 Agent 탐색이 seed를 읽어 "승인 후보"로 축적; 임의 경로 창작 금지, MCP policy가 최종 집행).
- 도구 e2e 매트릭스 확대(Capability와 공동).

### PM / 공통 계약 (`core/contracts`·`core/ports`)
- **S1: `seed_endpoints`/`bootstrap_paths` 계약 필드** 신설. operator-approved discovery seed(예: `/openapi.json`, OIDC discovery).
- **A1: ArtifactStore 공통 Port 승격 + `ToolResult`의 artifact ref 전달 규격.** (Control PLAN.md §5 PM Issue 후보와 동일 건)

---

## 3. 최신 dev에 이미 반영됨 — 재작업 불필요

이전 판본/외부 리뷰가 "미해결"로 적었으나 **최신 dev에 존재**. 다시 만들지 말 것:

| 과거 지적 | 최신 dev 상태 | 근거 |
|---|---|---|
| 기본 LLM이 fake | **`litellm` 기본** | `core/config.py:66` `provider="litellm"`, `app.yaml` |
| LLM candidate 생성 미연결 | **연결·활성** | `orchestration/settings.py:86` `llm_candidate_generation=True`, `engine.py`/`candidates/compiler.py` wiring, `test_llm_candidate_generation_loop.py` |
| MissionPlan/TaskGraph/OperatorIntent 없음 | **존재** | `core/contracts/mission.py`, `core/ports/task_graph.py`, `orchestration/task_graph/`, `orchestration/operator_intents/`, `mission_bootstrap.py` |
| 완료/success criteria 평가 없음 | **존재** | `orchestration/replanning.py` `_evaluate_criterion`, `test_terminal_mission_criteria.py` |
| reconciliation 진입점 없음 | **존재(개편됨)** | `orchestration/recovery.py`(구 `reconciliation.py`), `runner.reconcile_once` |
| ArtifactStore 백엔드 없음 | **존재** | `evidence/artifacts/`(`ArtifactBackend`, `FilesystemArtifactBackend`) — 단 `artifact_ref` 연결은 §1 A1 참고 |

---

## 4. 우선순위 (비-Control)

1. **D2 기본값 결정** (Capability+PM) — silent fake 제거. 경고 로그부터라도.
2. **S1 seed_endpoints 계약** (PM) → Agent 소비 — 루트 404 dead-end 해소.
3. **A1 artifact_ref 연결** (PM 포트 + Capability 생산) — 재현성.
4. 도구 e2e 매트릭스 확대 (Capability+Agent).

---

_Control 항목은 Agent 팀이 직접 처리하며 이 문서 범위 밖. 기준 HEAD가 `6978596`에서 바뀌면 재확인 1회 필요._
