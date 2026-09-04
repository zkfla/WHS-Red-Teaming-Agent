"""Strix를 Decepticon·경량 코어와 같은 자로 재는 도구.

Decepticon(bench/baseline.py)과 다른 점 하나 — Strix는 완전 자율 CLI라
사람이 붙어 있을 필요가 없다. 그래서 리셋→실행→채점→비용복원까지
한 번에 자동으로 돈다.

  python -m bench.strix            1회
  python -m bench.strix --repeat 8 8회

## 공정성 조건 (Decepticon과 동일하게 맞춘 것)
  · 모델      auth/claude-haiku-4-5 — LiteLLM 프록시 경유, 세 시스템 모두 동일
  · 타깃      같은 Juice Shop, 매 실행 전 초기화
  · 채점      스코어보드 API 자동 집계 (자체 보고 불신)
  · 턴 제한   걸지 않음. Decepticon도 자연스럽게 끝날 때까지 뒀다.
              --max-budget은 폭주 방지용 안전장치일 뿐 정상 실행에선 안 걸린다.

## 비용 복원이 필요한 이유
LiteLLM의 spend 컬럼은 auth/ OAuth 패스스루에서 입력 토큰을 못 잡아
출력만으로 비용을 계산한다(Decepticon 측정에서 4배 과소계상으로 확인).
그래서 metadata.usage_object에서 캐시 토큰을 직접 복원한다.
"""
import argparse
import json
import os
import subprocess
import time

import config
from bench import juiceshop
from core.metrics import Run

STRIX_BIN = ".strix-venv/bin/strix"
ENV = {
    "STRIX_LLM": "openai/auth/claude-haiku-4-5",
    # 127.0.0.1을 명시해야 한다. localhost는 IPv6로 풀려 다른 서비스에 닿을 수 있다.
    "LLM_API_BASE": "http://127.0.0.1:4000",
    "LLM_API_KEY": "sk-decepticon-master",
}
# 샌드박스 컨테이너에서 호스트의 타깃을 보는 주소
TARGET = "http://host.docker.internal:3100"
MODEL = "auth/claude-haiku-4-5"


def _spend_since(ts: str) -> dict:
    """LiteLLM 지출 로그에서 토큰을 복원한다. spend 컬럼은 못 믿는다."""
    q = (f"SELECT COALESCE(SUM((metadata->'usage_object'->>'cache_creation_input_tokens')::bigint),0),"
         f" COALESCE(SUM(COALESCE((metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint,0)),0),"
         f" COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), COUNT(*)"
         f" FROM \"LiteLLM_SpendLogs\" WHERE model='{MODEL}' AND \"startTime\" > '{ts}';")
    out = subprocess.run(
        ["docker", "exec", "decepticon-postgres", "psql", "-U", "decepticon",
         "-d", "litellm", "-t", "-A", "-F", "|", "-c", q],
        capture_output=True, text=True).stdout.strip()
    cw, cr, inp, outp, n = (int(x) for x in out.split("|"))
    P = config.PRICING["claude-haiku-4-5"]
    cost = (inp / 1e6 * P["in"]
            + cw / 1e6 * P["in"] * config.CACHE_WRITE_MULT
            + cr / 1e6 * P["in"] * config.CACHE_READ_MULT
            + outp / 1e6 * P["out"])
    return {"cache_write_tokens": cw, "cache_read_tokens": cr,
            "input_tokens": inp, "output_tokens": outp,
            "llm_calls": n, "cost_usd": round(cost, 4)}


def _db_now() -> str:
    return subprocess.run(
        ["docker", "exec", "decepticon-postgres", "psql", "-U", "decepticon",
         "-d", "litellm", "-t", "-A", "-c", "SELECT now();"],
        capture_output=True, text=True).stdout.strip()


def _containers() -> int:
    """Strix가 띄우는 자체 인프라 컨테이너 수 (타깃 제외)."""
    out = subprocess.run(["docker", "ps", "--filter", "name=strix", "-q"],
                         capture_output=True, text=True).stdout
    return len([l for l in out.splitlines() if l.strip()])


def one_run(log_dir: str = "runs") -> dict:
    print("· 타깃 리셋 중…")
    juiceshop.reset()
    before = juiceshop.snapshot()
    print(f"· 분모 {before['denominator']}개 / 이미 풀린 것 {len(before['solved'])}개")

    mark = _db_now()
    env = {**os.environ, **ENV}
    t0 = time.time()
    with Run("strix", "haiku") as run:
        run.m.containers = 0          # 실행 후 실측으로 덮어씀
        run.m.agents = 0              # Strix는 에이전트 수를 외부에 노출하지 않음
        run.m.solved_before = len(before["solved"])
        run.m.denominator = before["denominator"]
        print(f"· run_id = {run.m.run_id}  (code {run.m.code_version})")
        try:
            p = subprocess.run(
                [STRIX_BIN, "-n", "--target", TARGET, "--max-budget", "3.00"],
                env=env, capture_output=True, text=True, timeout=3600)
            run.m.stopped_reason = "done" if p.returncode == 0 else f"exit{p.returncode}"
            run.write("strix-stdout.log", p.stdout[-40000:])
            run.write("strix-stderr.log", p.stderr[-40000:])
            peak = _containers()
        except subprocess.TimeoutExpired:
            run.m.stopped_reason = "timeout"
            peak = 0
        finally:
            wall = time.time() - t0
            try:
                after = juiceshop.snapshot()
                sc = juiceshop.score(before, after)
                run.m.solved_after = sc["solved_after"]
                run.m.solved_keys = sc["solved_keys"]
            except Exception as e:
                run.m.scoring_failed = f"{type(e).__name__}: {e}"
            for k, v in _spend_since(mark).items():
                setattr(run.m, k, v)
            run.m.containers = peak
            run._t0 = t0                      # finish()가 진짜 경과를 쓰도록
            run.m.per_model = {MODEL: {"calls": run.m.llm_calls,
                                       "in": run.m.input_tokens,
                                       "out": run.m.output_tokens,
                                       "cache_w": run.m.cache_write_tokens,
                                       "cache_r": run.m.cache_read_tokens,
                                       "usd": run.m.cost_usd}}

    m = json.load(open(run.path("metrics.json")))
    tail = (f"  [채점무효: {m['scoring_failed']}]" if m["scoring_failed"]
            else f"  신규해결 {m['newly_solved']}/{m['denominator']}  {m['solved_keys']}")
    print(f"  → {m['wall_sec']:.0f}s  ${m['cost_usd']:.4f}  LLM {m['llm_calls']}회{tail}")
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()
    res = [one_run() for _ in range(a.repeat)]
    if len(res) > 1:
        import statistics as st
        f = lambda k: st.mean([r[k] for r in res])
        print(f"\n=== Strix 평균 (n={len(res)}) ===")
        print(f"  시간 {f('wall_sec'):.1f}s | 비용 ${f('cost_usd'):.4f} | "
              f"LLM {f('llm_calls'):.1f} | 탐지 {f('newly_solved'):.2f}")
