"""변경별 성능 귀속표.

'무엇을 고쳤더니 무엇이 얼마나 좋아졌는가'를 묻는 표다. 정직하게 답하려면
두 가지를 지켜야 한다.

  1) 지표마다 기전이 다르다. 병렬 실행은 벽시계만 건드리고 API 호출 수는
     건드리지 않는다. 한 배치에서 여러 변경을 같이 넣었어도, 각 변경이
     움직일 수 있는 지표가 다르면 기전으로 귀속할 수 있다.

  2) n이 작고 분산이 크면 차이는 노이즈일 수 있다. 탐지율은 실행당 편차가
     ±1.3개라 n=3으로는 대부분의 차이가 유의하지 않다. 행동 지표는 분산이
     훨씬 작아 같은 n에서도 신호가 뚜렷하다 — 그래서 인과 주장은 행동 쪽에서
     하고, 탐지율은 '방향이 어긋나지 않았다' 정도로만 읽는다.

  python -m bench.attribution
"""
import glob
import json
import os
import statistics as stats

from bench.behavior import analyse

# code_version → (라벨, 그 버전에서 들어간 변경)
VERSIONS = {
    "legacy": ("기준선", "최초 프로토타입"),
    "0dfd02ce65": ("코드리뷰", "결함 10건 수정 (계측 정확성)"),
    "099cf56694": ("깊이+배칭+병렬", "프롬프트 재작성 + ThreadPoolExecutor"),
    "590b625d4f": ("동상(추가1회)", "099cf5와 동일 동작, 디스크 해시만 상이"),
    "e10427102c": ("+스텝 30%↓", "위 + exploit 20→14, recon 10→7 (haiku-lean)"),
}

METRICS = [
    ("attack_ratio",  "공격 비율",   "{:.0%}",  "up",   "예산이 공격에 쓰인 비율"),
    ("depth",         "후보당 시도", "{:.1f}",  "up",   "한 번 쏘고 포기하는가"),
    ("rebrowse",      "헛열람 비율", "{:.0%}",  "down", "recon 인계 실패로 인한 낭비"),
    ("calls_per_llm", "도구/LLM턴",  "{:.2f}",  "up",   "한 턴에 몇 개를 태우는가"),
    ("wall_sec",      "벽시계(s)",   "{:.0f}",  "down", "실행 시간"),
    ("cost_usd",      "비용($)",     "{:.3f}",  "down", "실행당 비용"),
    ("newly_solved",  "탐지수",      "{:.1f}",  "up",   "분산 큼 — 참고용"),
]


def collect() -> dict:
    out = {}
    for d in sorted(glob.glob("runs/*/")):
        d = d.rstrip("/")
        mp = os.path.join(d, "metrics.json")
        if not os.path.exists(mp):
            continue
        m = json.load(open(mp))
        b = analyse(d) or {}
        if not b:
            continue
        row = {**m, **b}
        # 도구/LLM턴 — exploit 단계 기준
        row["calls_per_llm"] = _calls_per_llm(d)
        out.setdefault(m.get("code_version") or "legacy", []).append(row)
    return out


def _calls_per_llm(run_dir: str) -> float:
    role, llm, tool = None, 0, 0
    for line in open(os.path.join(run_dir, "timeline.jsonl")):
        e = json.loads(line)
        if e["kind"] == "agent_start":
            role = e["role"]
        elif role == "exploit" and e["kind"] == "llm":
            llm += 1
        elif role == "exploit" and e["kind"] == "tool":
            tool += 1
    return tool / llm if llm else 0.0


def main():
    groups = collect()
    known = [v for v in VERSIONS if v in groups]
    if len(known) < 2:
        print("비교할 버전이 부족합니다. 현재:", list(groups))
        return

    base = known[0]
    print(f"기준선: {VERSIONS[base][0]} (n={len(groups[base])})\n")
    w = 22
    hdr = f"{'지표':<12}" + "".join(
        f"{VERSIONS[v][0] + f'(n={len(groups[v])})':>{w}}" for v in known)
    print(hdr)
    print("-" * len(hdr))

    for key, label, fmt, good, _ in METRICS:
        line = f"{label:<12}"
        b = stats.mean([r.get(key) or 0 for r in groups[base]])
        for v in known:
            vals = [r.get(key) or 0 for r in groups[v]]
            mean = stats.mean(vals)
            sd = stats.stdev(vals) if len(vals) > 1 else 0
            cell = fmt.format(mean) + (f"±{fmt.format(sd)}" if sd else "")
            if v != base and b:
                pct = (mean - b) / b * 100
                arrow = "↑" if pct > 0 else "↓"
                improved = (pct > 0) == (good == "up")
                cell += f" {arrow}{abs(pct):.0f}%{'✓' if improved and abs(pct) > 5 else ''}"
            line += f"{cell:>{w}}"
        print(line)

    print("\n변경 내역")
    for v in known:
        print(f"  {VERSIONS[v][0]:<16} {VERSIONS[v][1]}")

    print("\n주의")
    print("  · 탐지수는 실행당 분산이 커서 n=3으로는 대부분의 차이가 유의하지 않다.")
    print("  · 행동 지표(공격 비율·후보당 시도)는 분산이 작아 같은 n에서도 신호가 뚜렷하다.")
    print("  · 마지막 버전은 프롬프트와 프레임워크를 함께 바꿨다. 기전이 다른 지표로만")
    print("    분리해 읽을 것 — 병렬 실행은 벽시계만, 배칭은 도구/LLM턴만 움직인다.")


if __name__ == "__main__":
    main()
