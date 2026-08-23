"""타임라인에서 '에이전트가 무엇을 했는가'를 뽑는다.

탐지율은 결과 지표라 원인을 설명하지 못한다. 두 시스템의 탐지율이 다를 때
'왜'를 말하려면 행동 자체를 재야 한다. 여기서 재는 것:

  attack_ratio   공격 시도 / 전체 exploit 호출 — 예산이 공격에 쓰였는가
  rebrowse       recon이 이미 본 곳을 '공격 없이 다시 열람' 한 비율 — 인계 실패
                 (공격 목적의 재방문은 낭비가 아니라 의도된 동작이므로 제외한다)
  depth          후보 하나당 평균 시도 횟수 — 한 번 쏘고 포기하는가

세 지표 모두 타깃과 무관하게 정의된다 (Juice Shop 지식 불필요).

  python -m bench.behavior              전체 실행 비교
  python -m bench.behavior <run_id>     한 실행 상세
"""
import collections
import glob
import json
import os
import re
import statistics as stats
import sys

# 공격으로 볼 신호 — 페이로드 문법, 상태 변경 메서드, 경로 조작
# 공격으로 볼 신호. 맨따옴표는 제외한다 — 모든 curl이 -H "..." 와 -d '...' 를
# 쓰기 때문에 따옴표를 세면 거의 모든 명령이 '공격'으로 잡힌다 (실제로 그랬다).
# 여기 있는 것들은 정상 요청에는 나타나지 않는 문법만 남긴 것이다.
_ATTACK = re.compile(
    r"""(  \bOR\b\s+['\"%]?\d|\bAND\b\s+['\"%]?\d   # ' OR 1 / AND 1
        |  \bUNION\b\s+\bSELECT\b|\bDROP\s+TABLE\b
        |  %27|%22                                       # 인코딩된 따옴표
        |  --\s*$|--%20|/\*                              # SQL 주석
        |  \.\./|%2e%2e|\.\.%2f                          # 경로 조작
        |  <script|<img|<svg|onerror=|onload=|javascript: # 스크립트 주입
        |  -X\s*(PUT|PATCH|DELETE)                       # 상태 변경
        |  \$\{|\{\{|__proto__|constructor\[          # 템플릿·프로토타입
        |  \bsleep\(|\bWAITFOR\b|\bpg_sleep\b          # 시간 기반
        )""",
    re.I | re.X)


_ENDPOINT = re.compile(r"https?://[^/\s\"']+(/[A-Za-z0-9/_.-]*)")


def _by_role(run_dir: str) -> dict:
    path = os.path.join(run_dir, "timeline.jsonl")
    if not os.path.exists(path):
        return {}
    role, out = None, collections.defaultdict(list)
    for line in open(path):
        e = json.loads(line)
        if e["kind"] == "agent_start":
            role = e["role"]
        elif e["kind"] == "tool":
            out[role].append(e.get("cmd", ""))
    return out


def analyse(run_dir: str) -> dict | None:
    per = _by_role(run_dir)
    xs = per.get("exploit", [])
    if not xs:
        return None

    seen = {m for c in per.get("recon", []) for m in _ENDPOINT.findall(c)}
    attacks = [c for c in xs if _ATTACK.search(c)]
    # recon이 이미 설명한 곳을 '공격도 하지 않고' 다시 읽은 것만 낭비로 센다.
    # 공격을 위한 재방문까지 세면 개선될수록 지표가 나빠지는 역설이 생긴다.
    rebrowse = sum(1 for c in xs
                   if not _ATTACK.search(c)
                   and any(e in seen for e in _ENDPOINT.findall(c)))

    # 깊이 — 같은 엔드포인트를 연속으로 공격한 최대 길이의 평균
    runs_len, cur, prev = [], 0, None
    for c in xs:
        ep = next(iter(_ENDPOINT.findall(c)), None)
        is_atk = bool(_ATTACK.search(c))
        if is_atk and ep == prev:
            cur += 1
        else:
            if cur:
                runs_len.append(cur)
            cur = 1 if is_atk else 0
        prev = ep if is_atk else None
    if cur:
        runs_len.append(cur)

    return {
        "run_id": os.path.basename(run_dir),
        "exploit_calls": len(xs),
        "attack_ratio": len(attacks) / len(xs),
        "rebrowse": rebrowse / len(xs),
        "depth": stats.mean(runs_len) if runs_len else 0.0,
    }


def _fmt(rows: list, title: str):
    print(f"\n=== {title} (n={len(rows)}) ===")
    for k, label, f in [("exploit_calls", "exploit 호출", "{:.0f}"),
                        ("attack_ratio", "공격 비율", "{:.0%}"),
                        ("rebrowse", "헛열람 비율", "{:.0%}"),
                        ("depth", "후보당 시도", "{:.1f}")]:
        vals = [r[k] for r in rows]
        sd = stats.stdev(vals) if len(vals) > 1 else 0
        cell = f.format(stats.mean(vals)) + (f" ±{f.format(sd)}" if sd else "")
        print(f"  {label:<12} {cell}")


def main():
    if len(sys.argv) > 1:
        for d in glob.glob(f"runs/*{sys.argv[1]}*"):
            r = analyse(d)
            if r:
                print(json.dumps(r, indent=2, ensure_ascii=False))
        return

    groups = collections.defaultdict(list)
    for d in sorted(glob.glob("runs/*/")):
        r = analyse(d.rstrip("/"))
        if not r:
            continue
        mp = os.path.join(d, "metrics.json")
        ver = json.load(open(mp)).get("code_version") or "legacy" if os.path.exists(mp) else "?"
        groups[ver].append(r)

    for ver, rows in groups.items():
        _fmt(rows, f"code {ver}")


if __name__ == "__main__":
    main()
