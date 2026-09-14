from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from spec.schema.attack_graph import FindingRecord, GraphView, Transition
from spec.schema.tool_manifest import ProbeResult, ToolManifest

PORTS_VERSION = 1

# ══════════════════════════════════════════════════════════════════════
#  팀 경계에서 오가는 타입. policy.PolicyPort 와 같은 역할을 나머지 셋에 한다.
#
#  ★ 이 파일이 없으면 각 팀이 자기 시그니처를 스스로 정한다. 그러면 합칠 때
#    "①이 돌려주는 dict 의 키 이름" 같은 걸 회의로 맞춰야 하고, 맞춘 뒤에도
#    한쪽이 바꾸면 조용히 어긋난다. 타입으로 박아두면 안 맞을 때 바로 터진다.
# ══════════════════════════════════════════════════════════════════════


# ── ① Agent → ② Orchestration ────────────────────────────────────────
#
# 모델이 고른 것 한 건. ① 이 돌려주는 유일한 타입이다.
class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    params: dict = {}
    step: int = 0
    # ★ 멀티에이전트(Recon / web+api / Mobile ...)에서 누가 고른 건지.
    #   Transition.agent_id 로 그대로 넘어가 기록에 남는다.
    agent_id: str = ""

    # ★ 모델이 만든 문장이다. 리포트의 **서술**에는 써도 되지만
    #   FindingRecord.reason 에는 절대 들어가면 안 된다 —
    #   판정 근거가 모델 생성물이면 재현도 검증도 안 된다.
    rationale: str = ""


@runtime_checkable
class AgentPort(Protocol):
    # observation = GraphView.observe() 결과
    # tools       = frontier 를 반영한 MCP tool 정의 목록 (mcp_tool_list)
    # None 을 돌려주면 "더 할 게 없다" — ② 가 루프를 끝낸다.
    def choose(
        self, observation: dict, tools: list[dict], *, step: int
    ) -> ToolCall | None: ...


# ── ② Orchestration → ③ Capability ───────────────────────────────────
#
# ★ bindings 가 여기 들어가는 게 핵심이다. preflight() 가 모델의 params 로
#   확정한 바인딩을 ③ 에게 넘기고, **같은 값**을 ingest() 에도 넘긴다.
#   이 값이 갈라지면 "모델이 고른 대상" 과 "스코프 대조 기준" 이 달라져서
#   이탈 탐지가 조용히 엉뚱한 기준으로 돈다.
class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str
    step: int = 0
    # 원본 값이다(민감값 포함). 기록에 남는 건 manifest.redact() 를 거친 사본이고,
    # 실행에는 이 원본을 쓴다.
    params: dict = {}
    bindings: dict[str, list[str]] = {}


@runtime_checkable
class Executor(Protocol):
    def supports(self, tool_id: str) -> bool: ...

    # 예외를 던지지 말고 ProbeResult.error / failure_code 로 돌려줄 것.
    # 실패도 기록되어야 하는 관측이다.
    def execute(
        self, manifest: ToolManifest, request: ExecutionRequest
    ) -> ProbeResult: ...


# ── ④ Control Plane — 판정 ───────────────────────────────────────────
#
# ★ ingest() 는 어느 오라클을 돌릴지 고르지 않는다. 그 선택권이 생기는 순간
#   "이 tool 은 이 불변식만" 이라는 좁힘이 부활한다.
#   구현체가 오라클 registry 전체를 매번 돌려야 한다.
#
# ★ GraphView 를 받는 이유: 판정에 그래프 맥락이 필요하다("이 계좌가 우리 것인가").
#   하지만 **읽기 전용**이다 — 오라클이 그래프를 쓰면 판정과 상태 갱신이 섞인다.
@runtime_checkable
class OraclePort(Protocol):
    def evaluate(
        self, result: ProbeResult, view: GraphView, transition: Transition
    ) -> list[FindingRecord]: ...
