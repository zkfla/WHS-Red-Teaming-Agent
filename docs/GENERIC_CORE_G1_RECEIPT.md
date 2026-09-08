# G1 Observation lookup 구현 기록

2026-09-08 · PASSED_OFFLINE (기능/정확성) · DEFAULT_OFF · index speed gate INCONCLUSIVE

- `knowledge/lookup.py`: operation-local requirement→Observation index. 외부 응답 cache나 영속 index가 아니다.
- batch commit/freshness에서 index를 한 번 구축하고 slot에 전달한다. 단일 observation API도 keyword를 전달한다. 기존 deep-copy/CAS/validation/atomic assignment는 보존한다.
- `effective_payload_envelope`에 authoritative ID 직접 조회를 추가했다. ID 정렬과 requirement/hash binding을 유지한다.
- `lookup_mode="indexed"`로 선택 가능하며 **기본은 `"full_scan"`**이다. invalid mode는 mutation 전에 거절한다. 저장 schema/Observation ID/semantic revision 계약은 바꾸지 않았다.
- G0 독립 Knowledge oracle을 두 mode에 적용했다. duplicate commit, 여러 batch, freshness, payload lookup, public ledger 변경 후 rebuild, invalid mode, structural scan count를 추가했다.
- 신규/관련 gate **94 PASS**. 기본 경로의 기존 Knowledge/independent oracle 85 PASS도 별도 확인했다.
- 100 Observation/10 requirement freshness의 `.values()` instrumentation: full_scan 10회, indexed 1회. 이는 sorting/deep-copy를 포함한 전체 알고리즘이 O(R+O)라는 주장이 아니다.
- indexed payload의 전체 `.values()` 스캔 0회도 직접 확인했다.
- 미등록 requirement, ID collision, 미래 acquisition, stale CAS, private recomputer mutation 후 예외를 양쪽 mode에서 실행하여 전체 session 원자성을 확인했다. 관련 전체 **112 PASS** (독립 oracle/runner 포함), Ruff PASS.
- paired runner에 `--comparison g1`을 연결했다. commit/freshness/payload × 규모, growth fixture를 동일 clock·동일 ledger로 비교한다. 각 arm의 factory 생성은 timing 밖이지만 case watchdog 안이며, 별도 Python allocation peak는 fixture+operation+verification을 포함한다. invocation 결과의 전체 semantic hash가 다르면 실패한다.

## gate 처리 결과

1. opt-in/off paired workload 연결, 30회 A/B timing 및 별도 peak-memory 비교 완료. 3개 TIMEOUT은 미입증으로 보존하며 기본화하지 않는다.
2. commit failure/unknown requirement/collision/recomputer failure의 indexed 직접 검증은 완료했다.
3. 전체 610 tests 중 608 PASS / 2 FAIL (94.88s). 실패 2개는 이전 G0 source를 가리키는 승인 hash 검사뿐이었다. 모든 semantic/architecture/원자성/독립 oracle 검사가 통과한 후 diff를 검토했고 `296e9b...e2c52`를 `g1-tested-offline-default-off`로 명시적으로 등록한다. historical baseline은 보존한다. 등록 후 freeze 검사는 별도 재실행한다.
4. 기본화 보류 결정은 아래 표와 원시 receipt에 근거한다. G9 최종 조합 검증은 남아 있다. 구조 counter 개선만으로 speed/default-enable을 선언하지 않는다.

승인 source 등록 후 freeze/reference 검증 **10 PASS**. source 변경 없이 승인 metadata만 갱신한 뒤 기존 2개 hash 실패를 해소했다. Ruff PASS, diff whitespace 검사 PASS. 위 608개 전체 의미 회귀 + 후속 freeze 검증을 근거로 기능/정확성 상태를 PASSED_OFFLINE으로 닫는다. 610개가 한 번의 실행에서 모두 PASS였다고 바꿔 적지 않는다.

rollback: keyword를 full_scan으로 설정하거나 생략. 새 durable data 형식은 없다. 실제 domain/provider, commit/push는 수행하지 않았다.

## 30회 단독 A/B 결과와 기본값 결정

[원시 receipt](GENERIC_CORE_G1_RELEASE_AB.json): warmup 5, paired samples 30, source_integrity PASS. source `296e9b9e2dba3dd7ab9a1e616307a4104706c8b37853d14bf616d9e6d30e2c52`. 실행 중 source 변경/병렬 회귀 실행 없음. 7 cases PASS / 3 TIMEOUT. `PASS`는 측정·semantic parity 성공이지 최적화 승인이라는 뜻이 아니다.

| stage / Observations / cycles | p50 full→indexed ms | ratio | paired bootstrap 95% | separate Python peak ratio |
|---|---:|---:|---|---:|
| commit / 100 / 1 | 7.414→7.771 | 1.048 | 0.966..1.132 | 0.995 |
| commit / 1,000 / 1 | 143.342→131.031 | 0.914 | 0.833..1.007 | 0.999 |
| freshness / 100 / 1 | 4.282→4.675 | 1.092 | 0.880..1.180 | 0.998 |
| freshness / 1,000 / 1 | 78.933→74.410 | 0.943 | 0.889..1.031 | 0.999 |
| payload / 100 / 1 | 0.121→0.034 | 0.280 | 0.185..0.307 | 1.196 |
| payload / 1,000 / 1 | 11.601→0.286 | 0.025 | 0.020..0.027 | 1.001 |
| commit / 100 / 10 | 67.059→62.211 | 0.928 | 0.881..0.992 | 0.993 |

commit/freshness/payload 각각 10,000 Observation case는 60초 watchdog TIMEOUT. fixture 구축·warmup·양쪽 30 samples·별도 memory·검증 전체의 제한이다. timing sample 일부를 성공으로 구제하지 않았으며 10,000 scale 성능은 미입증이다. memory cap 157,115,392 bytes; Python peak와 sampled RSS는 다른 지표다.

**결정:** commit/freshness index는 구조 개선은 확인했으나 대표 workload p50 20% 개선을 입증하지 못했다. DEFAULT_OFF 유지, 성능 gate INCONCLUSIVE. payload 직접 조회는 측정된 100/1,000 fixture에서 시간 gate를 충족하고 p95 악화가 없었으며 Python peak 증가는 20% 이내다. G9의 최종 ablation에서 별도 기본화 후보로 평가한다. G1 전체를 속도 향상/default enabled라고 선언하지 않는다. 기준을 바꾸거나 timeout을 제외한 평균으로 승인하지 않았다.
