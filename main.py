"""진입점.

  python main.py --profile lite            # 1회 실행 (스코어보드 리셋 포함)
  python main.py --profile lite --repeat 3 # 3회 반복 (LLM은 비결정적 → 평균이 필요)
  python main.py --no-reset                # 리셋 없이 (디버깅용)
"""
import argparse
import json

from dotenv import load_dotenv

load_dotenv()

import config
from bench import juiceshop
from core.metrics import Run
from core.orchestrator import run_pipeline


def one_run(profile: str, do_reset: bool) -> dict:
    if do_reset:
        print("· 타깃 리셋 중…")
        try:
            juiceshop.reset()
        except juiceshop.ContaminatedError as e:
            print(f"\n[중단] 측정 오염\n{e}\n")
            raise SystemExit(2)

    before = juiceshop.snapshot()
    started_at = juiceshop.container_started_at()
    print(f"· 분모 {before['denominator']}개 / 이미 풀린 것 {len(before['solved'])}개")

    with Run("lite", profile) as run:
        # 구조 지표는 설정에서 유도한다. 상수로 박아두면 프로파일을 바꿔도 그대로 남아
        # '컨테이너 2개'가 거짓이 된다 — 이건 D안의 주 지표라 더더욱 안 된다.
        run.m.containers = config.LITE_CONTAINERS
        run.m.agents = config.PROFILES[profile].agent_count
        run.m.solved_before = len(before["solved"])
        run.m.denominator = before["denominator"]
        print(f"· run_id = {run.m.run_id}  (code {run.m.code_version})")
        try:
            run_pipeline(run, profile)
        finally:
            # 채점이 실패하면 '탐지 0건'과 구분되어야 한다. 그리고 채점 실패가
            # 파이프라인의 진짜 예외를 가리지 않도록 자체 try로 감싼다.
            try:
                if juiceshop.container_started_at() != started_at:
                    run.m.scoring_failed = "target_restarted_midrun"
                    print("  ! 타깃이 실행 중 재시작됨 — 스코어보드가 초기화되어 채점 무효")
                else:
                    after = juiceshop.snapshot()
                    sc = juiceshop.score(before, after)
                    run.m.solved_after = sc["solved_after"]
                    run.m.solved_keys = sc["solved_keys"]
            except Exception as e:
                run.m.scoring_failed = f"{type(e).__name__}: {e}"
                print(f"  ! 채점 실패: {type(e).__name__} — 탐지 0건과 다릅니다")

    m = json.load(open(run.path("metrics.json")))
    tail = (f"  [채점무효: {m['scoring_failed']}]" if m["scoring_failed"]
            else f"  신규해결 {m['newly_solved']}/{m['denominator']}  {m['solved_keys']}")
    print(f"  → {m['wall_sec']}s  ${m['cost_usd']:.4f}  "
          f"LLM {m['llm_calls']}회  도구 {m['tool_calls']}회{tail}")
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=config.DEFAULT_PROFILE, choices=list(config.PROFILES))
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()

    results = [one_run(a.profile, not a.no_reset) for _ in range(a.repeat)]

    if len(results) > 1:
        n = len(results)
        avg = lambda k: sum(r[k] for r in results) / n
        print(f"\n=== {a.profile} 평균 (n={n}) ===")
        print(f"  시간 {avg('wall_sec'):.1f}s | 비용 ${avg('cost_usd'):.4f} | "
              f"LLM {avg('llm_calls'):.1f} | 도구 {avg('tool_calls'):.1f} | "
              f"탐지율 {avg('detection_rate')*100:.1f}%")
