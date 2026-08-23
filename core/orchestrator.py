"""에이전트를 파일로 연결한다. 대화를 공유하지 않는 게 핵심.

두 가지 배치를 지원한다:
  순차  recon → exploit → reporter          (에이전트 3)
  팬아웃 recon → exploit×N(동시) → reporter  (에이전트 2+N)

팬아웃을 넣은 이유는 측정에서 나왔다. 벽시계의 97%가 LLM 왕복 대기이고
도구 실행은 3%였다. 즉 시간을 줄이는 유일한 레버는 '동시에 기다리는 것'이다.
비용 쪽도 같이 이득인데, 전문가들이 각자 작은 컨텍스트를 갖기 때문이다 —
단일 exploit은 한 대화에 모든 도구 출력이 쌓여 매 턴 그걸 다시 읽는다.

한 단계가 실패해도 파이프라인을 죽이지 않는다 — 지금까지의 산출물로 리포트까지 간다.
(안정성도 측정 지표이므로 실패는 '기록'되어야지 '크래시'가 되면 안 된다)
"""
from concurrent.futures import ThreadPoolExecutor

import config
from core.agent import run_agent

HALT = ("budget", "refusal")

# 팬아웃 시 전문가별 담당. 타깃 지식이 아니라 취약점 '분류'라 어떤 대상에도 쓰인다.
SPECIALISTS = {
    "injection": "injection flaws — SQL/NoSQL, command, template, and anything where "
                 "input reaches an interpreter",
    "access":    "broken access control — reading or changing data belonging to other "
                 "users, missing authorization on endpoints, IDOR",
    "authn":     "authentication and session handling — credential handling, token "
                 "forgery or misuse, registration and password flows",
    "exposure":  "sensitive data exposure — internal details in responses or errors, "
                 "readable files and directories, secrets and metadata left reachable",
    "logic":     "business logic and input validation — operations accepted in the "
                 "wrong order or with values the flow should reject",
}


def _safe(run_ctx, role, task, role_cfg, out_file, **kwargs):
    """실패해도 파이프라인을 죽이지 않고, 이미 쓰인 산출물이 있으면 살려서 넘긴다.

    out_file을 명시적으로 받는다 — 예전엔 f"{role}.md"로 추측했는데
    exploit은 findings.md, reporter는 report.md라 실패 시 복구가 안 됐다.
    """
    try:
        return run_agent(role, task, role_cfg, run_ctx, out_file, **kwargs)
    except Exception as e:
        run_ctx.m.errors += 1
        run_ctx.m.stopped_reason = f"{role}_failed:{type(e).__name__}"
        run_ctx.event("agent_failed", role=role, error=f"{type(e).__name__}: {e}")
        print(f"  ! {role} 단계 실패: {type(e).__name__} — 다음 단계로 진행합니다")
        return run_ctx.read(out_file, "")


def run_pipeline(run_ctx, profile_name: str):
    prof = config.PROFILES[profile_name]
    roles = prof.roles
    t = config.TARGET_SANDBOX_URL

    # recon을 빼는 구성도 잰다. recon은 LLM 턴의 ~27%를 쓰는데, 전문가들이
    # 각자 담당 범위만 정찰하면 그 공용 정찰이 필요 없을 수 있다.
    # (없앤 턴은 전문가에게 돌려줘서 총 예산을 맞춘다 — config 참조)
    if "recon" in roles:
        recon = _safe(run_ctx, "recon",
                      f"Map the attack surface of {t}. Report what you verified.",
                      roles["recon"], "recon.md")
        base = (f"Here is the recon report for {t}. Confirm real vulnerabilities by "
                f"exploiting them.\n\n<recon_report>\n{recon}\n</recon_report>")
    else:
        recon = ""
        base = (f"Target: {t}. There is no recon report — map what you need yourself, "
                f"then confirm real vulnerabilities by exploiting them.")

    if run_ctx.m.stopped_reason not in HALT:
        fan = getattr(prof, "fan_out", None)
        if fan:
            _fan_out(run_ctx, base, roles["exploit"], fan)
        else:
            _safe(run_ctx, "exploit", base, roles["exploit"], "findings.md")

    _safe(run_ctx, "reporter",
          "Write the final report from these notes.\n\n"
          f"<recon>\n{run_ctx.read('recon.md')}\n</recon>\n\n"
          f"<findings>\n{run_ctx.read('findings.md')}\n</findings>",
          roles["reporter"], "report.md", use_tools=False)

    if not run_ctx.m.stopped_reason:
        run_ctx.m.stopped_reason = "done"


# 대조군: 담당을 주지 않은 일반 탐색자. 이름만 다르고 지시는 같다.
# 팬아웃의 이득이 '전문화' 때문인지 '병렬 독립 컨텍스트' 때문인지 가른다.
GENERIC = "no specific assignment — pursue whatever the recon plan makes most promising"


def _fan_out(run_ctx, base_task: str, role_cfg, names: list):
    """exploit 전문가 N명을 동시에 돌리고 결과를 findings.md 하나로 합친다.

    각자 별도의 대화를 갖는다 — 공유하면 컨텍스트가 다시 커져서 팬아웃의
    비용 이점이 사라진다. 조율이 없으니 담당이 겹칠 수 있는데, 그건 의도한
    트레이드오프다: 조율에 드는 LLM 왕복이 중복보다 비싸다.
    """
    def one(name):
        spec = SPECIALISTS.get(name.split("-")[0] if name.startswith("gen") else name)
        if spec is None:      # 대조군 — 담당 없음
            task = base_task
        else:
            task = (f"{base_task}\n\nYOUR ASSIGNMENT: focus on {spec}. "
                    f"Other specialists cover the rest — do not spend your budget there.")
        return _safe(run_ctx, "exploit", task, role_cfg, f"findings-{name}.md")

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        outs = list(pool.map(one, names))

    merged = "\n\n".join(f"<!-- {n} -->\n{o}" for n, o in zip(names, outs) if o)
    run_ctx.write("findings.md", merged or "(no output)")
    return merged
