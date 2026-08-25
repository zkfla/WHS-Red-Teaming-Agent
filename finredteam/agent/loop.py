"""메인 진입점 — `agent/react.py` 의 ReAct 루프로 위임한다.

과거엔 결정적 배치 파이프라인(agent/strategy.py 의 STRATEGIES 로 후보를 미리
다 나열하고 일괄 실행)이었으나, ReAct 로 전환하면서 "다음에 뭘 할지"를 LLM 이
매 스텝 스스로 정하는 구조로 바뀌었다(Thought → Action → Observation 반복).
실행/판정은 여전히 100% 코드다 — 자세한 루프는 `agent/react.py` 참고.
"""
from __future__ import annotations

from finredteam.agent.react import AgentContext, Step, bootstrap_principals, run

__all__ = ["AgentContext", "Step", "bootstrap_principals", "run"]
