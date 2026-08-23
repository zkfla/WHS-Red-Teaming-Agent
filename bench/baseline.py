"""Decepticon(baseline)을 '바깥에서' 재는 도구.

내부 계측을 넣을 수 없으니 경량 쪽과 공통으로 잴 수 있는 것만 잰다:
  벽시계 · 컨테이너 수 · 스코어보드 차분 · (가능하면) LiteLLM 지출.
사용법 —
  python -m bench.baseline start     # 리셋 + 스냅샷 + 타이머 시작
  (여기서 Decepticon을 평소대로 실행)
  python -m bench.baseline stop      # 스냅샷 + metrics.json 기록
"""
import json
import os
import subprocess
import sys
import time

import config
from bench import juiceshop
from core.metrics import Run

STATE = "runs/.baseline_pending.json"


def _containers() -> int:
    out = subprocess.run(["docker", "ps", "--filter", "name=decepticon", "-q"],
                         capture_output=True, text=True).stdout
    return len([l for l in out.splitlines() if l.strip()])


def start(reset: bool = True):
    if reset:
        print("· Juice Shop 리셋 중…")
        juiceshop.reset()
    before = juiceshop.snapshot()
    os.makedirs("runs", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"t0": time.time(), "before": before,
                   "containers": _containers()}, f, indent=2)
    print(f"· baseline 타이머 시작 | 분모 {before['denominator']} | "
          f"컨테이너 {_containers()}개")
    print("· 이제 Decepticon을 실행하세요. 끝나면: python -m bench.baseline stop")


def stop(note: str = ""):
    with open(STATE) as f:
        st = json.load(f)
    after = juiceshop.snapshot()
    s = juiceshop.score(st["before"], after)

    run = Run("decepticon", "default")
    run.m.wall_sec = round(time.time() - st["t0"], 2)
    run.m.containers = st["containers"]
    run.m.agents = 16                     # Decepticon 전문 에이전트 수 (문서 기준)
    run.m.solved_before = s["solved_before"]
    run.m.solved_after = s["solved_after"]
    run.m.solved_keys = s["solved_keys"]
    run.m.denominator = s["denominator"]
    run.m.stopped_reason = note or "external"
    m = run.finish()
    os.remove(STATE)

    print(json.dumps({k: m[k] for k in
                      ("run_id", "wall_sec", "containers", "newly_solved",
                       "denominator", "detection_rate", "solved_keys")}, indent=2))
    print("\n※ 토큰/비용은 LiteLLM 지출 로그에서 채워 넣으세요 "
          f"→ {run.path('metrics.json')} 의 input_tokens/output_tokens/cost_usd")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        start(reset="--no-reset" not in sys.argv)
    elif cmd == "stop":
        stop(" ".join(sys.argv[2:]))
    else:
        print(__doc__)
