"""runs/ 아래 모든 metrics.json을 모아 D안의 결론 표를 만든다.

  python -m bench.compare            코드 버전별로 분리 (기본)
  python -m bench.compare --pool     버전 무시하고 합침 (경고와 함께)

코드를 고치면 같은 프로파일이라도 다른 시스템이다. 섞어서 평균 내면
개선 효과가 옛 데이터에 희석돼 사라진다 — 기본값을 분리로 둔 이유다.
"""
import glob
import json
import statistics as stats
import sys
from collections import defaultdict

FIELDS = [
    ("containers",     "컨테이너",   "{:.0f}"),
    ("agents",         "에이전트",   "{:.0f}"),
    ("wall_sec",       "시간(s)",    "{:.0f}"),
    ("cost_usd",       "비용($)",    "{:.4f}"),
    ("llm_calls",      "LLM호출",    "{:.1f}"),
    ("tool_calls",     "도구호출",   "{:.1f}"),
    ("nonzero_exits",  "헛발질",     "{:.1f}"),
    ("errors",         "장애",       "{:.1f}"),
    ("output_tokens",  "출력토큰",   "{:.0f}"),
    ("newly_solved",   "해결수",     "{:.1f}"),
    ("detection_rate", "탐지율",     "{:.1%}"),
]


def load(pool: bool = False):
    groups, dropped = defaultdict(list), []
    for p in sorted(glob.glob("runs/*/metrics.json")):
        m = json.load(open(p))
        if m.get("scoring_failed"):
            dropped.append((m["run_id"], m["scoring_failed"]))
            continue
        ver = m.get("code_version") or "legacy"
        key = f"{m['system']}/{m['profile']}"
        if not pool:
            key += f"@{ver}"
        groups[key].append(m)
    return groups, dropped


def main():
    pool = "--pool" in sys.argv
    groups, dropped = load(pool)
    if not groups:
        print("runs/ 에 유효한 metrics.json이 없습니다.")
        return

    if dropped:
        print("채점 무효로 제외된 실행:")
        for rid, why in dropped:
            print(f"  · {rid} — {why}")
        print()

    if pool:
        for name, rows in groups.items():
            vers = {r.get("code_version") or "legacy" for r in rows}
            if len(vers) > 1:
                print(f"⚠ {name}: 서로 다른 코드 버전 {sorted(vers)}를 합쳐 평균냅니다.\n")

    names = list(groups)
    w = max(len(n) for n in names) + 2
    header = f"{'지표':<12}" + "".join(f"{n + f'(n={len(groups[n])})':>{w+8}}" for n in names)
    print(header)
    print("-" * len(header))
    for key, label, fmt in FIELDS:
        row = f"{label:<12}"
        for n in names:
            vals = [g.get(key) or 0 for g in groups[n]]
            mean = stats.mean(vals)
            sd = stats.stdev(vals) if len(vals) > 1 else 0.0
            cell = fmt.format(mean) + (f" ±{fmt.format(sd)}" if sd else "")
            row += f"{cell:>{w+8}}"
        print(row)

    # 합집합 — 평균과 다른 이야기를 하므로 함께 본다
    print()
    for n in names:
        union = sorted({k for g in groups[n] for k in g.get("solved_keys", [])})
        per = stats.mean([g.get("newly_solved") or 0 for g in groups[n]])
        print(f"  {n}: 실행당 평균 {per:.2f}개 / 합집합 {len(union)}개")

    base = next((n for n in names if n.startswith("decepticon")), None)
    if base and len(names) > 1:
        print(f"\n=== {base} 대비 ===")
        for n in names:
            if n == base:
                continue
            print(f"\n[{n}]")
            for key, label, _ in FIELDS:
                b = stats.mean([g.get(key) or 0 for g in groups[base]])
                v = stats.mean([g.get(key) or 0 for g in groups[n]])
                if b:
                    print(f"  {label:<10} {(v - b) / b * 100:+.1f}%")


if __name__ == "__main__":
    main()
