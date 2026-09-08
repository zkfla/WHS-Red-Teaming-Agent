# G0 착수 기준선 — 2026-09-08

상태: **기존 baseline PASS / 신규 correctness gate FAILED / G0 IN_PROGRESS**

이 기록은 성능 개선 receipt가 아니다. 부하 benchmark와 G1 이후 최적화는 아직 실행하지 않았다.

## 환경·보존

- worktree: `C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent-redteam-core`
- HEAD: `a01417cb241fc437ce0556ab0d9d5afb0381e8a2` / `redteam_core`
- Python 3.13.14, pytest 8.4.2, Windows. 해당 worktree의 `src`를 PYTHONPATH로 지정.
- 시작 당시 변경은 앞선 계획 문서 2개와 `PLANS.md`뿐. 보존하고 진행 상태만 갱신했다.
- 변경 전 175 tests PASS (`21.83s`). 부하 성능 수치가 아니다.
- 기존 Batch A~F 완료 문서·donor checkout·remote는 변경하지 않았다.
- `src/` 변경, commit/push, 외부 provider 호출은 없다.

## source freeze 분리

역사적 Git blob과 현재 local core 모두 다음 exact-byte tree SHA를 검증했다.

`53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c`

`tests/baselines/generic_core_release.json`에 historical commit/hash와 approved release/hash를 분리했다. 현재 승인 값은 여전히 Batch F다. 기존 reference test는 승인된 release hash를 검사하고 별도 test는 역사적 Git blob을 검사한다. 자동 expected 갱신·skip·xfail은 없다.

초기 historical read에서 `git archive`가 이 환경의 줄바꿈 변환을 적용해 raw blob과 다른 hash를 냈다. 현재 helper는 `git ls-tree` + `git cat-file --batch`로 **원본 blob bytes**를 읽는다. 원본 코드를 바꾸거나 줄바꿈을 임의 정규화한 것이 아니다. historical test에는 local Git과 해당 commit object가 필요하며, 없으면 명확히 실패한다.

## 이번 구현

| 산출물 | 범위 | 결과 |
|---|---|---|
| `tests/performance/baseline.py` | historical/current hash·release manifest | PASS |
| `tests/performance/oracles.py` | Knowledge 집합 모델, small exhaustive graph, transition/completion specification | 구현 |
| `tests/performance/graph_adapter.py` | primitive fixture → registered production graph | 구현 |
| `tests/performance/test_oracles.py` | freshness/conflict/inferred/guard/bridge/cycle/수렴 경로 | 2개 correctness FAIL |
| `tests/performance/telemetry.py` | allowlisted stage, immutable numeric measurement, bounded optional collector | harness 수준 PASS |
| `tests/performance/test_telemetry.py` | observer/clock 실패, raw 비노출, canonical commit 동등성 | 6 PASS |
| `tests/performance/reproduce_frontier.py` | 반례·대조군 permutation과 JSON receipt; mismatch면 exit 1 | gate FAILED |

Telemetry는 **offline wrapper**이며 production 전체 call-site 계측은 미완료다. input_items는 호출자가 제공한 수이며 input_bytes/elapsed_ns 미측정은 null이다. raw input/result/error text를 observer에 넘기지 않는다. durable/audit 실패를 무시하는 데 사용하면 안 된다.

Graph oracle은 source/flow/bridge/terminating sink, unregistered edge, target-scoped guard의 제한된 어휘에서 최대 8 nodes/12 edges의 simple path를 열거한다. production pruning helper를 호출하지 않는다. source/sink/context/ceiling이 같은 route는 하나의 reachability로 비교하지만 서로 다른 source를 합치지 않는다. 전체 Frontier 기능에 대한 완전한 oracle이라고 주장하지 않는다.

Transition/completion은 **후속 G7 specification tests**이며 production loop와 연결한 검증이 아니다. Knowledge/graph 비교는 현재 production code를 실제 실행한다.

## 검증

- 변경 후 전체: **246 PASS / 2 FAIL**, 총 248개, `12.27s`.
- 신규 73개: 71 PASS / 2 FAIL. 기존 의미적 175개는 모두 PASS.
- Ruff `check src tests`: PASS. `git diff --check`: PASS.
- 새 실패: `test_distinct_sources_keep_reachability_after_convergence`, `test_nonviable_short_route_must_not_prune_viable_detour[FALSE]`.
- 실패를 숨기는 xfail/skip/expected 변경 없음. 현재 전체 테스트는 exit 1이다.
- full test 시간 차이를 성능 향상률로 계산하지 않는다.

재현(worktree root에서):

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
$env:PYTHONDONTWRITEBYTECODE = '1'
./.venv/Scripts/python.exe -m pytest -ra -o addopts='' -p no:cacheprovider
./.venv/Scripts/python.exe -m tests.performance.reproduce_frontier
```

재현 명령은 같은 반례가 남아 있으면 exit 1. `--output <새 파일>` 옵션은 JSON을 저장하며 기존 파일을 덮어쓰지 않는다.

## 미완료 / 다음 단계

G0 correctness gate가 실패했으므로 G1/G2 최적화 착수 금지. 다음은 [G0-C1 정확성 수정안](GENERIC_CORE_G0_CORRECTNESS_PROPOSAL.md)의 별도 검토·수정이다.

남은 G0: bounded quick/release benchmark runner, 규모/growth datasets, memory/watchdog, p50/p95/paired bootstrap, 단계별 counters, scheduler oracle, production validation/completion 연결의 단계별 gate. **G0 COMPLETE/PASSED_OFFLINE로 표시하지 않는다.** 실제 domain·LLM/provider 실행은 계속 제외한다.
