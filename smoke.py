"""M1/M2 점검: 키가 되는지, 도구가 도는지, 캐시가 먹는지 한 번에 확인.

  .venv/bin/python smoke.py
"""
from dotenv import load_dotenv
load_dotenv()

import config
import core.llm as llm
from core.metrics import Run
from tools import shell
from agents.prompts import RECON

with Run("smoke", "lite") as run:
    cfg = config.PROFILES["lite"].roles["recon"]
    print(f"[M1] {cfg.model} 호출…")
    msgs = [{"role": "user", "content":
             "Reply with exactly: PONG. Do not use any tool."}]
    r1 = llm.call(role_cfg=cfg, system=RECON, messages=msgs,
                  tools=[shell.TOOL_SPEC], run_ctx=run, max_tokens=64)
    print("  응답:", llm.text_of(r1) or "(빈 응답)")
    print(f"  usage: in={r1.usage.input_tokens} out={r1.usage.output_tokens} "
          f"cache_w={getattr(r1.usage,'cache_creation_input_tokens',0)} "
          f"cache_r={getattr(r1.usage,'cache_read_input_tokens',0)}")

    print("\n[캐시] 같은 프리픽스로 한 번 더…")
    r2 = llm.call(role_cfg=cfg, system=RECON, messages=msgs,
                  tools=[shell.TOOL_SPEC], run_ctx=run, max_tokens=64)
    cr = getattr(r2.usage, "cache_read_input_tokens", 0)
    print(f"  cache_read={cr} → {'적중 ✅' if cr else '미적중 (프리픽스가 1024토큰 미만일 수 있음)'}")

    print("\n[M2] 도구 실행…")
    out, code = shell.run(f"curl -s -I {config.TARGET_SANDBOX_URL}/ | head -3",
                          "check target is up", run)
    print(f"  exit={code}\n  {out[:250]}")

    run.m.stopped_reason = "done"

print(f"\n총 비용 ${run.m.cost_usd:.6f} | {run.path('metrics.json')}")
