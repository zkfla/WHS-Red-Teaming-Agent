# G0 계측·정답·기준선 구현 기록

2026-09-08 · PASSED_OFFLINE · 계측/기준선 도구 gate, 성능 향상 승인 아님

## 구현 범위

- 독립 Knowledge/Frontier/transition/completion/scheduler oracle. 실제 production scheduler/completion과의 differential 연결은 G3/G4/G7에서 수행한다.
- 원시 Git blob을 읽는 historical freeze 검증과 명시적인 current release baseline 분리.
- Frontier correctness 수정은 [G0-C1](GENERIC_CORE_G0_C1_RECEIPT.md)로 분리했다. 기존 최소 반례 JSON은 보존했다.
- 실제 commit/recompute/frontier/context/checkpoint/resolve 함수의 opt-in numeric span. ContextVar로 async caller를 분리하며 disabled 경로에서는 size/clock/observer를 호출하지 않는다. decision span vocabulary는 존재하지만 실제 step engine 연결은 G7 대상이다.
- tests/performance의 bounded collector, scale/growth fixture, 통계, 별도 process watchdog. 네 가지 in-memory stage와 실제 fsync checkpoint, fake passive resolve를 지원한다.
- runner는 quick warmup 1 + 측정 5회, release warmup 5 + 측정 30회. 단일 case/전체 시간은 5/60초와 60/900초. 메모리 한도는 시작 직전 가용 메모리 25%와 2GiB 중 작은 값이다.
- 부모가 10ms 간격 RSS를 검사한다. 이는 OS allocation hard limit가 아니며 순간 peak를 놓칠 수 있다. Python allocation peak는 timing과 별도 sample로 측정한다. 부모 소유 임시 디렉터리 아래에서 checkpoint를 생성하여 강제 종료 후에도 정리한다.
- p50/nearest-rank p95/원시 표본, deterministic paired bootstrap, fixture·lock·core·config hash와 환경을 보존한다. 30개 미만 표본은 release p95 gate 통과로 취급하지 않는다.

## 계측 계약

| 필드 | 단위/정의 | 누락 정책 |
|---|---|---|
| stage | 고정 enum | 임의 label 거절 |
| elapsed_ns | operation의 inclusive elapsed ns | clock 실패/역행이면 null |
| event 수 | 실제 wrapped operation 호출 수 | observer drop은 collector.dropped 별도 기록 |
| input_items | commit batch Observation 수, freshness ledger Observation 수, recompute/frontier Relation 수, resolve 1 | context/checkpoint 또는 size 추출 실패는 null; 0으로 대체하지 않음 |
| input_bytes | 향후 안전한 bounded size source용 reserved field | 현재 null; 계측용 raw serialization 안 함 |
| outcome | OK/ERROR | exception 메시지·payload 기록 안 함 |

enabled observer 실패는 truth를 바꾸지 않지만, 실제 commit/audit/fsync 예외는 그대로 전파한다. span은 inclusive이므로 중첩 span 시간을 합산해 end-to-end 시간이라고 주장하면 안 된다. timing/observer 정보는 canonical ID/checkpoint state에 추가하지 않았다.

## 검증 및 release source 등록

- G0-C1 등록 후: 513 passed in 13.21s.
- production span 추가 후: 554 tests 중 552 PASS, 2 FAIL. 두 실패는 current 승인 hash가 이전 G0-C1을 가리키는 freeze 검사뿐이었다. 기존 의미 회귀/architecture와 신규 observer failure·async cancellation 검사는 통과했다.
- 위 core 변경은 decorator/비권위 진단 모듈이며 semantic schema 변경은 없다. diff와 기존 불변조건을 검토하여 현재 source를 `g0-diagnostics` 단계로 명시적으로 등록한다. runtime version은 G0-C1의 0.1.1을 유지한다.
- 승인 후 확장 fixture 포함 전체 회귀: **562 passed in 148.18s**. Ruff PASS. 이 시간은 부하가 있는 현재 머신의 회귀 실행 시간이며 performance baseline으로 사용하지 않는다.
- 승인 exact-byte core SHA: `706792635a2d5ff6b82ca8fdcf23783a7b36e2f79160705eea99a0a5d44d3d24`.
- 최초 quick baseline: 10 cases 중 8 PASS / 2 TIMEOUT. timeout은 context 1,000과 commit growth 100 cycles다. 성공 표본으로 포함하지 않는다. [원본 JSON](GENERIC_CORE_G0_QUICK_BASELINE.json).
- 초기 두 JSON(`QUICK_BASELINE`, `QUICK_INSTRUMENTED`)은 harness 개발 기록으로만 보존한다. 첫 실행은 시작/종료 source fingerprint 검사가 없었고 실행 중 harness가 수정됐다. 두 번째는 회귀 테스트와 겹쳤다. 둘 다 **NOT_COMPARABLE**, 최적화 승인용 A/B baseline으로 사용하지 않는다.
- runner는 현재 시작/종료 source fingerprint를 대조하며 변경되면 `SOURCE_CHANGED` 및 nonzero exit로 처리한다. 최신 단독 측정은 별도 `QUICK_ISOLATED` receipt에 기록한다.
- [단독 quick 측정](GENERIC_CORE_G0_QUICK_ISOLATED.json): **10 PASS / 4 TIMEOUT**, source_integrity PASS. timeout은 commit 1,000 / freshness 1,000 / checkpoint 1,000 / commit 100 observations × 100 cycles. case watchdog 5초는 fixture 구성·5 timing samples·별도 memory sample·검증 전체를 포함한다. 이를 operation 단일 호출이 5초라는 뜻으로 읽으면 안 된다. 이번 실행의 memory cap은 127,906,816 bytes였다.

## 호환성·사용·rollback

- `observe_performance(observer)` 범위 안에서만 관측한다. 외부 telemetry 전송 기본값/의존성은 없다. observer를 설치하지 않으면 비활성이다.
- 측정기: `uv run python -m tests.performance.runner --output <새 receipt 경로>`; release는 `--release`. 기존 receipt를 덮어쓰지 않는다. 오류/timeout/resource limit는 nonzero exit와 개별 상태를 남긴다.
- 과거 Batch F hash와 실패 반례 receipt는 보존한다. 승인 hash는 test 실행 중 자동 갱신하지 않는다.
- Candidate 10/100/1,000개와 실제 requirement/coverage ledger를 갖는 pathless context fixture를 추가했다. 모델 불변조건과 deterministic bounded projection 검증 PASS. 기존 Observation-only context receipt와 새 fixture의 timing은 직접 비교하지 않는다.
- 교차 A/B helper는 warmup 제외, 매 pair 실행 순서 교차, 독립 factory 재구성, 양쪽 및 pair 사이 semantic hash 동일성을 강제한다. mismatch/exception은 실패다. runner의 `--paired`는 현재 동일 factory A/A 대조군으로 watchdog 연결을 검증한다. 최적화별 A/B arm은 해당 G에서 명시적으로 연결한다. paired timing의 memory는 NOT_MEASURED이며 별도 memory sample과 구분한다.
- 신규 bounded paired runner/context 검증 11 PASS, context/telemetry 검증 14 PASS. 확장 전체 회귀 결과는 아래 최신 gate에서 관리한다.
- release runner 구현은 존재하지만 30회 release 측정은 아직 NOT_RUN. G1/G2/G6/G8 기본 활성화는 각각의 release A/B 및 G9 gate가 없으면 허용하지 않는다. TIMEOUT fixture의 성능 개선이나 LIVE provider 성능은 미입증이다.

## G0 gate와 후속 계측 연결

G0는 기준선·도구의 검증이며 optimization/default enable 승인이 아니다.

| 항목 | 현재 근거 | 후속 책임 |
|---|---|---|
| historical/current release 분리 | baseline tests, exact raw blob hash | G마다 명시적 승인 hash |
| 독립 semantic 기준 | Knowledge/Frontier/transition/completion/scheduler oracle tests | G3/G4/G7 실제 구현 differential 연결 |
| opt-in 관측·failure isolation | 실제 6 stage, clock/observer 오류, async cancellation/isolation tests | G7 decision span 연결 |
| scale/growth | 100..100,000 Observation, 100..10,000 Relation case bounds; 1/10/100 cycles; Candidate 10/100/1,000 tests | 대형 입력 timeout/resource limit 보존 |
| bounded timing/memory | process watchdog/메모리 제한 강제 실패 tests; 단독 quick receipt | 각 G release paired timing + 별도 memory |
| 통계와 A/B 무결성 | 원시 samples/p50/p95/seeded bootstrap, paired oracle mismatch tests | 실제 optimized/off factory 지정 |
| 구조 counter | G0 stage call events/input sizes, 누락 정책 위 표 | G1 global scan count, G2 queue/edge/guard/aux count, G4 physical invocation/receipt fsync count |

위 후속 counter는 아직 존재하지 않는 해당 최적화/스케줄러 내부에 귀속된다. G0의 null input_bytes나 generic stage event를 그 counter 구현으로 대체 주장하지 않는다.

최종 G0 전체 회귀: **568 passed in 101.40s**, Ruff PASS, diff whitespace 검사 PASS. G0 신규 tests-only 변경 중 승인 core SHA는 그대로 유지했다. 이후 G1 source 변경은 별도 package gate를 따른다. release 30회 sample/default enable 검증을 G0 완료로 주장하지 않는다.

도메인 이식·외부 provider 호출·commit·push는 하지 않았다.
