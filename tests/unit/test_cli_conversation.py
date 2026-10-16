from __future__ import annotations

import asyncio
from typing import Any

import pytest

from redteam.app.cli.conversation import (
    ConversationAssistant,
    ConversationDecision,
    ConversationError,
)
from redteam.core.enums import AgentPhase
from redteam.core.ids import ApprovalId, EngagementId, RunId
from redteam.orchestration.runtime_observation import RuntimeObservation
from redteam.orchestration.state import RuntimeState

pytestmark = pytest.mark.asyncio


class _ScriptedDecisionLLM:
    """Explicit stub going through the same structured call."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.structured_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    async def complete_structured(self, **kwargs: Any) -> Any:
        self.structured_calls.append(kwargs)
        index = min(len(self.structured_calls) - 1, len(self._responses) - 1)
        item = self._responses[index]
        if isinstance(item, BaseException):
            raise item
        return item

    async def complete(self, **kwargs: Any) -> str:
        self.complete_calls.append(kwargs)
        raise AssertionError("final_response must make zero LLM calls")


class _SlowLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_structured(self, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(10)
        return {"mode": "answer", "response": "late"}

    async def complete(self, **kwargs: Any) -> str:
        raise AssertionError("conversation must use structured decisions")


class _CancellingLLM:
    async def complete_structured(self, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    async def complete(self, **kwargs: Any) -> str:
        raise asyncio.CancelledError()


async def test_scripted_conversation_explains_plan_before_execution() -> None:
    llm = _ScriptedDecisionLLM(
        [
            {
                "mode": "execute",
                "response": "허용된 범위 안에서 조사를 시작하겠습니다.",
                "objective": "localhost:5000을 조사해",
                "success_criteria": ["등록된 계정의 응답 차이를 근거와 함께 요약합니다"],
                "assumptions": ["선언된 대상만 허용 범위로 간주합니다."],
                "plan": ["사용 가능한 MCP 도구를 후보로 비교합니다."],
            }
        ]
    )
    assistant = ConversationAssistant(llm)

    decision = await assistant.decide(
        "localhost:5000을 조사해",
        engagement_name="local",
        scope=["localhost:5000"],
        tools=["recon/http_probe [service_discovery]"],
        prefer_execute=True,
    )

    assert decision.mode == "execute"
    assert decision.objective == "localhost:5000을 조사해"
    assert decision.success_criteria == ["등록된 계정의 응답 차이를 근거와 함께 요약합니다"]
    assert decision.assumptions
    assert any("MCP" in item for item in decision.plan)
    assert len(llm.structured_calls) == 1
    assert llm.structured_calls[0].get("num_retries") == 0
    assert llm.complete_calls == []


async def test_execute_without_success_criteria_keeps_legacy_optional_contract() -> None:
    llm = _ScriptedDecisionLLM([
        {"mode": "execute", "response": "조사를 시작합니다.", "objective": "인증을 확인해"}
    ])
    assistant = ConversationAssistant(llm, max_attempts=1, transport_limit=0)

    decision = await assistant.decide(
        "인증을 확인해",
        engagement_name="local",
        scope=["localhost"],
    )

    assert decision.mode == "execute"
    assert decision.success_criteria == []


async def test_success_criteria_schema_publishes_list_and_item_bounds() -> None:
    schema = ConversationDecision.model_json_schema()
    criteria = schema["properties"]["success_criteria"]

    assert "success_criteria" not in schema["required"]
    assert criteria["maxItems"] == 8
    assert criteria["items"]["maxLength"] == 1000


async def test_success_criteria_are_redacted_and_duplicate_items_collapsed() -> None:
    llm = _ScriptedDecisionLLM([{
        "mode": "execute",
        "response": "등록된 대상만 확인합니다.",
        "objective": "인증 응답을 확인해",
        "success_criteria": [
            " 허용된 계정별 응답 결과를 비교합니다 ",
            "허용된 계정별 응답 결과를 비교합니다",
            "token=hidden",
        ],
    }])

    decision = await ConversationAssistant(llm).decide(
        "인증 응답을 확인해",
        engagement_name="local",
        scope=["localhost"],
    )

    assert decision.success_criteria == [
        "허용된 계정별 응답 결과를 비교합니다",
        "token=***REDACTED***",
    ]


async def test_real_conversation_can_answer_without_starting_work() -> None:
    llm = _ScriptedDecisionLLM(
        [
            {
                "mode": "answer",
                "response": "현재는 승인 대기 중입니다.",
                "assumptions": [],
                "plan": [],
            }
        ]
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
    assert len(llm.structured_calls) == 1
    assert llm.structured_calls[0].get("num_retries") == 0
    assert llm.complete_calls == []


async def test_prefer_execute_does_not_force_answer_into_execute() -> None:
    llm = _ScriptedDecisionLLM([{"mode": "answer", "response": "알겠습니다."}])
    assistant = ConversationAssistant(llm)

    decision = await assistant.decide(
        "localhost를 조사해",
        engagement_name="local",
        scope=["localhost"],
        prefer_execute=True,
    )

    assert decision.mode == "answer"
    assert decision.objective is None
    assert len(llm.structured_calls) == 1


async def test_final_response_uses_only_bounded_runtime_summary() -> None:
    llm = _ScriptedDecisionLLM(
        [{"mode": "answer", "response": "현재 상태를 확인했습니다."}]
    )
    assistant = ConversationAssistant(llm)
    await assistant.decide(
        "지금 어떤 상태야?",
        engagement_name="local",
        scope=["localhost"],
    )
    state = RuntimeState(
        run_id=RunId("run_chat"),
        engagement_id=EngagementId("eng_chat"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=1,
    )

    response = await assistant.final_response(state)

    assert "no_actionable_plan" in response
    assert "1" in response
    assert llm.complete_calls == []
    assert len(llm.structured_calls) == 1


async def test_guidance_mode_is_preserved_without_tool_use() -> None:
    llm = _ScriptedDecisionLLM(
        [
            {
                "mode": "guidance",
                "response": "요청을 다음 판단에 반영하겠습니다.",
                "guidance": "다른 방법으로 시도해 주세요.",
            }
        ]
    )
    assistant = ConversationAssistant(llm)

    decision = await assistant.decide(
        "다른 방법으로 시도해 주세요.",
        engagement_name="local",
        scope=["localhost"],
        run_active=True,
    )

    assert decision.mode == "guidance"
    assert decision.guidance == "다른 방법으로 시도해 주세요."
    assert llm.complete_calls == []


async def test_clarify_mode_is_preserved() -> None:
    llm = _ScriptedDecisionLLM(
        [{"mode": "clarify", "response": "대상을 알려주세요."}]
    )
    assistant = ConversationAssistant(llm)

    decision = await assistant.decide(
        "조사해",
        engagement_name="local",
        scope=["localhost"],
    )

    assert decision.mode == "clarify"


async def test_transport_error_raises_without_execution() -> None:
    llm = _ScriptedDecisionLLM([Exception("boom")])
    assistant = ConversationAssistant(llm)

    with pytest.raises(ConversationError):
        await assistant.decide(
            "지금 어떤 상태야?",
            engagement_name="local",
            scope=["localhost"],
        )

    assert len(llm.structured_calls) == 6
    assert llm.complete_calls == []


async def test_repeated_format_errors_call_exactly_six_times() -> None:
    llm = _ScriptedDecisionLLM([{}])
    assistant = ConversationAssistant(llm)

    with pytest.raises(ConversationError):
        await assistant.decide(
            "지금 어떤 상태야?",
            engagement_name="local",
            scope=["localhost"],
        )

    assert len(llm.structured_calls) == 6
    for call in llm.structured_calls:
        assert call.get("num_retries") == 0
    assert llm.complete_calls == []


async def test_timeout_raises_without_execution() -> None:
    assistant = ConversationAssistant(_SlowLLM(), timeout_seconds=0.05)

    with pytest.raises(ConversationError, match="timed out"):
        await assistant.decide(
            "지금 어떤 상태야?",
            engagement_name="local",
            scope=["localhost"],
        )


async def test_cancellation_propagates() -> None:
    assistant = ConversationAssistant(_CancellingLLM())

    with pytest.raises(asyncio.CancelledError):
        await assistant.decide(
            "지금 어떤 상태야?",
            engagement_name="local",
            scope=["localhost"],
        )


async def test_error_message_hides_raw_secret() -> None:
    secret = "sk-live-should-never-appear"

    class _LeakyProviderError(Exception):
        pass

    llm = _ScriptedDecisionLLM([_LeakyProviderError(f"api_key={secret}")])
    assistant = ConversationAssistant(llm)

    with pytest.raises(ConversationError) as exc_info:
        await assistant.decide(
            "지금 어떤 상태야?",
            engagement_name="local",
            scope=["localhost"],
        )

    assert secret not in str(exc_info.value)
    assert "api_key=" not in str(exc_info.value)
    assert "conversation decision failed" in str(exc_info.value)


async def test_final_response_korean_uses_actual_state() -> None:
    llm = _ScriptedDecisionLLM(
        [{"mode": "answer", "response": "현재 상태를 확인했습니다."}]
    )
    assistant = ConversationAssistant(llm)
    await assistant.decide(
        "지금 어떤 상태야?",
        engagement_name="local",
        scope=["localhost"],
    )
    state = RuntimeState(
        run_id=RunId("run_chat"),
        engagement_id=EngagementId("eng_chat"),
        phase=AgentPhase.FINISHED,
        termination_reason="goal_achieved",
        actions_executed=2,
    )

    response = await assistant.final_response(state)

    assert "goal_achieved" in response
    assert "2" in response
    assert llm.complete_calls == []


async def test_final_response_english_when_history_is_english() -> None:
    llm = _ScriptedDecisionLLM(
        [{"mode": "answer", "response": "Status checked."}]
    )
    assistant = ConversationAssistant(llm)
    await assistant.decide(
        "What is the current status?",
        engagement_name="local",
        scope=["localhost"],
    )
    state = RuntimeState(
        run_id=RunId("run_chat"),
        engagement_id=EngagementId("eng_chat"),
        phase=AgentPhase.FINISHED,
        termination_reason="goal_achieved",
        actions_executed=2,
    )

    response = await assistant.final_response(state)

    assert "Run ended as goal_achieved" in response
    assert llm.complete_calls == []


async def test_final_response_masks_secrets_and_bounds_observations() -> None:
    llm = _ScriptedDecisionLLM(
        [{"mode": "answer", "response": "Status checked."}]
    )
    assistant = ConversationAssistant(llm)
    await assistant.decide(
        "What is the current status?",
        engagement_name="local",
        scope=["localhost"],
    )
    observations = [
        RuntimeObservation(
            kind="tool",
            summary=f"obs-{index} note",
            outcome="ok",
            step=index,
        )
        for index in range(7)
    ]
    observations[6] = RuntimeObservation(
        kind="tool",
        summary="probe result token=sk-live-should-never-appear",
        outcome="ok",
        step=6,
    )
    state = RuntimeState(
        run_id=RunId("run_chat"),
        engagement_id=EngagementId("eng_chat"),
        phase=AgentPhase.FINISHED,
        termination_reason="goal_achieved",
        actions_executed=7,
        recent_observations=observations,
        pending_approval_id=ApprovalId("appr_1"),
    )

    response = await assistant.final_response(state)

    assert "obs-0 note" not in response
    assert "obs-1 note" not in response
    assert "obs-6" not in response or "REDACTED" in response
    assert "sk-live-should-never-appear" not in response
    assert "REDACTED" in response
    assert "appr_1" in response
    assert "Approval pending" in response
    assert llm.complete_calls == []
