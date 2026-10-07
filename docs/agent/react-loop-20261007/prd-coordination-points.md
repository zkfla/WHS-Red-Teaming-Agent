# PRD 조정점 — specialist-worker-parallelism

기준 문서: prd-specialist-worker-parallelism-20261006 (1).md (구현 전 설계 초안).
이 문서는 구현이 아니라, 이번 ReAct 전환과 팀 PRD가 맞닿는 지점의 조정 요청 목록이다.
PRD의 예정 기능을 현재 구현으로 취급하지 않는다.

## 조정점

재개 시 PRD 원문을 다시 대조했다. 아래는 설계 접점이며 팀원 현재 구현 상태의 확인은 아니다. 새 7역할·Planner/Atomizer·기동 시스템·프롬프트 로더를 이번 전환에서 구현하지 않는다.

1. 가시성과 허용의 분리. purpose-scoped 도구 가시성은 모델에 보여줄 목록이며 기존 실행 허용(readiness, Control admission, 범위·승인)을 대체하지 않는다. 해당 문구가 나오는 곳마다 한 줄 명시가 필요하다.
2. 스케줄러 조건. WorkScheduler.lease_batch는 현재 의존·충돌·admission 검사 없이 순서대로 lease한다. READY 6조건은 스케줄러 이행 후에만 보장된다는 단서가 PRD §2·§8·§13에 필요하다.
3. 승인 대기 형제 처리. 단일 승인 pending 시 형제 호출을 보류하고 원래 호출 ID로 복구하는 현재 동작(worker_node + runner 재개)을 유지 전제로 적시하고, 병렬 다중 승인 큐는 후속으로 분리한다.
4. 역할/프롬프트 자산. specialists.yaml·MD 로더·7역할은 미구현(설계 제안)이다. role manifest에 도구 허용 목록을 중복 저장하지 않고 tools_for_role/resolve_role_selection 기준을 유지한다.
5. 이벤트 선행 조건. PRD §18.1의 사건명·깨울 역할 표는 설계 제안이며, 기존 이벤트 enum·EventWakeup 확장과 어댑터 연결이 선행해야 한다.
6. 예산 분리. 판단/실행/검증 reserve/시간/토큰 한도는 기존 verification_reserve·위임 한도 이행 명세와 묶어 확정한다.
7. 완료 게이트 의미. completed는 질문·작업 완료이며 취약점 존재가 아니다. candidate_findings는 검토 전 가설이고, Planner 제안만으로 종료하지 않는다.
8. Native ReAct 접점. 일반 도구 호출은 path/step 승인 없이 Control admission과 작업·목적 출처(engine.validate_current_action의 native 분기, work 행의 mission_task_id·task_purpose_hash)로 실행된다. AtomicTask/READY 조건이 도입되면 이 native admission과 충돌하지 않도록 조정이 필요하다.
9. 정산 이벤트 소유권. TASK_COMPLETED/MISSION_UPDATED는 semantic_completion의 실제 상태 정산 뒤 발행한다. legacy plan 노드의 별도 diff 발행은 제거했다. 역할 기동 시스템은 일반 tool 성공과 이 정산 이벤트를 구분해야 한다. LocalToolContext/Result는 선택적 last_event_id를 전달하며 native/legacy 소비자는 같은 인과 체인을 사용한다.
10. 인증 관측 비교. 기존 authentication 담당의 저장 원본 비교를 선택적 runtime tool로 공개하는 수정은 조사 역할을 추가하는 것이 아니다. 일반 Web 호출에 공통 authentication 주석이나 추가 모델 심사를 요구하지 않는다. 최종 finding은 기존 graph.record의 목적·원본 검토를 사용한다. 팀원 Research/Evidence와 후속 연결 시 비교 결과·원본 refs와 확정 finding을 구분해야 한다.

## 이번 작업이 선점하지 않은 것

전문 역할 확장, Planner/Atomizer 분리, 이벤트 기반 역할 기동, 프롬프트 로더, 도구 구현.
