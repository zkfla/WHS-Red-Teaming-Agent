"""ReAct 루프 하나. 이게 '경량 하네스'의 전부다 — 프레임워크 없이 ~60줄.

경량화하면서 유지한 두 가지 패턴 (Decepticon에서 배운 것):
  · 컨텍스트 격리 — 에이전트가 끝나면 대화 기록은 버리고 산출물 파일만 남긴다
  · 계획-실행 분리 — 스코프 가드를 도구 계층에서 강제 (tools/shell.py)
"""
import core.llm as llm
from tools import shell
from agents.prompts import BY_ROLE


def run_agent(role: str, task: str, role_cfg, run_ctx, out_file: str,
              use_tools: bool = True) -> str:
    """역할 하나를 끝까지 실행하고 산출물 텍스트를 돌려준다."""
    system = BY_ROLE[role]
    messages = [{"role": "user", "content": task}]
    tools = [shell.TOOL_SPEC] if use_tools else None

    run_ctx.event("agent_start", role=role, model=role_cfg.model,
                  effort=role_cfg.effort, max_steps=role_cfg.max_steps)

    final, stop = "", "done"
    for step in range(role_cfg.max_steps):
        if run_ctx.over_budget():
            stop = "budget"
            break

        resp = llm.call(role_cfg=role_cfg, system=system, messages=messages,
                        tools=tools, run_ctx=run_ctx)

        if resp.stop_reason == "refusal":
            stop = "refusal"
            final = llm.text_of(resp)
            break

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            final = llm.text_of(resp)
            break

        messages.append({"role": "assistant", "content": resp.content})

        # 병렬 tool_use는 결과를 '한 개의 user 메시지'에 모아 돌려줘야 한다
        results = []
        for tu in tool_uses:
            out, code = shell.run(tu.input.get("command", ""),
                                  tu.input.get("purpose", ""), run_ctx)
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": out, "is_error": code not in (0,)})
        messages.append({"role": "user", "content": results})
    else:
        stop = "max_steps"
        # 스텝을 다 썼으면 도구 없이 한 번 더 불러 지금까지의 결론을 받아낸다.
        # 이 호출이 실패해도 12스텝의 작업을 통째로 잃으면 안 되므로 감싼다.
        if run_ctx.over_budget():
            stop = "max_steps+budget"
        else:
            messages.append({"role": "user",
                             "content": "Step budget reached. Write your final report now."})
            try:
                final = llm.text_of(llm.call(role_cfg=role_cfg, system=system,
                                             messages=messages, run_ctx=run_ctx))
            except Exception as e:
                stop = f"max_steps+summary_failed:{type(e).__name__}"
                run_ctx.m.errors += 1
                run_ctx.event("summary_failed", role=role, error=str(e)[:200])
                # 마무리 호출이 죽었으면 마지막 어시스턴트 텍스트라도 건진다
                final = _last_assistant_text(messages)

    run_ctx.write(out_file, final or "(no output)")
    run_ctx.event("agent_end", role=role, stop=stop, chars=len(final),
                  cost_so_far=round(run_ctx.m.cost_usd, 4))
    if stop in ("budget", "refusal"):
        run_ctx.m.stopped_reason = stop
    # 여기서 messages는 버려진다 = 컨텍스트 격리. 다음 에이전트는 파일만 읽는다.
    return final


def _last_assistant_text(messages: list) -> str:
    """마무리 호출이 실패했을 때의 최후 수단 — 지금까지 나온 어시스턴트 텍스트."""
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        parts = [b.text for b in m["content"]
                 if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip()]
        if parts:
            return "\n".join(parts).strip()
    return ""
