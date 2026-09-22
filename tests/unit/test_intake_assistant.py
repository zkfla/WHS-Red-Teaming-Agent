"""``IntakeAssistant`` against ``FakeLLMClient`` -- no network, deterministic."""

from __future__ import annotations

import pytest

from redteam.intake.assistant import IntakeAssistant, IntakeAssistantError, IntakeSession
from redteam.intake.models import EngagementDraft
from redteam.llm.fake import FakeLLMClient

pytestmark = pytest.mark.asyncio


async def test_step_returns_structured_turn_and_grows_history() -> None:
    llm = FakeLLMClient(
        canned_structured={
            "draft": {"name": "VulnBank Local", "allowed_hosts": ["127.0.0.1:5000"]},
            "acknowledgement": "확인했습니다.",
            "follow_up_questions": ["최대 action 수는?"],
            "ready": False,
        }
    )
    assistant = IntakeAssistant(llm)
    session = IntakeSession()

    turn = await assistant.step(session, "127.0.0.1:5000 test the VulnBank app")

    assert turn.draft.name == "VulnBank Local"
    assert turn.draft.allowed_hosts == ["127.0.0.1:5000"]
    assert turn.ready is False
    assert session.history  # the turn was recorded for the next call
    assert session.history[0]["role"] == "user"
    assert session.history[-1]["role"] == "assistant"


async def test_step_never_sends_secret_shaped_data_even_if_user_pastes_one() -> None:
    llm = FakeLLMClient(canned_structured={"draft": {}, "ready": False})
    assistant = IntakeAssistant(llm)
    session = IntakeSession()

    # A user might paste something secret-looking by mistake; the draft
    # model has no field to carry it forward, but the raw turn text
    # necessarily passes through messages -- this test documents that
    # the *draft* (what gets serialized/persisted) never carries it.
    turn = await assistant.step(session, "my api key is sk-live-abc123, test 127.0.0.1")

    assert "sk-live-abc123" not in turn.draft.model_dump_json()


class _BrokenLLMClient:
    async def complete(self, *, messages, system=None, **kwargs):  # noqa: ANN001, ANN003
        return "ack"

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("provider exploded")


async def test_step_wraps_provider_failure_without_leaking_exception_text() -> None:
    assistant = IntakeAssistant(_BrokenLLMClient())
    session = IntakeSession()

    with pytest.raises(IntakeAssistantError) as excinfo:
        await assistant.step(session, "test 127.0.0.1")

    assert "provider exploded" not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)


async def test_editing_session_gets_the_edit_context_appended() -> None:
    captured_system: list[str | None] = []

    class _RecordingLLMClient:
        async def complete(self, *, messages, system=None, **kwargs):  # noqa: ANN001, ANN003
            return "ack"

        async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):  # noqa: ANN001, ANN003
            captured_system.append(system)
            return {"draft": {}, "ready": False}

    assistant = IntakeAssistant(_RecordingLLMClient())
    session = IntakeSession(draft=EngagementDraft(name="X"), editing=True)

    await assistant.step(session, "max actions를 200으로")

    assert captured_system[0] is not None
    assert "editing an EXISTING" in captured_system[0]
