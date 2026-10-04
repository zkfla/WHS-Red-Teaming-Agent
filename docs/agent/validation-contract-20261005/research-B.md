# research-B - 공통 작업자 그래프 기록 호출 (2026-10-05, 계획고정용)

상태: 조사만 함. 제품코드 수정/테스트/커밋 없음. 독립리뷰 진행중이라 점수반복 없이 고정 가능한 선에서만 쓴다.

결론: 도구별 해석기 없으면 원본결과가 작업자에게 전달만 되고 그래프에 안 남는다. 공통 기록을 장식 필드로 두고 기존 검증을 그대로 강제하면 꽝이다. 공통 기록은 추론 관문에서 작업자가 실제로 부르는 호출이어야 하고, 기존 출처/주장 검사를 재사용한다. 새 병렬 그래프 저장소 금지.

## 1. 수정된 입력 계약 (받아쓰기 금지)

- 모델 입력에 action_id/invocation_id/tool_id 반복 요구 금지. top raw_pointer 강제 금지.
- 모델이 내는 건 정규화된 facts/hypotheses/findings/branches + evidence_ids뿐이다. 근거자원은 증거id로만 가리킨다.
- 관측(facts)에만 코드가 접지된 원본포인터를 요구한다: evidence_id + raw_path [output,...]. 추론/가설/취약점주장은 자유 의미주장 + evidence_refs로 낸다. 원본경로 강제 금지.
- per-fact 포인터가 있으면 top 포인터 불필요. 런타임이 evidence_ids로 저장소에서 권위있는 출처를 풀어서 채운다. 모델이 적은 식별자는 힌트로만 보고 덮어쓴다.
- 출처 표기는 런타임 자동채움만 인정한다: 단계, 호출출처, 실행id, 증거id, 측정/추론 구분. 작업자 임의출처 필드 금지. 재사용: src/redteam/attack_graph/predicates.py 22-36/248-500/678-802, service.py 200-440.
- 출처인정은 같은 실행/같은 임무면 이전 단계 것도 유효. 같은 실행단계 강제 금지. 기존 저장출처 확인(completion.py 532-640) 재사용.

## 2. 기대값 문제 해결 (사전확정 없이 인과증명으로)

- 현행 검증기는 실행 전 선언한 기대형태(expectations.py 36-50, evaluate 57-90)를 가설에 묶고 verdict_for 91-111로 판정한다. 매퍼의 FindingMutation도 expectation 필수를 박는다(mapper.py 63-82, 검증 157).
- 공통 취약점주장은 이 사전 기대필드를 요구하지 않는다. 대신 인과 실제증거 증명을 요구한다: 같은 실행 증거 + 보존원본 + 실제출처연결 + 정식운용증명 + 대상연결 + 계정범위면 원본세션영수증/접지바인딩.
- 그래프 사전검토는 이미 확정된 그래프 상태를 전제로 굴면 순환이라 금지한다. 사전검토는 형식/출처/대상 범위까지만 보고, 확정판정은 사후 증거주장 인정에 맡긴다.
- 사후 인정은 분석검증 담당을 사전 기대 없는 형태로 일반화한 공유 판정이다. 최종 인정영수증은 이 사후 인정을 한 번 거친 것만 완료 검사가 인정한다.

## 3. 등록 루트 (pluggy 재사용, 로컬호출 분리)

- 호출가능물 등록은 기존 확장선언/조립(app/extensions.py 17-99)을 재사용한다. 새 영역이 정당할 때만 runtime_tool 종류 기여 1개로 등록한다. 새 병렬 등록체계 금지.
- 공통 기록은 대상실행 도구가 아니라 로컬 모형함수다. 위임 실행경로의 구조화된 호출로 레지스트리에 붙고, 대상 행동예산을 쓰지 않는다. 실행 의존들과 현행 목적은 호출문맥에서 주입받는다(context.py 54-, nodes/worker.py 35-, _support.py 22-).
- 설명자 노출은 local_tools로 분리한다. 원격 대상도구와 같은 이름공간에 올리지 않는다. 중앙 분기에서 도구이름별 dispatch 추가 금지.
- 새 로컬호출 이음매(그래프 기록 처리기)는 한 번만 주입하고 확장들이 재사용한다. 조립점은 runtime_components.py 217-320, bootstrap.py 239-343/436-567 재사용. 매퍼는 폴백으로 두고 UPDATE(nodes/update.py)가 공통기록 배치를 제1입력으로 소비한다.

## 4. 완료 검사가 보는 법 (매직 원본종류 없이)

- 공유 완료 검사가 매직 원본문자열로 공통기록을 구분하면 안 된다. 보는 건 저장된 증거+그래프다: 증거 존재/같은실행, path연결, 실제출처연결(completion.py 386-530), 보존원본, 정식운용증명, 대상연결, 원본출처 확인(semantic_completion.py verify_single_origin/verify_receipt_bound_origins).
- 재사용: 출발지/식별자 기존코드 + 새 의미주장 검토결과. 의미주장은 result_claims.py, 작업/목표 영수증은 semantic_completion.py build_task_receipt/verify_task_receipt/verify_single_origin 계열을 쓴다.
- 공통 결과주장 인정영수증 소유자 명시: owner = 그래프 기록 처리기 기여id (예: runtime_tool:graph-record). 영수증 payload에 owner, source_evidence_ids, related 노드/가설id, claims를 넣고 불변 증거메타로 저장한다. 완료 검사는 owner가 달라도 권위로 안 보고 공유 증명으로 매번 다시 푼다. 목적해시 바뀌면 무효.
- 취약점 확정은 작업자 불리언으로 발행 불가. 그래프 가설상태 + 최종 인정영수증 + 증거접지(knowledge.py _claim_grounding 계열) 모두 요구. 미해결은 inconclusive/held/stalled로 유지.

## 5. B 작성범위 확정 (구현 아님, 디스패치 전 고정용)

B가 쓰는 범위:
- 새로 만드는 쪽: graph_recording.py (공통 기록 호출 처리기 본체), local_tools.py (로컬호출 등록/계약: 설명자+호출문맥+비동기 레지스트리).
- 손보는 쪽: analysis.py (사후 주장 인정 추가), findings.py (승격 게이트 재사용/확장, PromotionRequest/PromotionDecision 37-94 유지), result_claims.py (필요시 최소).
- B가 안 건드리는 쪽(루트 소유): completion.py, semantic_completion.py, engine/nodes update, RuntimeComponents/bootstrap 주입부. B는 인터페이스 가정만 적고 주입은 루트/A에 맡긴다.

로컬호출 레지스트리 구체화:
- 형태: 설명자 + call(context) 비동기 레지스트리. pluggy runtime_tool 기여로 등록, 한 번 주입 후 확장 재사용. 중앙에 도구이름별 분기 금지.
- context는 구체 엔진 의존 금지: 실행 의존모음 + 런타임상태 + 경로저장 + 현행 목적 + 호출가능 모형검토 포트 + before_call 예산가드. 엔진 타입 import 금지.발동: 위임 실행경로 구조화 호출에서만 탄다. 대상 행동예산 미소모.
- 실행결과 id는 내용해시 멱등으로: 기존 조정자 content_hash(coordination.py 35/187/270-304) + 증거저장소 dedupe 재사용. 같은 주장 반복호출은 같은 id로 수렴, 새 병렬 저장소 금지.

사후 검증 시그니처 구체화 (이름 고정):
- verifier.verify_claim_with_budget(request, budget, before_call): request = 원본출처묶음(evidence_ids→해결된 기록/원본/연결) + 새 주장(facts exact포인터, hypotheses/findings 자유의미+evidence_refs). budget/before_call은 기존 BudgetedAnalysisVerifier 패턴(analysis.py 54-65/474-481) 그대로 통과. 사전 Expectation 필드 없음.
- 처리순서: 형식/출처/대상 범위 사전점검 → 원본풀 해결 → 인과증명(보존원본/연결/운용증명/계정접지) → 모형 의미판정(필요시) → 인정영수증 발행. 사전점검에서 확정 그래프상태를 끌어오면 순환이라 금지.
- 공유 영수증은 완료 검사가 출처 rewrite 없이 받는다: validate_result_claims(completion.py 1146-)가 verify_single_origin/verify_receipt_bound_origins(semantic_completion.py 185-289)로 바운드 출처를 재확인. 최종 인정(analysis.py 989-1082 final admission) + 목적해시 일치 + 바운드 출처 전수통과를 모두 요구. owner는 판정권위가 아니라 추적용이다.

트리거/연동:
- 작업자 UPDATE 전 공통기록 호출 → 사후 인정 → 인정영수증(불변 증거메타) → 완료/의미완료 검사가 영수증+원본으로 재확인.
- 실패는 빈결과/성공으로 숨기지 않고 원인+다음판단 정보를 기존 진단계약으로 전달. 매퍼실패 동반기록(recovery.py 807-849/1264-1300)은 유지.

## 6. 세션 영수증 생산/소비 (BOLA 교훈)

- 생산자: web_http.py 30-120대, network.py summary()가 실제 사용한 불투명 세션참조를 원본결과에 남겨야 한다. 토큰값 기록 금지.
- 소비자: analysis.py 844대 + completion.py 1395대. 세션목록 선언만으로 계정 자동승격 금지, 중앙 도구이름 예외 추가 금지.

## 7. 미확인

- BOLA 과거실행 원본묶음 미확보. 생산자-소비자 누락은 후보로만 둠.
- 팀원 6개로그 전송실패 실제원인 자료없음. 429 소급금지.
- 완료한도 12 상향은 현행 한도계약(최대6)에 걸림. 한도분리 먼저.

