"""D안의 심장: 경량 에이전트와 Decepticon baseline을 '같은 자로' 재는 계측기.

핵심 원칙 — 비교 가능성:
  경량 쪽은 내부 계측(토큰/호출 단위), Decepticon 쪽은 외부 계측(벽시계/컨테이너/스코어보드).
  두 쪽 모두가 반드시 채우는 공통 필드를 정의해두면 표 하나로 나란히 놓을 수 있다.
"""
import glob
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict

import config

# 명령 자신의 exit code와 겹치지 않는 센티넬 (bash의 126/127을 피한다)
REFUSED_EXIT = 199   # 스코프 가드가 막음
HARNESS_EXIT = 198   # docker exec 자체가 실패


@dataclass
class RunMetrics:
    run_id: str
    system: str                 # "lite" | "decepticon" 등 비교 대상 이름
    profile: str                # 실험 조건 (모델 티어 조합)
    target: str
    code_version: str = ""      # 소스 해시 — 다른 코드의 실행을 섞어 평균내지 않기 위해

    # ── 효율성 (양쪽 모두 측정 가능) ──
    wall_sec: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    # ── 구조 복잡도 (정적 값, 수동 기입) ──
    containers: int = 0
    agents: int = 0

    # ── 효과성 (Juice Shop 스코어보드가 자동 채점) ──
    solved_before: int = 0
    solved_after: int = 0
    solved_keys: list = field(default_factory=list)
    denominator: int = 0        # 스코프 내 전체 챌린지 수 (탐지율 분모)

    # ── 안정성 ──
    # errors      = 하네스/인프라 장애 (진짜 불안정성)
    # nonzero_exits = 명령이 0이 아닌 코드로 끝남 (탐색 실패 = 낭비 지표, 장애 아님)
    # refusals    = 스코프 가드가 막은 것 (안전장치가 동작한 것, 장애 아님)
    errors: int = 0
    nonzero_exits: int = 0
    refusals: int = 0
    timeouts: int = 0
    scoring_failed: str = ""    # 채점 자체가 실패하면 '탐지 0건'과 구분되어야 한다
    stopped_reason: str = ""    # done | max_steps | budget | error

    per_model: dict = field(default_factory=dict)

    @property
    def newly_solved(self) -> int:
        return max(0, self.solved_after - self.solved_before)

    @property
    def detection_rate(self) -> float:
        return self.newly_solved / self.denominator if self.denominator else 0.0

    @property
    def cost_per_solve(self) -> float:
        return self.cost_usd / self.newly_solved if self.newly_solved else float("inf")


def code_version() -> str:
    """소스 전체의 해시. 어떤 코드로 잰 숫자인지 기록해두지 않으면
    코드를 고친 뒤의 실행이 이전 실행과 한 그룹으로 평균된다."""
    h = hashlib.sha256()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True)):
        if ".venv" in path or "/runs/" in path:
            continue
        with open(path, "rb") as f:
            h.update(os.path.relpath(path, root).encode())
            h.update(f.read())
    return h.hexdigest()[:10]


class Run:
    """with Run(...) as run: 로 감싸면 벽시계·타임라인·metrics.json이 자동으로 남는다."""

    def __init__(self, system: str, profile: str, target: str = None, root: str = "runs"):
        self.m = RunMetrics(
            run_id=f"{system}-{profile}-{uuid.uuid4().hex[:6]}",
            system=system,
            profile=profile,
            target=target or config.TARGET_HOST_URL,
            code_version=code_version(),
        )
        # 도구를 병렬로 실행하면 여러 스레드가 같은 카운터를 += 한다.
        # += 는 원자적이지 않아 집계가 조용히 새므로 잠근다.
        self._lock = threading.RLock()   # record_tool → event 중첩 획득 허용
        self.dir = os.path.join(root, self.m.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self._timeline = open(os.path.join(self.dir, "timeline.jsonl"), "a")
        self._t0 = time.time()

    # ── 기록 ──────────────────────────────────────────────────────
    def event(self, kind: str, **fields):
        rec = {"t": round(time.time() - self._t0, 3), "kind": kind, **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with self._lock:            # 병렬 도구 실행 중 타임라인 줄이 섞이지 않도록
            self._timeline.write(line)
            self._timeline.flush()

    def record_llm(self, model: str, usage):
        """Anthropic usage 객체를 받아 토큰·비용을 누적."""
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0

        with self._lock:
            self.m.llm_calls += 1
            self.m.input_tokens += inp
            self.m.output_tokens += out
            self.m.cache_write_tokens += cw
            self.m.cache_read_tokens += cr

            cost = self._cost(model, inp, out, cw, cr)
            self.m.cost_usd += cost

            s = self.m.per_model.setdefault(
                model, {"calls": 0, "in": 0, "out": 0, "cache_w": 0, "cache_r": 0, "usd": 0.0}
            )
            s["calls"] += 1; s["in"] += inp; s["out"] += out
            s["cache_w"] += cw; s["cache_r"] += cr; s["usd"] += cost

        self.event("llm", model=model, input=inp, output=out,
                   cache_write=cw, cache_read=cr, usd=round(cost, 6))
        return cost

    @staticmethod
    def _cost(model, inp, out, cache_w, cache_r) -> float:
        """가격표에 없는 모델은 반드시 터뜨린다.

        조용히 0.0을 돌려주면 '무료로 돌았다'는 그럴듯한 숫자가 나오고,
        비용 비교가 핵심인 이 프로젝트에서는 그게 가장 위험한 실패다.
        """
        p = config.PRICING.get(model)
        if not p:
            raise KeyError(
                f"'{model}'의 가격이 config.PRICING에 없습니다. "
                f"비용을 0으로 기록하면 측정이 무효가 되므로 중단합니다. "
                f"등록된 모델: {sorted(config.PRICING)}"
            )
        return (
            inp * p["in"]
            + out * p["out"]
            + cache_w * p["in"] * config.CACHE_WRITE_MULT
            + cache_r * p["in"] * config.CACHE_READ_MULT
        ) / 1_000_000

    def record_tool(self, name: str, cmd: str, exit_code: int, dur: float, out_len: int):
        """실패를 세 종류로 나눠 센다.

        섞어 세면 안정성 지표가 거짓말을 한다 — 401을 받아 jq가 파싱에 실패한 것은
        '하네스가 불안정하다'가 아니라 '에이전트가 헛다리를 짚었다'이다.
        """
        with self._lock:
            self.m.tool_calls += 1
            if exit_code == 124:
                self.m.timeouts += 1
            elif exit_code == REFUSED_EXIT:
                self.m.refusals += 1
            elif exit_code == HARNESS_EXIT:
                self.m.errors += 1
            elif exit_code != 0:
                self.m.nonzero_exits += 1
            self.event("tool", name=name, cmd=cmd[:300], exit=exit_code,
                       dur=round(dur, 2), out_len=out_len)

    def over_budget(self) -> bool:
        return self.m.cost_usd >= config.RUN_BUDGET_USD

    # ── 산출물 ────────────────────────────────────────────────────
    def path(self, *parts) -> str:
        return os.path.join(self.dir, *parts)

    def write(self, filename: str, text: str):
        with open(self.path(filename), "w") as f:
            f.write(text)

    def read(self, filename: str, default: str = "") -> str:
        try:
            with open(self.path(filename)) as f:
                return f.read()
        except FileNotFoundError:
            return default

    def finish(self):
        self.m.wall_sec = round(time.time() - self._t0, 2)
        d = asdict(self.m)
        d.update({
            "newly_solved": self.m.newly_solved,
            "detection_rate": round(self.m.detection_rate, 4),
            "cost_per_solve": (None if self.m.newly_solved == 0
                               else round(self.m.cost_per_solve, 4)),
        })
        with open(self.path("metrics.json"), "w") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        self._timeline.close()
        return d

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type and not self.m.stopped_reason:
            self.m.stopped_reason = f"error:{exc_type.__name__}"
            self.m.errors += 1
        self.finish()
        return False
