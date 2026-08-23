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

    @property
    def agent_count(self) -> int:
        return len(self.roles)


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
    # 상한선(성능 기준점): 전부 opus + high effort
    "max": Profile("max", {
        "recon":    RoleConfig("claude-opus-5", "high", 15),
        "exploit":  RoleConfig("claude-opus-5", "high", 35),
        "reporter": RoleConfig("claude-opus-5", "high",  5),
    }),
}

DEFAULT_PROFILE = os.getenv("PROFILE", "haiku")

# ── 실행 한계 ──────────────────────────────────────────────────────
TOOL_TIMEOUT_SEC = int(os.getenv("TOOL_TIMEOUT_SEC", "120"))
TOOL_OUTPUT_MAX_CHARS = 6000
RUN_BUDGET_USD = float(os.getenv("RUN_BUDGET_USD", "2.00"))
