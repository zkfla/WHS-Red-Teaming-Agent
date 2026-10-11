# audit-recording: graph.record / 원본조회 / 결과주장·가설·발견 검수 조사

- 범위: 검증 부담 완화 후보 중 기록 계열만 다룬다. 대상은 graph_recording(사실·가설·발견·분기 기록),
  원본조회·evidence 접근, 결과주장·완료 검수, 가설·발견 검수와 그 소비(지식 투영·보고·수렴·완료)이다.
- 방법: 1차 조사는 읽기 전용으로 했다(코드·파일 수정, 전체 검사 실행, 라이브 실행, 서브에이전트 생성 없음).
  R1 독립 리뷰 승인 뒤 같은 작업자가 `graph_recording.py`의 독립 검수 루프 제거와
  `tests/unit/test_semantic_completion_receipts.py`의 기대값 정렬을 구현했다.
  아래 C1–C10 판정은 소스 독해 결과이며, 구현 내용은 `## R1 구현 기록`에 분리해 적는다.
  실행 관측과 독해 추론을 구분해 적는다.
- 기준 시점: 작업 트리의 dev-orchestration 상태를 읽었다. 커밋 고정은 하지 않았으므로 행 번호는
  조사 시점 기준이며 이후 편집에 따라 어긋날 수 있다.
- R1 반영: 새 target 필수 조건은 추가하지 않는다. 무대상 open 초안은 현재도 허용되므로 그대로 둔다.
  이 문서의 완화 후보는 그 전제를 유지한다.

## 핵심 후보 결론: 잠정가설 등록 즉시 검수 제거

- 후보: graph.record 호출 안의 독립(standalone) 가설 즉시 검수 루프를 제거한다.
  가설은 open / INFERRED 저장까지만 하고, 명시적 finding 검수와 실제 행동 가설 검수는 유지한다.
  새 review 옵션을 추가하지 않는다.
- 근거 소스: `src/redteam/orchestration/graph_recording.py:840-887`
  (가려짐 검사 `:843-852`, 검수 불가 `:855`, 대상 연결 `:857-865`,
  모델 검수 `:867-870`, 첫 인용 접근 `:871`, 영수증 `:872-880`,
  상태 매핑·해결 `:881-883`, 실패 시 refused `:886-887`).
- 실제 발생 상황: finding에 가려지지 않은 가설이 하나라도 있으면 매 호출마다 모델 검수 1회가 나간다.
  예산·검수기 없음, 대상 연결 실패, 영수증 발급 실패, 해결 실패 중 하나라도 나면 출력 항목은 refused가 되고
  저장된 가설은 open으로 남는다(출력 refused와 저장 open의 엇갈림이 현재도 존재).
- 판정: 완화 후보로 유지한다. 단 아래 보존 조건(C3·C4·C9·C10)과 함께 가야 한다.
- 미확인: 모델 루프가 `ok=False` 응답을 받고 실제로 무엇을 하는지(재계획·보정·무시 중 어느 것인지)는
  이번 조사에서 끝까지 추적하지 못했다. 아래 C6에 별도로 적는다.

## 조건별 정리

### C1. 독립 가설 즉시 검수 (중복 모델 검수의 대표 사례)

- 소스: `graph_recording.py:840-887`, 상태 매핑 `analysis.py:1470-1490`
  (`hypothesis_status_for_assessment`: success→confirmed, refuted→rejected, 그 외 inconclusive),
  해결 `attack_graph/service.py` 경유 `resolve_hypothesis`.
- 실제 발생: 독립 가설 등록 직후 매번 `verify_claim_with_budget` 호출. 같은 가설을 finding이 함께
  제출하면 finding 쪽 검수로 가려져 독립 검수는 건너뛴다(`:843-854`).
- 판정: 완화. 등록(open 저장)과 검증(명시 finding 검수·실제 행동 가설 검수)을 분리한다.
- 미확인: 호출 빈도·비용 실측은 하지 않았다. 부담 주장은 구조적 중복에 근거하며 수치 근거는 없다.

### C2. 빈 evidence_refs 초안

- 소스(변경 전): 허용은 `HypothesisDraft`(`graph_recording.py:134-139`, `evidence_refs` 기본 빈 목록)와
  `_write_hypothesis`(`:691-760`, 빈 ref도 저장 경로 진행, 대상은 명시 target 또는 인용 대상 유도).
  실패는 제거된 독립 검수에서 발생했다. 대상 없음이면 `target linkage`(구 `:857-865`),
  명시 대상이 있어도 `draft.evidence_refs[0]`(구 `:871`)에서 IndexError 후 refused(구 `:886-887`).
  검수기 자체도 ref 없음이면 unknown(`analysis.py:655-670` 부근)이다. R1 구현 뒤에는 이 실패 경로가
  없으며, 빈 인용·무대상 초안 모두 `_write_hypothesis`의 open 등록 그대로 open 응답한다.
- 변경 전 실제 발생: 빈 인용+무대상 초안은 저장(open)된 뒤 출력 refused. 빈 인용+명시 유효 대상 초안도
  검수 단계에서 refused. 즉 변경 전에는 저장은 되는데 응답은 실패로 나온다.
- 최종 판정(R1 구현): 빈 인용·무대상 모두 open 등록·open 응답이다. 새 target 필수 조건은 넣지 않으며,
  대상 없음은 미해결 잠정 추론으로 남는다. 무대상 open 초안은 변경 전부터 허용된 저장이므로
  이번 변경은 응답(refused→open)만 의도대로 바꾼다.
- 미확인: 빈 인용 가설이 지식 투영·보고에서 실제로 어떻게 보이는지 라이브로 확인하지 않았다.
  코드상으로는 대상 없는 가설도 목록에는 오를 수 있다(`knowledge.py:1005-1163`이 대상 없이도 항목 구성).

### C3. finding 검수·승격 (보존)

- 소스: `_promote_finding`(`graph_recording.py:961-1240`), 검수 `:1080` 부근,
  해결 `:1181-1182`, 영속 후 해결 원칙(영수증 기록 뒤 `resolve_hypothesis`),
  승격 게이트 `findings.py:60-137` (security_check 한정, VIOLATION 한정, 최소 연결 증거 1개,
  가설 필수·confirmed 필수, claim_key 중복 거부).
- 실제 발생: finding 제출마다 의미 검수+영수증+해결+승격을 한 번에 수행. 검수 실패·미지원·미해결이면
  finding refused이며 가설 해결도 해당 경로에서는 일어나지 않는다.
- 판정: 유지. 완화 후보의 검증 축이므로 손대지 않는다.

### C4. 중복 모델 검수 (독립 가설 vs finding-포함 가설 vs 실제 행동 가설 vs 복구)

- 소스: 독립 가설 검수 `graph_recording.py:867`, finding 검수 `:1080`,
  실제 행동 가설 해결 `nodes/verify.py:496-540` (관측 전용·검증 실패 시 inconclusive로 내림),
  `update_hypothesis`는 open 기록·개정이력만 남기고 해결하지 않음(`engine.py:1760-1830` 부근),
  복구 경로 해결 `recovery.py:702-709`, 안전망 inconclusive 종결
  (`nodes/update.py:50-60` 차단 시, `nodes/worker.py:160-175` 예외 시, `runner.py:545-560` 포기 시).
- 실제 발생: finding에 가려진 가설은 독립 검수를 건너뛰므로 이중 검수는 finding 미포함 독립 가설에서만
  발생. 실제 행동 가설(제안 hypothesis_id)은 VERIFY 노드가 해결하므로 독립 검수와는 다른 대상이다.
- 판정: 완화(C1 제거) + 유지(VERIFY·finding·복구·안전망). `update_hypothesis`와 잠정 등록의 분리는
  이미 존재하는 구조이므로 새로 만들 것이 없다.
- 미확인: 복구 경로의 전체 분기 조건은 발췌만 읽었다. 전체 live 기대치(`_verify`) 경로는 부분 독해다.

### C5. 시스템 소유 참조의 반복 제출

- 소스: 모델은 증거 id·raw_path·값·사실명·대상만 제출하고, 실행·호출 id와 원본 판정은 코드가
  증거에서 복원한다(`graph_recording.py:1-40` 독스트링, 사실 기록 `:360-470`,
  `evidence_access.py`의 `raw_from_metadata`·`locate_raw`(missing/other_run/no_raw/ok),
  `EvidenceReader.read`(not_found/run_mismatch/no_raw/ok, 윈도우·절단 보고),
  `read_for_runtime` 최근 10개 유지·`restore_reads` 원본 재구성).
- 실제 발생: 호출마다 모델이 id를 다시 적어 내지만, 권위 원본(action·invocation·제안·대상)은
  저장된 증거에서 코드가 읽는다. 모델이 원본을 단언할 수 없는 구조다.
- 판정: 유지(구조는 이미 올바름). 줄일 수 있는 것은 모델이 id를 반복 제출하는 횟수가 아니라
  그 제출을 트리거하는 호출 횟수(C1 제거가 그 역할을 한다).
- 미확인: 윈도우 재조회 빈도 실측 없음.

### C6. 부분 거절 가능성 (항목별 refused와 호출 전체 ok)

- 소스: 항목별 refused(`_refused_entry`, 사실 `:360-470`, 가설 `:691-760`,
  finding `:961-1240`, 분기 말미), 합산 `refused_any`와 `LocalToolResult(ok=not refused_any)`
  (호출 말미 `:930-960` 부근), 전달 규격 `local_tools.py:58-70`, `:137-155`.
- 실제 발생: 하나라도 refused면 호출 전체가 `ok=False` + "some items refused; see payload".
  현재는 독립 가설 검수 실패가 호출 전체를 실패로 만든다. 완화 후에는 그 실패 원인이 사라지므로
  동일 입력의 호출 결과가 False→True로 바뀐다(의도된 변경).
- 판정: 유지(부분 거절 구조 자체는 둔다). 다만 C1 제거의 결과로 바뀌는 ok 값의 소비 영향을
  계획 리뷰에서 명시해야 한다.
- 미확인: `ok=False`를 받는 실행·재계획 소비의 실제 분기를 끝까지 추적하지 못했다.
  제품 소스에서 `graph.record` 문자열 직접 분기는 조립·등록 외에 확인되지 않았다.

### C7. 과도한 재수집·재검수 방지 장치 (보존)

- 소스: 동일 스냅샷 재제출의 결정적 ID 재사용(가설 안정 ID `:730-760`, finding claim_key
  `findings.py:claim_key`와 `existing_claims` 중복 거부 `graph_recording.py:890-900`),
  사실의 원본 대조(값 동등·raw_path·대상 일치 `:380-470`), 영수증 재사용 경로
  (완료 검수의 영수증→원본 전개 `completion.py:1290-1360`).
- 실제 발생: 동일 내용 재기록은 ID·키 수준에서 합쳐진다. 원본이 바뀌면 ID·키가 달라져 재검수된다.
- 판정: 유지. 새 영수증·지문·전역 무효화를 추가하지 않는다(피드백 종합의 B5·B6 축소 방향과 일치).

### C8. 사실 값·경로·자료형 검사 (진단 개선은 허용, 비교 완화는 조건부)

- 소스: 값 불일치·경로 없음·대상 불일치·예약어 사실명 거부(`graph_recording.py:380-470`,
  `_is_reserved_fact_name`), 보존 원본 우선(`evidence_access.py`).
- 실제 발생: 모델이 제출한 값과 보존 원본이 다르면 refused. 대괄호 자동 제거·문자열 변환 같은
  관대 처리는 없다.
- 판정: 유지. 진단 문구 구체화만 허용하고 자동 변환·경로 자동 수정은 넣지 않는다.
- 미확인: JSON 자료형 경계(참/1 등) 실측 재현은 하지 않았다.

### C9. open 가설의 지식·보고·수렴 소비 (보존)

- 소스: 지식 투영은 open도 항목으로 보여주되 지지·반박에는 명시 기록을 요구
  (`knowledge.py:1005-1163`, 특히 `:1064-1104`); 결론 지문은 confirmed·refuted·rejected 중
  원본 뒷받침 있는 것만 포함(`:1735-1790` 부근 `collect_progress_content`).
  보고는 open 전체 목록과 필수 범위 분리를 함께 투영(`reporting.py:451-600`,
  `:460-465` 분리, `:563-600` ready 조건).
  무결성 보고는 open을 unresolved로 올리되(`integrity.py:107-108`) 차단(blocking)에는 넣지 않는다
  (`:47-70`). 수렴은 필수 범위 unresolved만 차단하고 나머지는 deferred 정보로 둔다
  (`convergence.py:30-90`). 필수 범위는 required 완료조건의 hypothesis_id에서 온다
  (`app/bootstrap.py:760-790`).
- 실제 발생: open 가설이 있어도 비필수면 보고·수렴이 막히지 않는다. 필수 가설이 open이면
  release·수렴이 막힌다. 완료 검수도 open 가설로는 확인·반박 기준을 통과할 수 없다
  (`completion.py:1757-1772`).
- 판정: 유지. 완화 후 open이 늘어나도 잠정 추론으로 소비되며 성공으로 둔갑하지 않는다.
  (원본 없는 관측 확정으로 읽히지 않게 한다.) 필수 가설이 open으로 남는 것은
  정상적인 미완료 신호이므로 억지로 닫지 않는다.
- 미확인: 필수 범위 산정에서 graph.record 생성 가설이 실제로 포함되는 경우는
  완료조건에 hypothesis_id를 건 경우뿐이라는 점을 코드상으로만 확인했고 라이브로 확인하지 않았다.

### C10. 원본·대상·계정 증명 검사 (보존)

- 소스: 원본 불변 검사 `_posthoc_source_gap`(`analysis.py:242-270`),
  대상 접지 `claim_target_is_grounded`(`:271-340`),
  검수용 청구 로드 `resolve_claim_for_review`(`:343-400`),
  최종 승인 영수증 판별 `is_final_admission_receipt`(`:1453-1470`),
  영수증 발급 `append_posthoc_claim_receipt`(`:1494-1620` 부근,
  실행·호출 쌍과 ref 필수, 모델 입력이 아닌 인용 증거에서 원본 채취).
- 실제 발생: 독립 가설·finding 검수 모두 인용 증거의 run·engagement 일치, 원본 쌍 보존,
  보존 raw 응답 일치, 대상 접지를 코드로 확인한 뒤에야 모델 의미 검수로 넘어간다.
- 판정: 유지. 이 검사를 완화하면 근거 없는 확정이 열리므로 이번 후보에 포함하지 않는다.
- 미확인: live 기대치(`_verify`) 전체 분기는 부분 독해다. 의미 검수 모델 응답 스키마의
  세부 분기는 발췌 수준으로만 확인했다.

## 조사 inventory

- 읽은 파일(행은 조사 시점 기준, 부근 포함):
  - `AGENTS.md` 전체.
  - `docs/agent/validation-contract-20261005/review-proposal-synthesis-20261006.md` 전체(8장 포함).
  - `src/redteam/orchestration/graph_recording.py` 전수. 핵심: `:1-70`, `:73-74`,
    `:134-180`, `:280-380`, `:380-470`, `:691-760`, `:840-900`, `:890-960`,
    `:961-1090`, `:1090-1240`, `:1240-1370`.
  - `src/redteam/orchestration/evidence_access.py` 전체(269행).
  - `src/redteam/orchestration/result_claims.py` 전체(100행).
  - `src/redteam/orchestration/knowledge_validation.py` 전체(166행).
  - `src/redteam/orchestration/findings.py` 전체(137행).
  - `src/redteam/orchestration/analysis.py` 부분: `:90-170`, `:242-430`, `:640-900`,
    `:1453-1620` 부근.
  - `src/redteam/orchestration/completion.py` 부분: `:1253-1360`, `:1690-1780`.
  - `src/redteam/orchestration/completion_proposal.py` 앞부분(미해결 기록 계약).
  - `src/redteam/orchestration/engine.py` 부분: `:1740-1870`(`update_hypothesis`).
  - `src/redteam/orchestration/knowledge.py` 부분: `:427-530`, `:1005-1165`,
    `:1165-1230`, `:1730-1790`.
  - `src/redteam/orchestration/reporting.py` 부분: `:360-470`, `:451-620`.
  - `src/redteam/attack_graph/integrity.py` 전체(핵심 `:25-70`, `:100-115`).
  - `src/redteam/attack_graph/service.py` 부분: `:80-260`(읽기·쓰기·무결성 위임).
  - `src/redteam/orchestration/convergence.py` 부분: `:30-100`.
  - `src/redteam/app/bootstrap.py` 부분: `:750-810`(필수 가설 산정·보고 투영).
  - `src/redteam/orchestration/nodes/verify.py` 부분: `:460-560`.
  - `src/redteam/orchestration/nodes/update.py` 부분: `:30-80`.
  - `src/redteam/orchestration/nodes/worker.py` 부분: `:150-185`.
  - `src/redteam/orchestration/runner.py` 부분: `:535-570`.
  - `src/redteam/orchestration/recovery.py` 부분(가설 해결 `:700-710` 포함, 발췌).
  - `src/redteam/orchestration/local_tools.py` 부분: `:50-175`.
  - `tests/unit/test_semantic_completion_receipts.py` 부분: `:700-1050`
    (기록 회귀 묶음, 독립 가설 확정·finding 한 쌍·차용 금지 사례).
- 추가로 훑은 파일: `completion_proposal.py`, `semantic_completion.py` 미정리분은 이번 항목과
  직접 연결되는 부분만 보았다.
- 읽지 않은 것: `cognition.py`의 검수 문맥 구성(A5) 전체, `engine.py`의 증거 읽기·결정 분기 전체,
  `analysis.py`의 live `_verify` 전체, CLI 표시·보고 소비(`report.py`, `status.py`)의 세부 분기,
  `graph.record` 호출부의 모델 프롬프트 문구.

## 영향 검사 목록 (실행하지 않음 — 조사 시점)

- `tests/unit/test_semantic_completion_receipts.py`의 graph.record 회귀 묶음 전체가 1차 영향권이다.
  특히 독립 가설 확정 기대 검사는 완화 후 깨질 것으로 보이며(가설 open 유지·모델 호출 없음·
  해당 가설로의 확인형 완료 불통과), finding 경유나 VERIFY 노드 경유로 검증하도록 검사 흐름을
  고쳐야 한다.
- `tests/unit/test_orchestration_convergence.py`, `tests/unit/test_mission_reporting.py`,
  `tests/unit/test_orchestration_reporting.py`는 open·finding 소비 회귀용으로 함께 봐야 한다.
- 위 목록은 코드 독해로 추린 것이며 실행 결과로 확인한 것이 아니다.

## 남은 미확인 (계획 리뷰용)

- 모델 루프의 `ok=False` 실제 소비 분기(재계획·보정·무시).
- 필수 범위 산정에 graph.record 생성 가설이 들어가는 실제 경우의 빈도와 라이브 동작.
- 부담 수치(호출·토큰·시간) 실측. 중복 구조는 확인했으나 규모는 측정하지 않았다.
- `analysis.py` live `_verify` 전체와 의미 검수 스키마 세부.
- 지식 투영·보고에서 빈 인용·무대상 open 가설의 실제 표시.

## 작업 규칙 준수 기록

- 조사 단계에서는 이 문서만 작성하고 다른 파일은 수정하지 않았다. R1 구현 단계의 변경은
  `## R1 구현 기록`에 적은 파일로 한정한다.
- 분석 과정에서 얻은 추론을 실행 검증 결과로 쓰지 않았다. 위 미확인은 미확인으로 남긴다.

## R1 구현 기록

- 변경 파일:
  - `src/redteam/orchestration/graph_recording.py`: 독립 가설 즉시 검수 루프 제거
    (검수·영수증·해결 일괄, 기존 `:840-887`), open 등록 유지, finding 검수·VERIFY·복구·안전망 유지.
    모델 노출 설명자(descriptor)에 open 추론 문구 추가, 모듈 계약 문구에 독립 가설 무검수 명시,
    `HypothesisDraft` 스키마 설명에 open 추론·선택 인용·선택 대상 문구 추가.
    `claim_target_is_grounded`를 초안 승인 조건으로 추가하지 않았고 새 target 필수도 넣지 않았다.
  - `docs/contracts/attack-graph.md`: finding 생산 서술 정정. 기존 `never produced` 문구를
    현재 생산 경로(위반 기대치·명시 finding 검수+승격·검증 해결)로 좁게 고쳤다.
  - `tests/unit/test_semantic_completion_receipts.py`: 독립 가설 확정 기대 검사를
    open 등록·무모델·무확정 검사로 교체하고, 명시 보안 finding 뒤 confirmed·영수증 소비 검사를 추가했다.
    빈 인용 open 등록, 검수기 없음 open 등록, 확정 가설 재등록 상태 유지 검사를 추가했다.
    빈 근거·다른 run 거절·혼합 부분 수용 검사는 그대로 두었다.
  - 본 문서(`audit-recording.md`): 1차 인벤토리 + 본 구현 기록.
- 금지 준수: 전체 검사·라이브 실행·커밋·푸시를 하지 않았다. 관련 검사는 소유 테스트 파일만 실행한다.
- 검사 결과: 아래 최종 보고에 적는다(소유 파일 1개). dev 통합 없이 dev-orchestration 작업 트리에 둔다.
