# Teammate dev read-only review — 2026-10-16

비교 기준은 동결된 로컬 런타임 `6629f90e`와 `origin/dev` `7037d56f90207973054b7c3661228ce29fb7a48a`다. 현재 HEAD `fed756c6`는 6629 이후 추적된 문서 두 곳만 바뀌었다. 아래의 누락·차이는 `de799df3` 이후 로컬에서 추가한 작업과의 분기이며, 이를 팀메이트가 삭제하거나 회귀시켰다는 뜻은 아니다.

## 실제 변경 이력

`de799df3..origin/dev`의 non-merge 커밋은 세 개다.

- `a5890f0ab9233df01de4fbe38c93209479f528f8` — planner/worker 프롬프트에서 Kali를 선택적으로 쓸 수 있게 한다.
- `92ec7f6f5b96902aed8f9b0b847fd4033f090af5` — 요청별 Planner 입력 원칙, worker 대화/session 계약과 구현, 관련 복구·테스트를 추가한다. `worker_session.py`, `worker_conversation.py` 및 `nodes/react.py` 연결이 핵심이다.
- `a020b09c720c71936755f290778c1674abe43b1c` — Kali HTTP/workroom·profile과 도구 준비, 실행 투영·Finding·보고/그래프 UI를 추가하고 worker의 Kali 선택 지침을 강화한다.

`f13fdf3d`는 `92ec7f6f`와 `de799df3`를 부모로 하는 reconciliation merge이고, `7a222533`/`7037d56f`는 PR #37/#38 merge다. 따라서 diffstat의 159개 파일, +11,821/-822는 세 non-merge 변경이 반영된 최종 트리 차이이며 단일 커밋의 새 코드량은 아니다.

## 통합 시 보존할 로컬 계약

| 영역 | 확인된 코드 차이와 영향 | 권고 |
|---|---|---|
| Planner 조회 계약 | 로컬은 `context_request` v1 단일 조회와 v2의 1–4개 배치 조회를 구현한다 (`6629`: `planner_context.py:68–88,395–408`; `test_planner_context_batch.py`). origin은 v1만 허용한다 (`origin/dev`: `planner_context.py:56–66` 및 `hypothesis_planner.py:576`, 두 planner prompt도 v1만 안내). 각 브랜치 안에서는 일관되지만 parser·prompt를 한쪽에서만 가져오면 요청 형식 계약이 어긋나거나 v2 기능이 사라진다. | 코드를 옮길 때 validator, prompt, 출력 schema, contract/profile 버전을 함께 맞춘다. 로컬 v2 지원을 유지한다면 origin의 v1-only parser/prompt를 그대로 덮어쓰지 않는다. |
| Worker 도구 선택 | `a5890f0a`의 “Kali를 자유롭게 사용, 의무는 아님”은 선택적 사용이다. 뒤의 `a020b09c`는 사용 가능한 Kali Bash가 겹치는 검사에 맞으면 단일 목적 도구보다 먼저 선택하라고 한다 (`origin/dev`: `worker.md:36–44`). 로컬 한국어 worker는 목적에 따라 도구를 고르고 특정 도구군을 기본 선호/회피하지 말라고 한다 (`6629`: `worker.md:22–24`). 이는 지침의 직접적인 선택 우선순위 충돌이다. | 대화/session 지침은 흡수하되, Kali 우선 정책은 로컬 목적 우선 규칙과 명시적으로 조정한다. |
| 승인 대기 actor 재개 | origin driver의 `_resource_builders`는 blocked 사유가 `resource:`인 작업만 모은다 (`origin/dev`: `instance_driver.py:792–793`); origin `central_state.py`에는 `reserve_approved_actor`가 없다. 로컬 `138b1f0d`는 `approval:` 대기도 재검사하고 정확한 승인 invocation 집합을 CAS 예약한 뒤 원래 actor/checkpoint를 이어간다 (`6629`: `instance_driver.py:811–853,905–920`, `central_state.py:1661–1716`). | 이는 로컬의 de799 이후 변경이다. 신규 worker-session/checkpoint 경로와 합칠 때 승인 actor 재개를 보존하고 기존 continuation 경로의 테스트를 통합 후 다시 확인한다. |
| 원본 session/account 증명 | 로컬은 비교 요청의 각 leg가 자기 원본 receipt로 요청된 session과 일치하는지 검사하고, 같은 leg의 account/session 쌍만 인정한다 (`6629`: `analysis.py:535–625`, `completion.py:1299–1374`; 후속 로컬 proof 커밋 포함). origin 최종 트리는 aggregate `collect_actual_accounts`/`collect_actual_sessions` 방식이다 (`origin/dev`: `analysis.py:506–549`, `completion.py:1298–1353`)이며 로컬의 per-unit matcher가 없다. 이는 팀메이트 회귀가 아니라 로컬 후속 proof 변경과의 분기다. | 통합 시 로컬 exact-unit 검증과 관련 회귀 테스트를 유지하고, 신규 Finding/report 투영은 그 검증된 원본을 소비하게 한다. |

## 새 worker-session 연결의 호환 조건

origin의 `WorkerSession`은 별도 저장소 대신 기존 instance checkpoint에 들어가며 transcript, 초기 입력 표시, schema와 provider binding을 보존한다 (`worker_session.py:171`; `worker_conversation.py:83–116`). binding은 composed prompt/tool/provider hash를 포함하고 복구 시 system prompt가 달라지면 `worker_session_prompt_changed`로 거부한다 (`worker_session.py:39–73`; `worker_conversation.py:90–116`). 로컬 한국어 prompt/profile을 합친 뒤 기존 세션을 재개하는 경우 이 hash 경계를 고려해야 한다. 변경된 prompt를 같은 세션으로 조용히 이어갈 수 없으므로, 버전·재개·hold 동작을 하나의 계약으로 정한다. 이 확인은 코드 경계 검토이며 실제 resume 실행은 하지 않았다.

읽기 전용 diff/source 검토만 했다. 테스트, live Run, fetch, 통합, cherry-pick, commit은 수행하지 않았다.
