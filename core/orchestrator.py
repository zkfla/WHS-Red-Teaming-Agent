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

# ── 담당 로스터 ────────────────────────────────────────────────────
# 대상 유형별 담당. Decepticon이 커버하는 도메인을 그대로 유지한다 —
# 경량화는 '기능을 줄이는 것'이 아니라 '같은 기능을 적은 토큰·시간으로'이기 때문이다.
#
# 병렬 실행 + 컨텍스트 격리라는 기전 자체는 도메인과 무관하다. 실제로 순차(3종)에서
# 병렬(6종)로 갈 때 에이전트를 늘리면서 토큰과 시간이 함께 줄었다. 따라서 로스터를
# 넓혀도 최적화 효과는 유지된다는 것이 설계 전제이고, 이 전제는 측정으로 검증해야 한다.
DOMAINS = {
    "web": {
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
    },
    "network": {
        "netrecon":  "service and version enumeration across the reachable address space — "
                     "open ports, banners, protocol fingerprints",
        "netexploit":"remotely reachable service weaknesses — default and reused credentials, "
                     "unauthenticated administrative interfaces, known vulnerable versions",
        "lateral":   "movement between hosts — credential reuse, exposed shares and mounts, "
                     "trust relationships between services",
    },
    "ad": {
        "adrecon":   "directory enumeration — domain, users, groups, computers, trusts, "
                     "group policy, service principal names",
        "adcreds":   "credential attacks against the directory — kerberoasting, AS-REP, "
                     "password spraying, cached and stored secrets",
        "adescalate":"privilege paths inside the domain — delegation, ACL abuse, "
                     "certificate services, group nesting toward higher privilege",
    },
    "cloud": {
        "cloudrecon":"cloud surface enumeration — exposed storage, metadata endpoints, "
                     "public functions and registries, account and tenant structure",
        "cloudiam":  "identity and access misconfiguration — over-permissive roles, "
                     "assumable roles, key material left reachable",
    },
    "mobile": {
        "mobstatic": "package inspection — hardcoded secrets, endpoints, permissions, "
                     "certificate pinning and obfuscation posture",
        "mobdynamic":"runtime and traffic behaviour — insecure storage, transport, "
                     "interprocess surfaces exposed by the app",
    },
    "binary": {
        "binstatic": "static analysis of the target binary — imports, strings, protections, "
                     "obviously unsafe routines",
        "bindynamic":"runtime behaviour — input handling, memory-safety triggers, "
                     "reachable crash states",
    },
    "ics": {
        "icsrecon":  "industrial protocol surface — device and controller identification, "
                     "protocol dialects, exposed engineering interfaces",
    },
    "wireless": {
        "wifi":      "wireless access — authentication posture, exposed management frames, "
                     "credential capture opportunities",
    },
    "supply": {
        "deps":      "dependency and artifact integrity — vulnerable or typosquatted "
                     "components, unsigned artifacts, build and registry exposure",
    },
    # 대상 유형이 확정되지 않았을 때. 어떤 도메인에도 속하는 공통 각도.
    "generic": {
        "surface":   "what the target exposes at all — reachable services, interfaces, "
                     "entry points, and what technology stack they reveal",
        "authnz":    "authentication and authorization anywhere in the target — who can "
                     "act as whom, and where that boundary is not enforced",
        "exposure":  "information the target leaks — credentials, internals, configuration, "
                     "data belonging to others",
        "input":     "anywhere the target accepts input and does something with it — "
                     "where that input reaches an interpreter, a file path, or a query",
    },
}

# 하위 호환: 기존 프로파일이 평면 이름으로 담당을 지정한다.
SPECIALISTS = {k: v for d in DOMAINS.values() for k, v in d.items()}


def domain_of(name: str) -> str:
    """담당 이름이 속한 도메인. 로그·집계용."""
    for dom, members in DOMAINS.items():
        if name in members:
            return dom
    return "unknown"


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


def resolve_fanout(prof, target_domain: str | None) -> list:
    """이번 실행에서 띄울 담당을 정한다.

    프로파일이 담당을 명시하면 그대로 쓰고(재현 실험용), 아니면 대상 도메인의
    담당 전원을 띄운다. 도메인을 모르면 generic으로 떨어진다.

    이 함수가 '범용'을 담보한다 — 아키텍처(병렬+컨텍스트 격리)는 그대로 두고
    로스터만 대상에 맞춰 갈아끼운다. 웹 전용으로 굳어 있던 것을 푼 지점이다.
    """
    if getattr(prof, "fan_out", None):
        return prof.fan_out
    dom = target_domain or config.TARGET_DOMAIN
    return list(DOMAINS.get(dom, DOMAINS["generic"]))


def run_pipeline(run_ctx, profile_name: str, target_domain: str | None = None):
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
        fan = resolve_fanout(prof, target_domain)
        wave2 = getattr(prof, "wave2", None)
        if fan and wave2:
            # 1차 → 동기화 한 번 → 2차. 총 스텝은 순수 팬아웃과 맞춰서
            # '정보 공유' 효과만 분리해 본다 (턴 수를 늘린 효과와 섞이면 해석 불가).
            first = _fan_out(run_ctx, base, roles["exploit"], fan, wave=1)
            second = _fan_out(run_ctx, base, wave2, fan, wave=2, prior=first)
            run_ctx.write("findings.md", f"{first}\n\n{second}")
        elif fan:
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


def _fan_out(run_ctx, base_task: str, role_cfg, names: list, wave: int = 0,
             prior: str = ""):
    """exploit 전문가 N명을 동시에 돌리고 결과를 findings.md 하나로 합친다.

    각자 별도의 대화를 갖는다 — 공유하면 컨텍스트가 다시 커져서 팬아웃의
    비용 이점이 사라진다. 조율이 없으니 담당이 겹칠 수 있는데, 그건 의도한
    트레이드오프다: 조율에 드는 LLM 왕복이 중복보다 비싸다.

    wave/prior 는 2차 웨이브용이다. 1차가 끝난 뒤 딱 한 번 동기화해서
    서로의 발견을 넘긴다 — 예를 들어 authn 담당이 얻은 토큰을 access 담당이
    쓸 수 있게. 동기화 지점이 하나뿐이라 컨텍스트는 거의 안 커진다.
    """
    def one(name):
        run_ctx.event("specialist", name=name, domain=domain_of(name), wave=wave or 1)
        spec = SPECIALISTS.get(name.split("-")[0] if name.startswith("gen") else name)
        if spec is None:      # 대조군 — 담당 없음
            task = base_task
        else:
            task = (f"{base_task}\n\nYOUR ASSIGNMENT: focus on {spec}. "
                    f"Other specialists cover the rest — do not spend your budget there.")
        if prior:
            task += ("\n\nWhat the other specialists found in the first pass is below. "
                     "Use it — credentials, tokens, ids, and confirmed weaknesses are "
                     "reusable. Do not re-confirm what is already proven; build on it.\n\n"
                     f"<first_pass_findings>\n{prior}\n</first_pass_findings>")
        suffix = f"-w{wave}" if wave else ""
        return _safe(run_ctx, "exploit", task, role_cfg, f"findings-{name}{suffix}.md")

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        outs = list(pool.map(one, names))

    merged = "\n\n".join(f"<!-- {n} -->\n{o}" for n, o in zip(names, outs) if o)
    run_ctx.write(f"findings{'-w%d' % wave if wave else ''}.md", merged or "(no output)")
    return merged
