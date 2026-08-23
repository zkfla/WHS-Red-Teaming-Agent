"""실험 설정 한 곳. 경량화 축(모델 티어/effort/스텝 수)을 여기서만 바꾼다."""
import os
from dataclasses import dataclass, field

# ── 격리 네트워크 ───────────────────────────────────────────────────
# whs-bench 는 --internal 네트워크라 외부 인터넷 경로가 없다.
# 스코프 강제의 '진짜' 경계는 이것이고, shell.py의 정규식은 보조 수단이다.
BENCH_NETWORK = os.getenv("BENCH_NETWORK", "whs-bench")
SANDBOX_CONTAINER = os.getenv("SANDBOX_CONTAINER", "whs-sandbox")
SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "ghcr.io/purpleailab/decepticon-sandbox:1.1.40")

# ── 타깃 ────────────────────────────────────────────────────────────
JUICESHOP_CONTAINER = os.getenv("JUICESHOP_CONTAINER", "juiceshop-bench")
JUICESHOP_IMAGE = os.getenv("JUICESHOP_IMAGE", "bkimminich/juice-shop")
JUICESHOP_PORT = int(os.getenv("JUICESHOP_PORT", "3000"))     # 컨테이너 내부 포트

# 호스트 바인드는 127.0.0.1 + 비표준 포트.
# 브라우저에 남은 localhost:3000 탭이 continueCode로 오염시키는 걸 원천 차단한다.
HOST_BIND = os.getenv("HOST_BIND", "127.0.0.1")
HOST_PORT = int(os.getenv("HOST_PORT", "3100"))

TARGET_HOST_URL = os.getenv("TARGET_HOST_URL", f"http://{HOST_BIND}:{HOST_PORT}")
TARGET_SANDBOX_URL = os.getenv(
    "TARGET_SANDBOX_URL", f"http://{JUICESHOP_CONTAINER}:{JUICESHOP_PORT}")

# 보조 스코프 가드 허용 호스트 (진짜 경계는 위의 --internal 네트워크)
ALLOWED_HOSTS = [JUICESHOP_CONTAINER, "localhost", "127.0.0.1"]

# ── 모델 가격표 (USD / 1M tokens, 2026-06-24 기준) ──────────────────
# 캐시 배수: write=1.25x(5m TTL), read=0.1x
PRICING = {
    "claude-opus-5":      {"in": 5.00, "out": 25.00},
    "claude-sonnet-5":    {"in": 2.00, "out": 10.00},   # 2026-08-31까지 인트로 (정가 3/15)
    "claude-haiku-4-5":   {"in": 1.00, "out": 5.00},
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


@dataclass
class RoleConfig:
    """역할별 모델 티어 + 추론 강도 + 스텝 예산. 경량화 실험의 주 조절 손잡이."""
    model: str
    effort: str      # low | medium | high | xhigh | max
    max_steps: int   # ReAct 루프 최대 반복 (폭주 방지 + 비용 상한)


@dataclass
class Profile:
    name: str
    roles: dict = field(default_factory=dict)
    fan_out: list = None        # exploit을 이 전문가들로 동시 실행 (None이면 순차 1개)

    @property
    def agent_count(self) -> int:
        # 팬아웃이면 exploit 하나가 N명이 된다 — 구조 지표가 주 지표라 정확해야 한다
        n = len(self.roles)
        return n + len(self.fan_out) - 1 if self.fan_out else n


# 경량 코어가 띄우는 컨테이너: 타깃 + 샌드박스 (에이전트는 호스트 프로세스)
LITE_CONTAINERS = 2

# 프로파일 = 실험 조건. 같은 코드/같은 타깃에 프로파일만 바꿔 돌려 비교한다.
PROFILES = {
    "lite": Profile("lite", {
        "recon":    RoleConfig("claude-haiku-4-5", "low",    12),
        "exploit":  RoleConfig("claude-sonnet-5",  "medium", 25),
        "reporter": RoleConfig("claude-haiku-4-5", "low",     4),
    }),
    "balanced": Profile("balanced", {
        "recon":    RoleConfig("claude-haiku-4-5", "low",    12),
        "exploit":  RoleConfig("claude-opus-5",    "medium", 25),
        "reporter": RoleConfig("claude-haiku-4-5", "low",     4),
    }),
    # haiku 전용: 모델 접근이 haiku로 제한된 자격증명에서도 파이프라인 전체를 돌린다.
    "haiku": Profile("haiku", {
        "recon":    RoleConfig("claude-haiku-4-5", "low", 10),
        "exploit":  RoleConfig("claude-haiku-4-5", "low", 20),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }),
    # 배칭 검증용 — 턴당 도구 수가 2.5→3.3으로 늘었으니 같은 도구 커버리지를
    # 더 적은 턴으로 살 수 있어야 한다. 스텝을 30% 깎아 그 가설을 시험한다.
    # (컨텍스트는 턴마다 다시 읽히므로 턴 수가 비용을 지배한다)
    "haiku-lean": Profile("haiku-lean", {
        "recon":    RoleConfig("claude-haiku-4-5", "low",  7),
        "exploit":  RoleConfig("claude-haiku-4-5", "low", 14),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }),
    # 팬아웃 — exploit을 전문가 3명으로 쪼개 동시 실행.
    # 스텝 총량은 순차 haiku(20)와 맞춘다: 7×3=21. 같은 예산을 병렬로 쓰는 셈이라
    # 벽시계와 컨텍스트 크기의 효과만 분리해서 볼 수 있다.
    "haiku-fan": Profile("haiku-fan", {
        "recon":    RoleConfig("claude-haiku-4-5", "low", 10),
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  7),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn"]),
    # 에이전트 수 곡선 — 총 스텝을 ~20으로 맞추고 인원만 바꾼다.
    # "16→3이 맞나"는 D안의 핵심 질문이고, 점이 두 개뿐이면 곡선이 안 그려진다.
    "haiku-fan2": Profile("haiku-fan2", {
        "recon":    RoleConfig("claude-haiku-4-5", "low", 10),
        "exploit":  RoleConfig("claude-haiku-4-5", "low", 10),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access"]),
    "haiku-fan5": Profile("haiku-fan5", {
        "recon":    RoleConfig("claude-haiku-4-5", "low", 10),
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  4),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # 대조군 — fan5와 인원·스텝이 같고 담당만 없앴다.
    # fan5와 성적이 비슷하면 이득의 원인은 전문화가 아니라 병렬 독립 컨텍스트다.
    "haiku-fan5g": Profile("haiku-fan5g", {
        "recon":    RoleConfig("claude-haiku-4-5", "low", 10),
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  4),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["gen1", "gen2", "gen3", "gen4", "gen5"]),
    # recon 제거 — 공용 정찰 10턴을 없애고 전문가에게 되돌려준다.
    # 총 exploit 스텝 6×5=30 ≈ (recon 10 + fan5의 4×5=20). 예산은 같고 단계만 줄었다.
    "haiku-norecon": Profile("haiku-norecon", {
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  6),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # 예산 곡선 — 인원(전문가5)·단계(2) 고정, 1인당 스텝만 바꾼다.
    # 탐지가 계속 오르면 예산 제약, 평평해지면 능력 제약이다.
    # 이 구분이 "더 싸게" 주장의 한계선을 정한다.
    "haiku-nr4": Profile("haiku-nr4", {
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  4),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # ★ 권장 기본값. n=49 실행의 회귀 잔차에서 최고(+0.82).
    # 전문가 5명 × 9스텝이 최적점이다 — 12스텝으로 늘리면 +0.36으로 떨어진다.
    "haiku-nr9": Profile("haiku-nr9", {
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  9),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # 최소 recon — recon을 완전히 없앴더니 전문가 5명이 각자 정찰하며
    # 도구 호출이 54→71로 늘었다. 공용 정찰을 3턴만 남겨 그 중복만 걷어낸다.
    "haiku-minirecon": Profile("haiku-minirecon", {
        "recon":    RoleConfig("claude-haiku-4-5", "low",  3),
        "exploit":  RoleConfig("claude-haiku-4-5", "low",  5),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # 상관분석이 가리킨 방향: 탐지는 LLM 턴을 따라가고(r=+0.46) 컨텍스트 크기는
    # 거의 무관하다(도구 r=+0.16). 턴을 늘리고 컨텍스트를 줄이는 조합을 잰다.
    "haiku-nr12": Profile("haiku-nr12", {
        "exploit":  RoleConfig("claude-haiku-4-5", "low", 12),
        "reporter": RoleConfig("claude-haiku-4-5", "low",  4),
    }, fan_out=["injection", "access", "authn", "exposure", "logic"]),
    # 상한선(성능 기준점): 전부 opus + high effort
    "max": Profile("max", {
        "recon":    RoleConfig("claude-opus-5", "high", 15),
        "exploit":  RoleConfig("claude-opus-5", "high", 35),
        "reporter": RoleConfig("claude-opus-5", "high",  5),
    }),
}

DEFAULT_PROFILE = os.getenv("PROFILE", "haiku-nr9")

# ── 실행 한계 ──────────────────────────────────────────────────────
TOOL_TIMEOUT_SEC = int(os.getenv("TOOL_TIMEOUT_SEC", "120"))
# 도구 출력 상한. 컨텍스트 비용을 직접 지배한다 — 잘린 출력은 대화에 남아
# 이후 모든 턴에서 다시 읽히므로, 상한 1자당 비용이 턴 수만큼 곱해진다.
TOOL_OUTPUT_MAX_CHARS = int(os.getenv("TOOL_OUTPUT_MAX_CHARS", "6000"))
RUN_BUDGET_USD = float(os.getenv("RUN_BUDGET_USD", "2.00"))
