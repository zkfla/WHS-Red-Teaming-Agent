from __future__ import annotations

from typing import Any

import pytest

from redteam.app.cli.conversation import ConversationAssistant
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, RunId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.state import RuntimeState

pytestmark = pytest.mark.asyncio


class _ScriptedLLM:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response

    async def complete(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "관찰된 범위에서는 한 개의 작업이 완료됐습니다."


async def test_fake_conversation_explains_plan_before_execution() -> None:
    assistant = ConversationAssistant(FakeLLMClient())

    decision = await assistant.decide(
        "localhost:5000을 조사해",
        engagement_name="local",
        scope=["localhost:5000"],
        tools=["recon/http_probe [service_discovery]"],
        prefer_execute=True,
    )

    assert decision.mode == "execute"
    assert decision.objective == "localhost:5000을 조사해"
    assert decision.assumptions
    assert any("MCP" in item for item in decision.plan)


async def test_real_conversation_can_answer_without_starting_work() -> None:
    llm = _ScriptedLLM(
        {
            "mode": "answer",
            "response": "현재는 승인 대기 중입니다.",
            "assumptions": [],
            "plan": [],
        }
    )
    assistant = ConversationAssistant(llm)

    decision = await assistant.decide(
        "지금 어떤 상태야?",
        engagement_name="local",
        scope=["localhost"],
        session_context="approval pending",
    )

    assert decision.mode == "answer"
    assert "승인" in decision.response
    assert llm.calls


async def test_execution_path_cannot_be_silently_reclassified_as_chat() -> None:
    assistant = ConversationAssistant(_ScriptedLLM({"mode": "answer", "response": "알겠습니다."}))

    decision = await assistant.decide(
        "localhost를 조사해",
        engagement_name="local",
        scope=["localhost"],
        prefer_execute=True,
    )

    assert decision.mode == "execute"
    assert decision.objective == "localhost를 조사해"


async def test_final_response_uses_only_bounded_runtime_summary() -> None:
    llm = _ScriptedLLM({})
    assistant = ConversationAssistant(llm)
    state = RuntimeState(
        run_id=RunId("run_chat"),
        engagement_id=EngagementId("eng_chat"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=1,
    )

    response = await assistant.final_response(state)

    assert "한 개의 작업" in response
    prompt = str(llm.calls[-1]["messages"])
    assert "no_actionable_plan" in prompt
    assert "actions_executed=1" in prompt
