> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# 상태·저장소 · Control 영역 모듈화 런타임 조사 결과

- 작성: 외부 오케스트레이터(DeepSeek 4.1 Flash Max), 워커 2명(Muse 1.3 Contributor xhigh) 교차검토 후 통합
- 조사 시점 커밋: 9eb70ed1f9fff1fbf15540731de87de89feaf7fe (origin/dev, PR #29 merge) — 감사 자료 3종과 같은 커밋
- 저장소: .
- 성격: **읽기 전용 정적 조사**. 이 문서 작성 전후로 제품 소스·테스트·Git·설치 상태를 변경하지 않았다. 테스트는 실행하지 않았다.
- 입력 자료: docs/agent/modularization-20261004/2026-10-04-WHS-modularity-coupling-audit.md(M01~M30), 사용자 제공 자료/dev-control-audit-20261004.md(C-01~C-05), 사용자 제공 자료/dev-agent-system-audit-20261004.md(F01~F13)
- 지위: 이 문서의 계약 제안·책임 지도는 **제안**이다. 구현은 루트 지시 후에만 진행한다. 별도 언급이 없으면 file:line은 이 커밋 기준 확인값이다.

## 0. 조사 방식과 근거 수준

- 워커1(상태·저장소·경로·과제·작업·체크포인트·이벤트/outbox·원자적 조정)과 워커2(Control admission·승인재개·재시도·대상 신원·증거/그래프·완료)가 담당 소스를 직접 읽고 file:line 근거로 보고했다.
- 오케스트레이터가 두 보고의 핵심 주장과 행번호를 실제 소스로 재대조했고, 아래 11절에 정정 내역을 분리했다.
- 구분 표기: [확인] 정적 소스 대조 완료 / [정정] 자료·보고와 실제가 다름 / [미검증] 실행 재현 없음 / [운영한계] 결함이 아니라 현재 설계 한계 / [정상] 의도된 검사.

## 1. 핵심 결론

1. **저장 변경 세션(M20)은 실제로 닫혀 있다.** 새 저장소가 공개 포트만 구현하면 원자적 조정자가 존재 자체를 인정하지 않는다. 조정자는 구체 클래스 6종(메모리/SQLite 과제·경로, 메모리/SQLite 지시 이력)만 받고, 사설 속성·사설 메서드에 직접 접근한다. 가장 작은 이관은 **단일 복합 제공자**(Task/Path/records 소유)의 공개 change(callback) 계약을 orchestration 내부 공개 port로 올리고, Coordinator는 change와 bind/validate만 소비하는 것이다. 구체 Memory/SQLite 선택과 같은 conn·공통 lock·records 저장은 제공자 내부가 소유한다.
2. **admission 공통화는 실제로 필요하다.** 승인 대기, 세마포어 대기, 재시도 시점에는 ROE 기간·허용 시간대·기간 예산·동시성이 재검사되지 않는다. 단, 최초 허용 1건의 회계(속도 1슬롯 + 예산 1회(행동+도구호출 1))는 evaluate 또는 approval_outcome에서 이미 발생하므로, 호출 직전 재확인은 **비과금·멱등**이어야 하고, 재시도분만 별도 원자 예약으로 1회 과금해야 한다. 이 구분을 어기면 같은 행동에 이중 과금이 생긴다.
3. **그래프 edge 신원은 실제로 깨진다.** edge 식별자는 네임스페이스 이전의 출발/도착으로 조립되어 공유 저장소에서 다른 Engagement의 동일 관계를 덮어쓴다. 래퍼에서 edge 식별자까지 양방향 변환 + 기존 SQLite 데이터 재색인/검사가 필요하다.
4. **대상 신원의 authority가 세 곳에 흩어져 있다.** 범위 판단(IPv6 브래킷·스킴 기본 포트), 그래프 신원(host/port 문자열 조립), 관측 매퍼(문자열 동일 비교)가 각자 규칙을 가진다. F01(포트 포함 관측 누락)은 그 결과다. host+port+scheme 정규화·비교의 단일 authority를 공통 계약에 두고 세 소비자가 그것을 쓴다.
5. **무증거 요약 단계 함정은 등록 시 교정으로 닫는다.** 기존 Task/Goal 완료가 이미 집계를 담당하므로 새 aggregation kind/receipt 엔진은 이번 필수 범위가 아니다. 실행 불가능한 필수 단계는 register_path/작성 검사에서 correction과 함께 거절하고 정상 Task/Goal 완료로 연결한다.
6. **구조 변경만으로 결함이 저절로 사라지지는 않는다.** C-01~C-05, F01~F05는 이 커밋에서 (감사자 측 오프라인 재현으로) 조건이 확정된 결함이고, 계약·배선·마이그레이션이 각각 필요하다. 반면 rate_limit의 in_flight=None 미적용, 승인 outcome 캐시, unsupported_roe_policy의 ABORT 등은 의도된 정상 검사/운영 한계다.

---

## 2. 저장 변경 세션 공개 계약 (M20) — 가장 작은 이관안

### 2.1 실제 현재 상태 [확인]

공개 포트(전부 Protocol):

- 임무·과제: src/redteam/core/ports/task_graph.py:13 MissionStore, :22 TaskGraphStore, :45 MissionTaskStore(합성).
- 경로: src/redteam/orchestration/paths/store.py:10 PathStore — get_active/list_paths/register/update_step.
- 작업 장부: src/redteam/orchestration/work/store.py:12 WorkStore — add/get/list_items/acquire/heartbeat/complete/release/reclaim_expired/bind_job/reconcile_job/abandon_run.
- 운영자 지시 이력: src/redteam/orchestration/operator_intents/store.py:27 OperatorIntentStore — append/update/get/list_for_mission.
- 사건: src/redteam/core/ports/event_bus.py:18 EventBus — publish/subscribe만. 이력 조회는 포트가 아님.
- 증거: src/redteam/core/ports/evidence_store.py:14 EvidenceStore — append/get/list_for_run/list_for_action.

원자적 조정자(Coordinator)의 실제 의존:

- 생성자 분기: src/redteam/orchestration/coordination.py:127 __init__. :135 InMemoryTaskGraphStore 검사, :136 InMemoryPathStore 검사, :142 SQLiteTaskGraphStore 검사, :144 SQLitePathStore 검사, :145-146 같은 SQLite 파일(경로 resolve 비교) 강제, :150 그 외 전부 atomic_store_configuration_unsupported로 거절.
- 경로 지연 결합: coordination.py:152 attach_paths — :161-164 SQLite면 같은 파일+SQLite 경로, :166-168 메모리면 메모리 경로만 허용.
- SQLite 트랜잭션: :171 _transaction — :174 사설 _connect, :176 BEGIN IMMEDIATE, :181-184 orchestration_records DDL, :186-187 스키마 버전 검사, :191-193 사설 _load_memory 2종으로 전체 스냅샷 적재, :200-203 콜백은 분리 메모리에서만, :204-206 사설 _save_memory 2종, :207-212 records UPSERT, :213 커밋.
- 메모리 변경: :221 change — :230-236 사설 dict 6종 deepcopy(_missions/_tasks/_snapshots/_progress/_active/records), :239-247 await 없이 통째 교체.
- 조정자 캐시/검증: :285 coordinator_for(과제 저장소의 _orchestration_coordinator 속성에 조정자 부착), :295 validate_configuration — :302-305 지시 이력 저장소가 조정자 모드와 짝이 아니면 atomic_intent_history_configuration_unsupported.

조정자 소유 기록(records)의 실제 키 [확인]:

- ("binding", run_id), ("generation", "run:mission"), ("admission", action_id), ("intent", intent_id), ("outbox", event_id), ("outbox_sequence", run_id).
- 근거: coordination.py:61-123, engine.py:2584, operator_intents/service.py:616/738/832(및 546-548), outbox.py:52.

조립·호출부 [확인]:

- bootstrap.py:380-391에서 validate_configuration 후 coordinator.change(initialize).
- 작업 장부는 조정자 밖: bootstrap.py:308-311에서 별도 생성, :283/:518/:557에서 주입. 작업 장부는 조정자 원자 세션에 참여하지 않는다. [정정: 이 사실은 M21의 범위를 좁힌다 — 작업 장부 규칙 중복은 별개 문제다]
- 저장소 파일은 SQLite일 때 과제·경로가 같은 settings.task_graph_path를 쓴다: bootstrap.py:296/:304.
- SQLite 실행은 단일 워커 스레드 위임: orchestration/sqlite_support.py:94 run_sqlite — 콜백이 트랜잭션을 소유·롤백하고 3회 재시도. 메모리 경로는 asyncio 단일 루프에서 락+무await 교체.

### 2.2 확정된 계약 형태 (루트 결정 반영)

- 각 참여자가 begin_change→commit을 따로 하는 per-participant API는 채택하지 않는다. atomic_scope가 같아도 별개 연결·순차 commit이면 단일 원자성이 보장되지 않기 때문이다.
- 대신 **하나의 복합 저장 제공자**(Task/Path/records를 함께 소유)가 복합 변경 세션을 열고 단일 commit/rollback을 수행한다. Coordinator는 공개 backend.change(callback)와 bind/validate 역할만 소비한다.
- 기본 Memory/SQLite 제공자 내부가 기존 규칙을 소유한다: SQLite는 같은 커넥션 + BEGIN IMMEDIATE, 메모리는 공통 asyncio.Lock + 무await 교체, records 저장도 제공자 내부. 즉 현재 coordination.py의 트랜잭션·스냅샷·records 로직은 제공자 내부 구현으로 이동한다.
- 공개 port는 같은 팀 orchestration 내부의 공개 계약으로 둔다. core 승격과 Path 모델 전체 이동은 연쇄 변경이 크므로 이번 범위에서 하지 않는다(정책이 아니라 실제 상태 도메인이라 팀 내부 계약으로 충분).

계약 골격:

    class AtomicStateBackend(Protocol):
        async def change(self, callback: Callable[[ChangeSet], Awaitable[T]]) -> T: ...
        # 진단·호환용(선택): def atomic_scope(self) -> Hashable: ...

    class Coordinator:  # 얇은 파사드
        async def change(self, callback): return await self.backend.change(callback)
        # bind/validate: 기존 오류 코드·의미 유지

계약 의무(명시 필수):

1. backend.change(callback)가 단일 원자 경계다. SQLite면 한 커넥션·BEGIN IMMEDIATE 안에서 Task/Path/records를 적재하고 콜백 실행 후 함께 커밋/롤백한다. 메모리면 공통 lock 아래 deepcopy 스테이징 후 무await 스왑한다. 부분 반영이 남으면 실패다.
2. 콜백은 기존 ChangeSet(공개 모델 기반 detached 뷰 + records 매핑)만 본다. 외부 I/O(LLM·도구·발행) 호출 금지 — 기존 독스트링 의미 유지.
3. 구체 Memory/SQLite 선택은 조립 팩토리에서 한 번만 일어난다. Coordinator 본문에 구체 타입 isinstance와 사설 접근(_missions/_tasks/_snapshots/_progress/_active/_lock/_connect/_load_memory/_save_memory/_path/_merged, records dict 직접 소유)이 남으면 불합격이다.
4. records 키·JSON 포맷·스키마 버전 검사 유지. 작업 장부·증거·체크포인트는 이 경계 밖(현행 유지).
5. 이력 저장소 짝 검사(기존 validate_configuration:302-305 의미)는 제공자 구성 검사로 흡수하거나 명시 검사로 유지한다.

### 2.3 이관 순서(최소)

1. orchestration에 내부 공개 port(AtomicStateBackend) + Coordinator 파사드 추가. Coordinator는 backend.change만 호출.
2. 기본 제공자 구현(예: orchestration/state_backend.py 1개, 또는 제공자 패키지): SqliteStateBackend/MemoryStateBackend가 기존 _transaction/change 로직·records 저장·스냅샷 적재를 소유(같은 conn/공통 lock 유지). "바뀐 행만 저장" 보존.
3. 제공자 팩토리(state_backend_for(tasks, paths))가 조립에서 한 번만 구체 선택. coordinator_for/validate_configuration은 팩토리와 진단만 담당.
4. 검증(구현 승인 후, 마지막): 공개 port만 구현한 테스트 백엔드로 change 성공, Task+Path+records 단일 커밋/롤백, 실패 주입 시 부분 반영 0, coordination.py에 구체 저장소 import 0, 기존 coordination/task_graph/path/operator_intents 테스트 통과.
5. 표면 리팩터 금지: 계약 선언만 하고 기존 isinstance/사설 경로를 남기면 불합격. 이 항목이 리뷰 체크다.

### 2.4 위험·반례

- 타입 검사만 지우고 원자성 없이 풀면 과제/경로가 따로 커밋되어 중간 상태가 읽힌다. M20 해결 방향의 경고 그대로다.
- SQLite는 전체 스냅샷 load/save 구조라 데이터가 커지면 느리다. 점진 저장으로 바꾸면 "바뀐 행만 저장" 최적화를 다시 설계해야 한다. 성능·원자성 트레이드오프는 구현 시 결정.
- records JSON 페이로드 포맷은 기존 DB와 호환되어야 한다.
- coordinator_for 캐시는 제공자 키 registry로 유지한다 — 이중 조정자 생성이 원자성을 깨는 것을 막는 장치라서 없애면 안 된다.
- 복합 제공자가 Task/Path/records를 함께 소유하므로, 향후 경로 저장소만 따로 교체하는 조합은 제공자 단위 교체로만 가능하다(의도된 제약, 문서화).

## 3. 작업 장부·체크포인트·구성 버전·이벤트 이력·저장 경계

### 3.1 작업 장부 규칙 중복 (M21) [확인]

- 메모리 구현과 SQLite 구현이 등록·임대·충돌·종료 판단을 각각 갖는다: src/redteam/orchestration/work/memory.py:25-66(등록/중복), :117-164(임대/하트비트), :328-356(종료/회수) vs work/sqlite.py:83-146(등록), :228-(임대), :44-79(색인·부분 유니크).
- 공통화 경계(제안): 순수 전이·충돌·종료 판단만 새 파일 work/rules.py로 내리고, memory/sqlite는 잠금·검색·기록 담당. SQL의 run_id+candidate_key 조회, 우선순위/상태 색인, 부분 유니크는 유지하되 공통 의미와 일치하는지 테스트로 고정.
- 반례: task_graph는 이미 rules.py 공유+SQLite의 메모리 규칙 재사용(task_graph/sqlite.py:122-143)이라 "모든 저장소 항목 추가가 전면 수정"은 틀렸다. 작업 장부만 해당한다.
- 작업 장부는 조정자 참여 대상이 아니므로 M20 이관과 묶지 않는다.

### 3.2 체크포인트·구성 버전 (M22, M23) [확인]

- 체크포인트 허용 타입은 패키지 스캔으로 파생: orchestration/checkpointing.py:24 STATE_PACKAGES(core/attack_graph/orchestration), :40-72 walk_packages+동적 import, :70-71 Pydantic/Enum만 수집, :27-36 스캔 중 ImportError는 loud 실패. 외부 등록 API는 없다.
- 최소 제안: checkpointing.py에 명시적 등록 함수(module, name, version, restore provider) 추가. 미등록 타입은 직렬화 시 조용한 dict 강등이 아니라 명시 거절. 스캔은 유지하되 외부 패키지는 스캔 대상에 넣지 않는다.
- 구성 식별자·버전은 수동 상수: component_binding.py:16-17(내장 프로파일 id/version), :31-36(내장 구성 id 4종), :173-187(저장 기록과 등록 불일치 시 거절, 폴백 없음). 살아있는 객체 주입 거절은 정상: app/runtime_components.py:170-174.
- 최소 제안: 등록 지점에서 내용 해시 또는 계약 버전을 계산해 RuntimeComponentBinding에 싣고, 복원 시 스냅샷/버전 중 하나로 대조. 자동 버전 규율 강제는 운영 피로가 크므로 "등록 선언에 버전 필수 + 불일치 시 명시 오류"가 균형점.

### 3.3 이벤트 이력 (M29) [확인]

- 공개 포트는 발행·구독만(event_bus.py:18-21). 이력은 메모리 구현의 비공식 기능: event_bus/memory.py:50("not part of the port"), :58 history(디버깅/테스트용).
- 운영자 설명 기능이 구현체에서 이력을 선택적으로 찾는다: operator_intents/service.py:1251-1275 _event_history, :1254-1258 history 없으면 빈 리스트. 실행은 안 깨지고 진단 자료만 빈다. [정정: 이 폴백은 의도된 방어이므로 "필수 이력 API"로 승격하면 과도 결합이다]
- 최소 제안: 이력 조회는 별도 Protocol(예: event_bus.py에 EventHistoryReader 추가 또는 신규 파일)로 분리 주입. 실시간 포트는 그대로.

### 3.4 저장 경계 보완 (F13) [확인]

- git ls-files 기준 .realrun/audit 7개, .realrun/evidence 7개+_index 1개, 총 15개 JSONL이 추적된다.
- 이는 결함 확정이 아니라 운영 정책 결정 사항이다: 합성 fixture와 운영 raw artifact 분리, 추적 해제(.gitignore) 여부는 운영/보안 결정. 실제 비밀값 유출은 확인하지 않았다.

## 4. admission 공통화 (C-01/F03, C-02/F04, C-04, M17)

### 4.1 실행 허용 검사 전수 [확인]

| 시점 | 위치 | 재검사하는 것 | 빠진 것 |
|---|---|---|---|
| 최초 판단 | control/service.py:131-249 | 범위(:135) → ROE(:143, in_flight 문맥 :141) → 위험/정책(:161) → 속도 비소비 조회(:191) → 예산 여유(:205) → 승인대기 무과금(:220-237) → ALLOW 과금(:240 속도 1, :249 예산 1) | 없음(이 시점 기준) |
| 승인 확정 | service.py:329-431 | 캐시 멱등(:331-333), 속도 조회(:387)/확정(:416), 예산 확인(:403)/소비(:428). 범위/ROE/정책은 "결정적"이라며 스킵(:373-378) | ROE 시작/종료(:roe.py:142-153), 허용 시간대(:168-176), 기간 예산, 병렬 상한 |
| 실행 직전 | nodes/execute.py:117-120 → engine.py:2667-2789 | 경로 버전·단계·의존·연산자 세대·대상 봉투·도구 바인딩·제안 정합 | 시간·기간·병렬 전부 |
| 재시도 예약 | nodes/execute.py:319-329 (+ policy_lock :326) | 도구호출 잔여 개수만: service.py:610-627 | 기간 예산, ROE 시간, 병렬 |
| 러너 승인 재개 | runner.py:745-845 | 체크포인트 정합(:769-795), 승인 확정(:797), 지속 중복 클레임(:818-845) | ROE 시간(승인 확정 경로가 스킵) |
| 동시 실행 상한 | execute.py:163-167 세마포어 | 실행 병렬 수(대기열 포함) | 대기 중 기간 경과 재검사 없음 |

시간 의존 조건의 위치: roe.py:142-153(UTC start/end), :168-176(허용 윈도우), :192-215(max_parallel — in_flight 없으면 닫힘 실패 :201-207), budget.py:75-87(첫 여유 확인에 앵커 고정), rate_limit.py:109-119(동시성 — in_flight 없으면 미적용, 구버전 직렬 경로 호환). 두 "신호 없음" 정책이 정반대이므로 계약에 각각 명시해야 한다(M17).

승인 outcome 캐시: service.py:120-129 설명 + :331-333. 프로세스 내 무기한이며, 시간 조건 재검사 앞에서는 이 캐시가 ALLOW를 그대로 돌려준다 — C-01의 본질. [미검증: 실행 재현은 감사 측 오프라인 재현 결과이며 이 워크스페이스에서 재실행하지 않음]

### 4.2 오늘의 회계 위치와 이중 과금 위험 [확인]

- 허용 1건의 단일 과금은 두 경로 중 하나에서만 발생한다: evaluate ALLOW(:240/:249) 또는 승인 확정(:416/:428). budget.consume() 1회 = 행동 1 + 도구호출 1(budget.py:89-92). 승인 대기·DENY는 무과금(service.py:235-236).
- 재시도는 도구호출 카운터만 추가 소비(execute.py:326-329, budget.py:94-106) — 정당한 추가 호출분.
- 위험: 호출 직전 공통 admission을 "과금 포함"으로 추가하면 evaluate/승인확정에서 이미 낸 속도 슬롯+예산을 한 번 더 낸다. 승인 확정 캐시는 approval_outcome 재호출만 막지, 새 admission 메서드의 과금은 막지 않는다. 팬아웃·세마포어 대기 후 재확인까지 겹치면 이중 과금이 더 쉽게 튄다.

### 4.3 최소 공개 계약(제안)

control_plane.py에 두 메서드를 추가한다(기존 evaluate/approval_outcome 시그니처·의미 유지).

    async def admit_invocation(
        self,
        proposal: ActionProposal,
        decision: PolicyDecision,
        context: ControlContext,
        *,
        receipt_id: str,
    ) -> PolicyDecision:
        """호출 직전 재확인. 과금 없음. 기존 승인 의도/ID 보존."""

    async def reserve_tool_call(
        self,
        engagement_id: EngagementId | str,
        *,
        receipt_id: str,
    ) -> ToolCallReservation:
        """재시도용 추가 호출 1건을 원자 확인(개수+기간+ROE 시간/병렬) 후 1회 차감."""

의무:

1. admit_invocation: decision.verdict == ALLOW + action_id 일치 + proposal/context engagement·agent 정합(C-04 불변량)을 fail-closed 검사. 재검사 대상은 범위 + ROE(UTC now, in_flight 문맥) + 속도 비소비 조회 + 기간 예산 여유. 새 승인 발행 금지, approval_id 불변, 과금 금지. receipt_id로 멱등. 컨텍스트 불일치는 무과금 DENY.
2. reserve_tool_call: 도구호출 잔여 + 기간 + ROE 시간/병렬을 한 호출에서 확인·차감(원자). receipt_id 멱등(같은 키 재호출은 재차감 없이 동일 예약 반환). 거절 시 사유 코드(기간 종료/ROE 종료/개수 소진/병렬 초과)를 반환해 CLI·이벤트가 사용자에게 그대로 전달.
3. 시간은 서버측 시계(ROE는 UTC, 기간은 monotonic)를 기본으로 하고 now 주입은 테스트 전용으로 제한한다. 호출자가 시각을 공급하는 계약으로 만들면 우회 통로가 된다.
4. in_flight는 orchestration이 Work Ledger로 계산해 문맥에 정식 필드로 싣는다(현재 policy node :132-141이 계산, metadata {"in_flight_actions"} 전달). 부재 의미는 ROE 병렬이면 닫힘 실패, 속도 동시성이면 미적용(문서화). M17.
5. 기존 has_tool_call_capacity/record_tool_calls는 유지하되 reserve_tool_call로 구현해 이중 경로를 없앤다(구버전 호출 호환).
6. approval_outcome의 캐시·소비 구조는 **그대로 둔다.** 시간 재검사는 admit_invocation이 담당한다. [정정: 워커2의 "해결済み 캐시에 TTL" 제안은 캐시가 담당하는 멱등 과금 보호를 약화시켜 이중 과금을 재도입할 수 있다. TTL은 승인 결과의 유효기간이 필요하다고 루트가 확정할 때만, receipt와 함께 설계한다]
7. 배선: execute.invoke() = validate_current_action → claim_action_admission → admit_invocation → ToolGateway.invoke. 재시도 = reserve_tool_call(정책 락 안) → _invoke_with_semaphore(내부에서 admit 재확인). 러너 재개·정책 노드 재개 경로도 같은 admit을 경유. 승인 대기·세마포어 대기 후 호출 0회가 완료 기준.

### 4.4 위험·반례

- 재확인을 과금 포함으로 만들면 정상 팬아웃 배치가 거부된다(판단 직렬, 실행 병렬 설계: context.py:74-86, bootstrap.py:289/:523). 반드시 비과금.
- 재시도 예약의 receipt 키는 호출 식별자(재시도 회차 포함)로 안정적이어야 한다. 회차 없이 action_id만 쓰면 재시도 2회가 1회로 접힌다.
- 승인 후 ROE 종료로 DENY될 때 실행이 "승인은 됐는데 왜 안 돌지"로 보이지 않게, 종료 사유(기간/ROE)를 종료 상태·이벤트에 그대로 노출.

---

## 5. 대상 신원과 네트워크 authority (M15, F01)

### 5.1 현재 정규화·비교 위치 [확인]

| 위치 | 역할 | 규칙 |
|---|---|---|
| core/contracts/target.py:19-96 | 대상 종류(host/domain/url/endpoint/service/principal/asset/device/graph_node) | 종류별 신원 필드. principal/graph_node는 host 신원 없음 |
| control/scope.py:103-149 _split_host_port | 주소 문자열 → (host, port, ok) | 브래킷 IPv6는 urlsplit, bare IPv6(: 2개 이상 콜론)는 먼저 리터럴 인식, 스킴 기본 포트(86-93: http80/https443/ws80/wss443/ftp21/ssh22), 빈 포트·0 거절 |
| control/scope.py:157-209 _resolve_target | 대상 종류별 (host, port) 추출 | EndpointTarget은 host 문자열과 port 필드 충돌도 거절 |
| control/scope.py:352-471 check_scope | allow/deny 판정 | 빈 allow는 DENY, deny 우선, host/IP/CIDR/domain suffix, asset 인자는 조건부 대조(:361-372), 무신원 종류 거절(:377-385) |
| attack_graph/identity.py:93-94 host_node_id | host 노드 키 | strip+lower만 |
| attack_graph/identity.py:102-115 endpoint_node_id | endpoint 키 | "host:port METHOD path". IPv6 브래킷 없음 — host="::1", port=8088이면 "::1:8088"이라는 그 자체로 유효한 IPv6 표기와 충돌 가능 [미검증: 실행 재현 없음, 구조적 모호성] |
| attack_graph/identity.py:184-189 _url_key | URL endpoint 키 | authority lower + path 정규화 |
| attack_graph/identity.py:192-220 node_id_for_target | 대상→노드 | 종류별 위 함수로 위임 |
| capability 서버 정책(network.py 등) | 실행 직전 origin/CIDR/path 재검사 | 독자 파서. 범위 계약과 별개 신뢰 경계 |

### 5.2 F01 확정 경로 [확인]

- observations/recon_http.py:50-61: HostTarget만 처리, target.host와 output["host"]를 문자열 그대로 비교. target이 "127.0.0.1:8088"이면 output host "127.0.0.1"과 달라 GraphMutation()을 반환 → nodes 0, edges 0. Evidence·일반 실행 사실은 남는다.
- 대상 쪽은 scope가 host:port를 분리하므로 "host" 필드에 포트가 들어오는 것은 정상 입력이다. 즉 매퍼만 authority 규칙이 없다.

### 5.3 최소 계약(제안): NetworkAuthority

위치: core(예: core/contracts/target.py 확장 또는 core/network.py 신설). control과 orchestration 양쪽이 소비해야 하므로 attack_graph·scope 어느 한쪽 소유는 팀 경계를 넘는다.

    @dataclass(frozen=True)
    class NetworkAuthority:
        host: str          # lower, 브래킷 제거, IDN은 입력 그대로(변환은 별도 결정)
        port: int | None   # 1..65535만 유효, 0/음수/초과 거절
        scheme: str | None # "http"/"https"/"ws"/"wss"/... 소문자

        @classmethod
        def parse(cls, raw: str) -> NetworkAuthority | None: ...
        @classmethod
        def from_target(cls, target: AnyTarget) -> NetworkAuthority | None: ...
        def text(self) -> str: ...
            # 포트 있을 때 IPv6는 "[::1]:8088", 그 외 "host:port", 포트 없으면 "::1"/"host"
        def matches(self, other: NetworkAuthority) -> bool: ...
            # host 비교 + (한쪽 포트 없음 = 모든 포트 허용 여부는 호출 정책이 결정) + 스킴 기본 포트 규칙은 범위 판정 전용

소비 규칙:

1. 범위 판정(scope.py)은 이 파서로 통일하고, 스킴 기본 포트 부여는 "범위 비교용"으로 한정한다. 저장·그래프 신원에는 기본 포트를 암묵 부여하지 않는다.
2. 관측 매퍼(recon_http 등)는 from_target과 output host/port/scheme을 각각 파싱해 matches로 대조한다. 문자열 동일 비교 제거. 서버가 host를 host:port로 합치거나 포트 pinning을 삭제하는 식의 임시 수정 금지.
3. 그래프 신원(endpoint_node_id/service_node_id/api_resource_node_id)은 text() 형태를 키로 쓴다. 이때 기존 저장 데이터 호환(현재 비브래킷 조립)이 깨지지 않도록 마이그레이션/호환 읽기를 함께 결정.
4. 회귀(구현 승인 후): IPv4:port, 브래킷 IPv6:port, bare IPv6(포트 없음), 스킴만 있는 URL, 포트 0/65536 거절, 도메인 대소문자.

### 5.4 M15 인자 신원 연결 [확인]

- asset_ref 대조는 조건부다: 인자가 없으면 바로 거절이 아니라 다른 검사로 진행(scope.py:361-372). 새 도구가 다른 인자명으로 자산 신원을 전달하면 같은 대조를 받으려면 중앙 목록 수정이 필요하다.
- 최소 제안: 대상↔도구 인자 연결 선언(예: ToolDescriptor의 target_argument_bindings 또는 기존 asset_argument_bindings 옆 확장)으로 일반 대조. 모르는 대상 기본 거부 유지. 에이전트 역할 식별자와 대상 계정 신원은 계속 별개 축.

## 6. 그래프 Engagement edge 신원 (C-03, F05)

### 6.1 확정 경로 [확인]

- edge 식별자 생성: attack_graph/service.py:105-109 — f"{edge_type}:{source_id}->{target_id}". 재링크 중복 방지가 목적. 저장은 :458-466.
- 네임스페이스 래퍼: stores/namespaced.py:88-96 add_edge — source/target만 prefix, edge.id는 그대로 전달. :75-81 _translate_edge — source/target만 strip, id 불변. :102-110 get_edge — 요청 id 그대로 조회+출발 소유 확인, :103-106의 "출발/도착이 네임스페이스되면 식별자도 전역 유일" 주석은 실제와 다름. :127-130 list_edges — source 소유 필터만. :138-143 remove_edge — id 그대로.
- 결과: 공유 저장소에서 eng_A/eng_B가 같은 관계를 쓰면 edge.id가 동일해 뒤에 쓴 쪽이 앞을 덮는다. 감사 측 오프라인 재현(예상 2, 실제 1)과 일치. [미검증: 이 워크스페이스에서 재실행 안 함]
- 적용 범위: bootstrap.py:468에서 SQLite/memory store 위에 scoped wrapper를 실제로 쓴다. 단일 fresh store 단일 engagement에서는 발생하지 않는다.

### 6.2 최소 수정 + 마이그레이션(제안)

1. 래퍼에서 edge id도 양방향 변환: 쓰기 시 namespaced(edge.id), 읽기 시 stripped. id 문자열 prefix가 node와 같은 구분자()를 쓰므로 make_node_id의 제어문자 거절과 충돌하지 않는다.
2. get_edge/remove_edge는 요청 id를 먼저 namespace로 변환해 조회. list_edges/get_edge의 소유 확인은 유지.
3. 기존 SQLite 데이터: 행 id가 구(비네임스페이스) 형식이다. (a) 마이그레이션으로 f"{type}:{ns}{US}{src}->{ns}{US}{dst}" 재계산, 또는 (b) 읽기 시 구 형식을 감지해 즉시 재색인. 어느 쪽이든 재시작 후 구 id가 되살아나는 미러(sqlite.py:112)를 고려해 저장소 일괄 변환이 안전.
4. export/import/snapshot 경로(service.py:788-835)의 id 변환도 같이 맞춘다.
5. 테스트 보강(구현 후): 두 Engagement가 동일 node ids+동일 관계 저장 → 각 관계 1, base 2, 한쪽 remove가 다른 쪽에 영향 0, memory/SQLite 재시작 모두. 현재 test_graph_tenancy.py:73-88은 A만 쓰고 B가 못 보는지, :119-132는 단건 조회만 본다.

## 7. 실행 단계와 집계 완료 분리 (F02, M24)

### 7.1 확정 경로 [확인]

- 경로 등록은 LLM 초안을 구조 검사 후 수용한다: engine.py:998-1170 — 작성 검사(:1005), 구조(:1025), 가설/증거 실존(:1027-1033), 진행 초기화(:1035-1036), 대상·역할·완료조건 검사(:1039-1063). allowed_operations가 실행 불가능한 값(예: mission.complete_task)이어도 구조만 맞으면 등록된다.
- 완료 검사는 단계별 증거를 요구한다: completion.py:930-936 "belongs to a different step" 거절, :958-1025 원본 ToolResult·정규 증거값·action/invocation 연결·타깃 연결 요구, :1092-1101 실패 원본으로 성공 주장 거절.
- 따라서 도구를 실행하지 않는 필수 요약 단계는 자기 단계 증거를 만들 수 없어 complete_step/complete_task가 막힌다. 감사 측 오프라인 재현(different_step 3, 유효 0)과 일치. [미검증: 이 워크스페이스에서 재실행 안 함]

### 7.2 확정 방향 (루트 결정): 등록 시 교정 + 기존 Task/Goal 완료 연결

- 기존 Task/Goal 완료가 이미 집계를 맡는다. complete_task는 필수 단계 전부가 완료·증거 보유일 때만 닫히고(engine.py:2328-2332), complete_goal은 모든 필수 단계 완료를 요구한다(:2097-2135). 따라서 별도 집계 엔진은 필요하지 않다.
- 이번 구현의 F02 해결은 **등록 시 교정**이다. required 단계가 실행 가능한 동작을 하나도 담지 못하거나 증거를 만들 수 없는 완료조건만 가지면, register_path/작성 검사가 correction과 함께 그 단계를 거절한다. 정상 실행 단계는 기존 Task/Goal 완료로 그대로 집계된다.
- 새로운 aggregation 완료조건 kind, aggregation receipt, 새 완료 수명주기는 이번 구현 필수 범위가 아니다(7.2 초기 제안은 채택하지 않는다).
- 완화 금지: completion.py:930-936(다른 단계 증거 거절), :958-1025(원본·연결 요구), :1092-1101(실패 원본 거절)은 그대로 유지한다. 등록을 교정하면 요약 함정 자체가 생기지 않는다.
- 범위 가정 금지: "사용자가 보고 단계 보장을 요구했다"는 현재 지시에 없다. 이를 근거로 새 완료 종류를 추가하지 않는다. 전체 모듈화 목표를 축소하는 것이 아니라, 요구에 없던 새 완료 의미를 피하는 것이다.
- 파일: paths/writing.py(작성 검사) + engine.register_path(등록 검사) 중심. completion.py는 건드리지 않는다.

## 8. 증거·원본 읽기·가림 (M19, M26, C-05)

### 8.1 보장 수준 [확인]

- 주 실행 검증 노드: nodes/verify.py:74-93 기본 변환+메타 병합, :652-701 원본 ToolResult 전체(:664), 원본 제안(:673), 계획 단계(:675), 경로·타깃·계정 연결(:691-700)을 첨부.
- 편의 기록: core/convert.py:82-104는 digest/ID/success 표시만, evidence/service.py:44-61은 그 결과를 그대로 저장. 차단 기록(:63-134)은 실행 원본이 아니라 결정 기록.
- 소비자별 해석: knowledge.py:297-308 원본 해석+:360-398 접지, evidence_access.py:35-119 원본 읽기·상태 구분, analysis.py:710-746 의미 검증·행위 대조, completion.py:958-1025 완료 원본 요구.
- 즉 "원본 보존"은 주 실행 경로에만 보장되고, 소비자가 각자 metadata 모양을 해석한다.

### 8.2 최소 공통 읽기 계약(제안)

evidence_access를 포트로 승격한다:

    RawEvidenceView = ok | missing | other_run | no_raw | truncated
    read_raw(evidence_id, invocation_id) -> (상태, 원본 dict, provenance{action/invocation/proposal/path/step/target/principal}, 잘림 여부)

각 검증기는 자기 목적의 추가 조건(계정 접지, 완료 최소 수량 등)을 그대로 유지한다. 완료 검증은 no_raw/other_run을 성공으로 취급하지 않는다.

### 8.3 C-05 가림 [확인]

- predicates.py:245-249 provenance/retractions/attrs_seen 키 스캔 제외, :256-257 first_seen/last_seen/cleanup 제외, :355-364 claim_provenance는 구조만 검사(값 재귀 스캔 없음). cleanup.note 같은 bookkeeping 문자열은 secret guard를 통과한다. [미검증 재현: 감사 측 합성 marker 재현, 이 워크스페이스에서 재실행 안 함]
- 제안: 가림 어휘·함수는 core/redaction.py:7-35/:59-71이 이미 소유. 그래프 저장 경로(장부 문자열·스냅샷 import 포함)는 공통 redaction을 거치게 하고, 구조 검사(스키마)와 문자열 가림을 분리한다. predicates의 키 제외 목록은 "구조 검사 제외"로만 유지하고 값 가림은 강제.

## 9. 소규모 계약 승격 (M16, M18, F12)

- M16 [확인]: 후속 대응이 matched_rules 문자열 꼬리표로 전달된다 — control/severity.py:38-53 to_tag, orchestration/nodes/policy.py:48-69 별도 파서, 정의는 core/enums.py:85-105. 제안: PolicyDecision에 정형 disposition 필드 추가, 꼬리표는 하위호환 읽기만. 도구 위험/취약점 심각도/거부 후속은 계속 별개 축.
- M18 [확인]: 계획 관련 키 목록 attack_graph/observation.py:35-65, 지문 :89-99. 목록 밖 새 사실 변화는 역할 재활성화 지문에 반영되지 않는다. 제안: 관측 제공자가 의미 변화 투영 규칙을 선언하고 일반 지문 계산기가 소비. 역할별 소비처(registry.py:165)의 정확한 재확인은 [미확인].
- F12 [확인]: ActionProposed에 target 미포함(nodes/plan.py:322-337), 화면은 target 기대(app/cli/view_models.py:19/:68-74), PolicyEvaluated는 reasons 기대(:85-86) vs 방출은 matched_rules 중심. 제안: canonical 이벤트 payload 키 계약 고정(안전한 target label/ref, reasons/matched_rules 양쪽), 비밀·전체 인자는 이벤트에 넣지 않음.

## 10. 구현 파일 책임 지도 (신규 파일 최소화, 이동·통합·삭제 우선)

| 계약·수정 | 소유 파일(제안) | 팀/소유 | 관련 번호 |
|---|---|---|---|
| 원자 변경 공개 port(내부) | orchestration/coordination.py(계약·파사드) | orchestration | M20 |
| 조정자 재작성·records·검증 | orchestration/coordination.py | orchestration | M20 |
| 복합 제공자 구현 | orchestration/state_backend.py (신규 1개) 또는 제공자 패키지 | orchestration | M20 |
| 이력 저장소 짝 검사 | orchestration/coordination.py:295-305 | orchestration | M20/M22 |
| 작업 장부 순수 규칙 | orchestration/work/rules.py (신규 1개) + work/{memory,sqlite}.py | orchestration | M21 |
| 체크포인트 타입 등록 | orchestration/checkpointing.py | orchestration | M22 |
| 구성 버전·스냅샷 | orchestration/component_binding.py + app/runtime_components.py | orchestration+app(루트) | M23 |
| 이벤트 이력 조회 계약 | core/ports/event_bus.py(별도 Protocol, 신규 파일 불필요) + operator_intents/service.py:1251 | core(루트)+orchestration | M29 |
| admission 포트 | core/ports/control_plane.py — 루트 | core(루트) | C-01/F03, C-02/F04, C-04, M17 |
| admission 구현·회계 | control/service.py + control/{budget,rate_limit,roe}.py | control | 상동 |
| 호출 직전·재시도 배선 | orchestration/nodes/execute.py, nodes/policy.py, runner.py | orchestration | 상동 |
| network authority | core/contracts/target.py 확장(또는 core/network.py) — 루트 | core(루트) | M15, F01 |
| authority 소비 | control/scope.py, attack_graph/identity.py, orchestration/observations/*.py | control/graph/orch | 상동 |
| edge 네임스페이스·마이그레이션 | attack_graph/stores/namespaced.py (+sqlite 재색인 헬퍼) | graph | C-03/F05 |
| 등록 시 실행불가 단계 교정 | orchestration/paths/writing.py + orchestration/engine.py(register_path) | orchestration | F02, M24 |
| 원본 읽기 계약 | core/ports/evidence_store.py 확장(또는 별도 Protocol) + orchestration/evidence_access.py + nodes/verify.py | core(루트)+orchestration | M19, M26 |
| 장부 문자열 가림 | core/redaction.py 소비 + attack_graph/predicates.py + snapshot import | core+graph | C-05, F13 |
| 후속 대응 정형화 | core/contracts/policy.py + control/severity.py + nodes/policy.py | core(루트)+control+orch | M16 |
| 변화 투영 등록 | attack_graph/observation.py | graph | M18 |
| 이벤트 payload 키 | core/contracts/events.py + nodes/plan.py + app/cli/view_models.py | core(루트)+orch+app | F12 |
| 앱 조립·주입 | app/bootstrap.py, app/runtime_components.py | app(루트) | M20/M23 |
| .realrun 추적 정책 | .gitignore / tests fixture 분리 | 운영 결정 | F13 |

신규 파일은 최대 3개(work/rules.py, orchestration/state_backend.py, 선택적 네트워크 authority 위치)로 제한한다. 원자 세션 계약은 orchestration 내부 공개 port로 두어 core 신규 파일을 만들지 않는다.

## 11. 워커 보고 교차검토·정정

오케스트레이터가 실제 소스로 재확인한 결과:

1. [정정] 감사 자료의 행번호 다수가 현 커밋 기준으로 ±5~30줄 밀린다(예: M20 구체 타입 분기 :135-150, F02 완료 거절은 completion.py:930-936, M19 완료 원본 요구는 :958-968). 내용 방향은 유지.
2. [정정] 워커2의 "승인 outcome 캐시에 TTL" 제안은 이중 과금 방지 구조를 약화시킬 수 있다. 캐시는 approval_outcome의 멱등 소비 보호이고, 시간 재검사는 새 admit_invocation이 담당하는 분리가 안전하다. TTL은 별도 결정 사항으로 내린다.
3. [정정] 워커1의 세션 초안(session_lock/participant별 load·commit)은 채택하지 않는다. atomic_scope가 같아도 별개 연결·순차 commit이면 원자성이 보장되지 않는다. 단일 복합 제공자의 change(callback) 단일 commit/rollback으로 확정(§2.2, §16).
4. [정정] 초기 검토에서는 core 승격을 권장했으나, 루트 결정으로 orchestration 내부 공개 port + 복합 제공자 소유로 확정했다(Path 모델 연쇄 이동 최소화). (§16)
5. [확인] 워커1의 "작업 장부는 조정자 참여 대상 아님"(bootstrap.py:308-311) 및 "task_graph는 이미 규칙 공유(task_graph/sqlite.py:122-143)"는 사실이다. M21 범위는 작업 장부로 한정.
6. [확인] 워커2의 accounting 지도(evaluate/approval_outcome/retry의 소비 지점)는 소스와 일치(budget.py:89-92 포함). C-01/C-02/C-03/C-04/C-05, F01/F02는 이 커밋에서 조건 확정이며, 재현은 감사 측 오프라인 결과다.
7. [확인] 워커2의 "in_flight=None 정책 상반"(rate_limit.py:109-119 미적용 vs roe.py:201-207 닫힘 실패)은 사실이다. 계약에 각각 명시 필요.
8. [확인] .realrun JSONL 15개 git 추적(F13), event_bus 이력은 포트 아님(memory.py:50-62), checkpoint 스캔 방식(checkpointing.py:24-72) 모두 소스 일치.
9. [미확인] runtime_state.py 부재(실제 state.py가 런타임 상태 모델), M18 역할별 소비처 행, F07/F06/F08~F11은 이번 담당 범위 밖으로 남긴다.

## 12. 재현된 결함 / 정상 검사 / 운영 한계 구분

- 감사 측 오프라인 재현으로 조건 확정된 결함(이 워크스페이스에서 재실행하지 않음): C-01(승인 후 ROE 시간), C-02(재시도 기간 예산), C-03(edge 덮어씀), C-04(포트 직접 호출 불변량), C-05(장부 free text), F01(포트 포함 관측 누락), F02(무증거 요약 단계).
- 정상 검사(건드리면 안 됨): 실행 직전 제안/경로 정합(engine.py:2667-2789), proposal/context engagement 일치(정상 CLI 경로 engineer.py:2717-2728, policy.py:175-180), 영수증 없는 재개 거절(runner.py:769-845), 승인 확정 캐시 멱등(service.py:331-333), rate's in_flight=None 미적용(구버전 직렬 경로 호환), unsupported_roe_policy의 fail-closed ABORT, 격리·검증 실패 시 UNKNOWN.
- 운영 한계(결함 아님): 속도/예산 카운터 프로세스 로컬(service.py:18-25) + 재개 단조 복원(:441-493), 승인 TTL 자동 만료기 없음, 아웃박스 at-least-once(outbox.py:1/:26-94), 그래프 SQLite 단일 프로세스 미러(sqlite.py:112), ArtifactBackend 미배선, Audit/Evidence JSONL 이중 파일 순차 쓰기, Evidence run ID 경로 조합 규율.

## 13. 루트가 확정할 최소 계약·결정 목록

1. (M20) [결정됨] orchestration 내부 공개 port + 단일 복합 제공자가 Task/Path/records 변경 세션을 소유, Coordinator는 change(callback)+bind/validate만 소비. core 승격 없음. (§2.2, §16)
2. (C-01/C-02/C-04/M17) ControlPlane에 admit_invocation(비과금·멱등) + reserve_tool_call(원자 확인·차감) 2메서드 추가 확정. approval_outcome 캐시는 유지, 재확인은 admit이 담당.
3. (M15/F01) NetworkAuthority 단일 계약 위치와 IPv6 브래킷·기본 포트 규칙. 기존 그래프 키 호환(마이그레이션) 정책.
4. (C-03/F05) edge id 네임스페이스 변환 방식과 SQLite 재색인/마이그레이션 시점.
5. (F02/M24) [결정됨] 등록 시 실행 불가 필수 단계 교정 + 기존 Task/Goal 완료 연결. 새 aggregation kind/receipt 엔진은 이번 범위 아님. (§7.2)
6. (M21/M22/M23/M29) 작업 장부 규칙 공유, 체크포인트 명시 등록, 구성 버전, 이력 조회 분리 채택 여부와 우선순위.
7. (M16/M18/F12/M19/M26/C-05) 정형 승격·공통 읽기·가림 통합의 채택 여부.
8. (F13) .realrun 추적 해제·fixture 분리 운영 결정.

## 14. 관련 번호 색인

- M15 대상 인자 신원·네트워크 authority, M16 후속 대응 꼬리표, M17 실행 문맥 신호, M18 변화 투영 키, M19 원본 읽기 공통, M20 원자 변경 세션, M21 작업 장부 규칙, M22 체크포인트 타입 등록, M23 구성 버전, M24 완료조건 수명주기, M26 증거 보장 수준, M29 이벤트 이력, M30 공통 기능 소유 이동.
- C-01 승인 후 ROE 미재검사, C-02 재시도 기간 예산, C-03 edge ID 충돌, C-04 engagement 정합 불변량, C-05 장부 free text 가드.
- F01 포트 포함 관측 누락, F02 무증거 요약 단계, F03/F04(Control 측 C-01/C-02와 동일), F05 edge 충돌(C-03와 동일), F12 이벤트 표시 불일치, F13 저장 경계·추적 정책.

---

## 15. 회계 함정과 최소 admission 계약 확정안 (초안1 C 구체화)

본 절은 루트 후속 지시(초기 evaluate/승인 outcome이 이미 action/rate/초기 tool-call를 소비한 뒤 invoke 직전 재검증이 전체 capacity를 다시 쓰면 마지막 슬롯 거부 또는 이중차감이 생긴다)에 대한 확정안이다. 4.3절의 reserve_tool_call은 본 절의 reserve_extra_tool_call로 이름·의미를 확정한다.

### 15.1 실제 회계 기계 [확인]

- BudgetTracker: has_capacity(budget.py:75-87) — 첫 호출에서 기간 시계 앵커(:78), 검사 3종(행동 수, 도구호출 수, 기간). consume(:89-92) — 행동 1 + 도구호출 1 동시 소비. consume_tool_calls(:94-106) — 도구호출만, cap 클램프. remaining/tool_calls_remaining(:185-201).
- RateLimiter: would_allow(:49-62) 비소비, allow(:63-69) 소비(:68 이벤트 추가).
- evaluate ALLOW: rate allow(service.py:240) + budget consume(:249) = 이 행동의 단일 소비. REQUIRE_APPROVAL은 무소비(:235-236).
- approval_outcome: 해결 1회에 rate allow(:416) + budget consume(:428). 캐시(:331-333)가 재호출을 멱등 처리.
- retry: has_tool_call_capacity(:610, 도구호출 잔여만) + record_tool_calls(:578) → consume_tool_calls 1회. 실행: nodes/execute.py:319-329가 policy_lock 안에서 쌍으로 호출.
- 실행 직전: validate_current_action(engine.py:2667-2789) + claim_action_admission(engine.py:2545-2593, records에 ("admission", action_id) 기록) — 경로·세대·의존·바인딩만 검사, 시간/기간 없음.

### 15.2 함정의 정확한 형태 [확인]

- max_actions=1: evaluate consume → consumed=1. admit이 has_capacity를 그대로 호출하면 1 >= 1 → False → 이미 ALLOW 받은 행동이 "예산 소진"으로 거부된다.
- max_actions_per_minute=1: evaluate allow → events=1. admit이 would_allow를 호출하면 1 >= 1 → 거부, allow를 다시 호출하면 events=2(이중차감).
- 승인 경로도 동일: approval_outcome이 allow+consume 한 뒤 admit이 같은 함수들을 다시 부르면 같은 함정.
- 근본 원인: has_capacity/would_allow는 "새 행동을 받을 수 있는가"를 판정하는데, admit은 "이미 받은 행동이 지금도 실행 가능한가"를 묻는다. 질문이 다르므로 함수를 재사용하면 안 된다.

### 15.3 규칙 확정

A. 신규 소비 지점은 evaluate와 approval_outcome 두 곳뿐이다(기존 그대로, 변경 없음). 여기서만 rate 슬롯·행동/도구호출 예산을 소비한다.

B. admit_invocation(proposal, decision, context, *, receipt_id) — 호출 직전 재확인. 원칙: 비소비·비발행.
- 불변량(fail-closed, 무과금): decision.action_id == proposal.action_id, decision.verdict == ALLOW, proposal.run_id/engagement_id == context.engagement_id, decision.approval_id가 있으면 그 승인이 이 행동의 승인이며 상태 APPROVED(읽기만; resolve·소비·재발행 금지).
- 재검사: 범위(현재 설정) → ROE(UTC now: start/end, allowed_time_window, availability, max_parallel — in_flight는 이 행동을 제외한 값) → 기간 예산(시간 조건만).
- 금지 목록: has_capacity(전체) 호출, rate would_allow/allow 호출, budget.consume 호출, 승인 생성/resolve, _pending_proposals 소비. 이 금지가 리뷰 체크 항목이다.
- 기간 검사는 BudgetTracker.duration_exhausted(now)로 분리한다: 기간 조건만 보고, 개수 조건을 접촉하지 않으며, 앵커를 재설정하지 않는다.
- receipt_id 멱등: 같은 키 재호출은 재검사·재기록 없이 같은 판정. DENY도 1회만 기록(차단 증거·감사 중복 방지). 프로세스 로컬 맵(기존 _resolved_outcomes와 같은 성질)으로 충분하다.
- 거절 사유 코드: decision_mismatch / scope_revoked / roe_window_closed / roe_parallel_exceeded / availability_blocked / duration_exhausted.
- 반환 타입: 최소안은 기존 PolicyDecision 재사용(action_id, DENY/ALLOW, reasons에 코드). 새 결과 타입은 루트가 필요하다고 확정할 때만 만든다.

C. reserve_extra_tool_call(proposal, decision, context, *, receipt_id, attempt_index) — 추가 호출(재시도) 전용.
- 전제: 같은 decision에 대한 admit_invocation ALLOW(회계권리 확인), attempt_index >= 1, 호출자가 deps.policy_lock 보유(팬아웃 예약과 직렬화).
- 원자 검사 + 차감: 도구호출 카운터 잔여(_tool_calls_consumed < cap) + 기간(duration_exhausted) + ROE 시간/availability. 병렬은 재검사하지 않는다 — 같은 행동이 이미 in_flight라 자기 자신을 두 번 세는 오판이 된다. rate 슬롯과 action 카운터는 접촉하지 않는다(새 행동이 아니다).
- 차감은 consume_tool_calls(1) 정확히 1회, receipt_id 멱등.
- 거절 사유 코드: tool_calls_exhausted / duration_exhausted / roe_window_closed. CLI·이벤트가 그대로 표시.
- 기존 has_tool_call_capacity/record_tool_calls는 호환용으로 남기되, execute 경로는 이 단일 호출로 대체해 확인-차감 사이 이중 경로를 없앤다.

D. 내부 분해: BudgetTracker에 duration_exhausted(now) 추가, RateLimiter 변경 없음, service에 시간 게이트 공유 헬퍼(_recheck_time_gates) 하나. 새 프레임워크·새 타입 없음.

E. 배선:
- execute.invoke(): validate_current_action → claim_action_admission → admit_invocation(비소비) → ToolGateway.invoke.
- 재시도 루프: reserve_extra_tool_call(정책 락) → _invoke_with_semaphore(내부 admit 재확인, 비소비).
- 승인 재개(runner.py:797 경로): approval_outcome(기존 1회 소비) → 실행은 같은 admit 경유. 러너의 지속 claim(:818-845)과 engine의 ("admission", action_id) 기록(:2584)은 그대로 유지 — 실행 자체의 중복 방지 책임은 그쪽이고, admit은 시간·정책 재확인 책임이다. 두 장치를 합치지 않는다.

F. 재개·지속성: 결정은 체크포인트(state의 policy_decision)에 남고 재개 replay도 admit으로 들어온다. admit은 비과금이므로 재개가 회계를 다시 차감하지 않는다. rehydrate(service.py:441-493)의 단조 복원도 그대로다.

G. 완료 기준(구현 후 검증, 마지막 단계): max_actions=1 + max_actions_per_minute=1 + 짧은 기간에서 evaluate ALLOW → (승인 경로면 approve) → admit ALLOW(거부·이중차감 없음), 잔여가 정확히 1회 소비 상태. 기간 경과 후 admit DENY + 도구 호출 0회. 재시도 1회 = 도구호출만 +1(action/rate 불변), 재시도 중 기간 경과 시 두 번째 호출 0회. 재개 replay에서 추가 차감 0회.

### 15.4 표면 리팩터 금지 확인

- 계약(포트 메서드)만 선언하고 coordination/service/execute의 기존 호출 경로를 그대로 두면 함정과 이중 과금이 그대로다. 리뷰 체크: (1) admit 경로에 has_capacity/would_allow/allow/consume 0회, (2) 재시도 경로에 옛 reserve+charge 쌍 0회(단일 reserve 호출), (3) evaluate/approval_outcome 외에 rate/action 소비 지점이 없는가.

## 16. 저장 변경 세션 최종 형태 (루트 결정 반영)

### 16.1 형태

- per-participant begin_change→commit API는 미채택이다. atomic_scope가 같아도 별개 연결·순차 commit이면 원자성이 보장되지 않는다.
- **단일 복합 저장 제공자**(Task/Path/records 소유)가 복합 변경 세션을 열고 단일 commit/rollback 한다.
- Coordinator가 소비하는 표면은 backend.change(callback) + bind/validate 뿐이다. 그 외 접근 금지.
- 기본 Memory/SQLite 제공자 내부가 기존 규칙·같은 conn·공통 lock·records 저장을 소유한다.
- 공개 port는 같은 팀 orchestration 내부 공개 port로 둔다(core 승격·Path 모델 연쇄 이동 최소화).

### 16.2 Coordinator가 보는 표면 (확정)

    class AtomicStateBackend(Protocol):
        async def change(self, callback: Callable[[ChangeSet], Awaitable[T]]) -> T: ...
        # 선택(진단): def atomic_scope(self) -> Hashable: ...

    class Coordinator:
        async def change(self, callback): ...   # backend.change 위임
        # bind/validate: 기존 오류 코드·의미 유지

### 16.3 제공자 의무

- SqliteStateBackend: 단일 커넥션 + BEGIN IMMEDIATE, Task/Path/records 적재·저장·커밋/롤백, 스키마 버전 검사, "바뀐 행만 저장" 유지. 같은 파일 조건은 제공자 구성 검사(기존 오류 코드)로 유지.
- MemoryStateBackend: 공통 asyncio.Lock, deepcopy 스테이징, 무await 스왑, records dict.
- 구체 선택은 조립 팩토리에서 한 번만. Coordinator에 구체 import·isinstance 금지.

### 16.4 사라져야 하는 것 / 완료 기준 (표면 리팩터 금지)

- coordination.py에서 제거: isinstance 6종 분기(:135-150), attach_paths 구체 타입 검사(:152-169), _transaction의 _connect/_load_memory/_save_memory/orchestration_records DDL(:174-212), change의 dict deepcopy/스왑(:228-248), ChangeSet의 _missions/_tasks/_snapshots/_progress/_active 읽기(:61-123) — 공개 뷰의 공개 메서드로 대체.
- 완료 기준(구현 후): 공개 port만 구현한 테스트 제공자로 change 성공 + Task/Path/records 단일 커밋/롤백 + 실패 주입 시 부분 반영 0 + coordination.py에 구체 저장소 import 0 + 기존 동작·성능 보존.
- 계약 선언만 하고 기존 isinstance/사설 경로를 남기는 표면 리팩터는 불합격이다.

### 16.5 남는 명시 조건

- SQLite 제공자는 Task/Path/records가 같은 DB 파일이어야 한다(제공자 구성 검사).
- 메모리·SQLite 혼용 금지, 이력 저장소는 제공자 영역과 짝(기존 의미 유지).
- 작업 장부·증거·체크포인트는 이 경계 밖. 경로만 따로 교체하는 조합은 제공자 단위 교체로만 가능(의도된 제약).

이 결정으로 13절 1번(M20 형태)과 5번(F02)은 닫혔다. 남은 루트 결정: admission 반환 타입(PolicyDecision 재사용 vs 새 타입), NetworkAuthority 위치·마이그레이션 시점, edge 재색인 시점, 6~8번 항목.
