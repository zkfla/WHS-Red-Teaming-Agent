# Generic Core 성능 계획 자기평가·수정 기록

> 2026-09-08 · 최종 R3 · PLAN_ONLY
> 대상: [최종 계획](GENERIC_CORE_PERFORMANCE_EVOLUTION_PLAN.md)

## 평가의 의미

사용자의 “100점이 될 때까지 계획을 자기평가하고 수정” 요청은 **사전 정의한 계획 완결성 항목 20개를 모두 충족하는 것**으로 해석했다. 각 항목은 해당 설계/검증/실패 처리 내용이 문서에 구체적으로 있으면 5점, 없으면 0점이다. 부분 점수로 부족한 항목을 덮지 않는다.

100/100은 아래 문서 체크리스트의 자기평가다. 객관적으로 완벽한 설계, 독립 평가자의 승인, 실제 속도 향상, 실제 LLM 정확도 100%, 구현 완료를 뜻하지 않는다. 성능 수치와 운영 결과는 구현·실험 전 UNKNOWN이다. 새로운 반례가 나오면 기준과 계획을 다시 개정한다.

동일 작성자가 순차적으로 검토했으며 외부 reviewer·subagent의 독립 검토를 받은 것처럼 표현하지 않는다. 이번 요청에 포함되지 않은 다중 agent 실행은 하지 않았다.

## 반복 1 — R1 초안 검토: 85/100

출발점: 직전 대화의 6가지 개선 후보를 도메인 제외 조건과 공개 source 조사에 맞춰 G0~G9로 구체화했다. 실제 local 코드 위치, cache/budget/progress 계약, 각 패키지 gate와 rollback을 포함했다.

반례/결함:

1. **기존 결과와 같으면 정답인가?** 아니다. 기존 pruning·guard 처리에 오류가 있으면 최적화도 오류를 보존한다. baseline differential만으로는 부족했다. C15 미충족.
2. **구버전 checkpoint에 resolver 예산이 없다면 어떻게 재개하는가?** 0으로 시작하면 예산을 새로 주는 셈이고, Observation 수로 복원하면 실패 요청이 빠진다. 자동 migration 금지·schema 경계를 명확히 해야 했다. C17 미충족.
3. **고정된 core SHA를 유지하면서 core를 수정할 수 있는가?** 기존 테스트와 후속 변경 목적의 충돌이 해결되지 않았다. C19 미충족.

R2 수정: 최종 계획 §9에 independent oracle, recorded replay와 실제 시간 차이의 구분, cold/warm/growth·통계·자원상한을 추가했다. §10에 schema version, missing ledger 처리, operational receipt durability, crash matrix와 rollback을 추가했다.

## 반복 2 — R2 검토: 95/100

해결: C15/C17이 문서상 충족됐다. correct decision denominator, false/충돌의 유효한 진전, raw result policy boundary, 실제 acquired_at 보존까지 점검했다.

남은 반례: “hash를 그냥 새 값으로 바꾸면 되지 않나?”는 동작 정확도나 reference-specific 변경 부재를 검증하지 못한다. 테스트 실행 때 자동 갱신하면 검사가 무의미해진다. C19는 여전히 미충족이었다.

R3 수정: 최종 계획 §11에 역사적 baseline 보존, current release/reference no-edit gate 분리, correctness 통과 후 명시적 hash 갱신, 자동 expected 생성/skip 금지를 추가했다. §12에 착수·인계 체크리스트를 고정했다.

## 반복 3 — R3 재검토: 100/100 (계획 완결성에 한함)

재검토 질문과 처리:

- 더 빨라진 이유가 탐색 누락인가? → §5 G2/G9, §6, §9에서 truncation·독립 oracle·quality gate로 구분.
- 캐시 hit가 오래된 TRUE를 되살리는가? → §4 cache 계약과 G1/G5에서 freshness/conflict/absence read/epoch 관리.
- concurrency 때문에 예산·진실이 달라지는가? → G3/G4와 §10에서 reserve-before-dispatch, single writer, receipt, clock replay 계약.
- 문맥 압축이 반증을 지우는가? → G6에서 evidence bundle, incomplete 표시, trust-channel 분리.
- 에이전트가 말만 바꾸면서 계속 돈을 쓰는가? → G7에서 semantic progress fingerprint와 bounded replan/repair.
- 0 Candidate 또는 blocked-only coverage를 성공이라 하는가? → G7 CompletionAssessment가 finalizer 실행 완료와 분리.
- 외부 LLM 없이 실제 판단 성능 향상을 주장하는가? → §1/§8/§12에서 offline contract 검증과 LIVE_LLM_VALIDATED를 구분.
- 아직 필요하지 않은 큰 변경을 강제하는가? → G5 및 C1~C4는 조건부/보류, trigger 미충족이면 구현하지 않음.
- 기존 완료 기록을 고치거나 donor domain을 붙이는가? → 기존 완료 계획 보존, 새 후속 계획만 등록, 실제 domain 적용 제외.

### 고정 평가표

| 기준 | 검증 내용 | R1 | R2 | R3 | 최종 근거 |
|---|---|---:|---:|---:|---|
| C01 | 실제 domain·외부 실행 제외가 명시됨 | 5 | 5 | 5 | §1 |
| C02 | local source와 가설/확정 사실을 구분함 | 5 | 5 | 5 | §2 |
| C03 | 공식 문서·고정 repository source 근거 | 5 | 5 | 5 | §3 |
| C04 | 채택하지 않는 대안과 이유 | 5 | 5 | 5 | §3, §7 |
| C05 | authority·도메인 독립·effect 불변조건 | 5 | 5 | 5 | §1, §4 |
| C06 | 선행관계·필수/조건부 순서 | 5 | 5 | 5 | §5 |
| C07 | 새 계약의 최소 필드와 소유권 | 5 | 5 | 5 | §4 |
| C08 | snapshot/mutation/atomicity 보존 | 5 | 5 | 5 | G1, G5 |
| C09 | cache key·invalidation·freshness·cap | 5 | 5 | 5 | §4, G1/G2 |
| C10 | dispatch 전 예산 예약·동시성 | 5 | 5 | 5 | G3/G4 |
| C11 | crash·UNCERTAIN·자동 replay 제한 | 5 | 5 | 5 | G3/G4, §10 |
| C12 | context 완전성·trust·반증 보존 | 5 | 5 | 5 | G6 |
| C13 | priority/무진전/완료 판단 구분 | 5 | 5 | 5 | G7/G8 |
| C14 | 측정 지표·채택 기준·실패 보고 | 5 | 5 | 5 | G0, §6, §9 |
| C15 | legacy와 독립적인 correctness oracle | 0 | 5 | 5 | §9.1 |
| C16 | determinism/replay 범위가 명시됨 | 5 | 5 | 5 | G4/G9, §9.2 |
| C17 | 구버전 schema·missing budget·rollback | 0 | 5 | 5 | §10 |
| C18 | 패키지별 gate와 보류 기준 | 5 | 5 | 5 | §5, §7 |
| C19 | 기존 source freeze 검사와 후속 변경 정합성 | 0 | 0 | 5 | §11 |
| C20 | 계획 점수·구현/실측/live 상태를 구분함 | 5 | 5 | 5 | 머리말, §8, §12 |
| **합계** | **20개 × 5점** | **85** | **95** | **100** | **실제 성능 점수 아님** |

## 남은 불확실성과 향후 반증 조건

1. G1/G2가 작거나 sparse한 상태에서 overhead만 늘릴 수 있다. → G0/G9 실측에서 불리하면 기본화하지 않음.
2. durable receipt I/O가 concurrency 이득을 상쇄할 수 있다. → 전체 저장 비용을 포함해 평가, concurrency=1 유지 가능.
3. dependency tracking의 복잡성이 이득보다 클 수 있다. → G5 trigger·full fallback, 기본 필수 제외.
4. synthetic priority 평가가 실제 조사 품질을 대표하지 않을 수 있다. → 실제 품질 주장 금지, 별도 승인된 후속 평가 필요.
5. 구버전 API를 외부 consumer가 사용 중일 수 있다. → 착수 때 in-scope consumer를 확인; 발견되면 migration/adapter 범위를 재평가.
6. Python plugin이 악의적으로 프로세스 내부 상태를 바꾸는 공격까지 방어하지 않는다. → 신뢰된 extension 계약이며 sandbox로 광고하지 않음.
7. 175개 baseline tests를 통과해도 새 oracle에서 legacy 결함이 드러날 수 있다. → correctness 변경을 최적화와 분리하고 release를 멈춤.

위 항목은 없어진 위험이 아니라 **발견/중단/보류 방법을 정한 위험**이다. 이 구분을 없애고 “완벽한 계획”이라고 표현하지 않는다.
