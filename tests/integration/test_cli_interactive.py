"""``redteam`` with no subcommand (sections 9-12, 26): in-process so the
fixed configuration prompts and terminal input are exercised without a real network/tty.

Real ``run_command.build``/``execute`` still runs for real underneath
(same ``ExampleMCPAdapter``/``FakeLLMClient`` path
``tests/integration/test_cli_smoke.py`` already exercises) -- only the
Intake Assistant's own LLM client and the terminal are test doubles.
"""

from __future__ import annotations

import contextlib
from typing import cast

import pytest
from rich.console import Console

from redteam.app.cli.commands import interactive


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.setenv("REDTEAM_RUNTIME_BACKEND", "legacy")

pytestmark = pytest.mark.asyncio


class _FakeConsole:
    """Minimal duck-typed stand-in for ``rich.console.Console`` -- only
    the surface ``interactive``/``renderer`` actually call."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.transcript: list[str] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        for arg in args:
            self.transcript.append(str(arg))

    def input(self, prompt: str = "") -> str:
        self.transcript.append(str(prompt))
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)

    def status(self, *_args: object, **_kwargs: object):  # noqa: ANN201
        return contextlib.nullcontext()


class _ScriptedIntakeLLM:
    """Records every call (for the secret-isolation assertion) and
    returns one scripted ``IntakeTurn``-shaped dict per call."""

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    async def complete(self, *, messages, system=None, **kwargs):  # noqa: ANN001, ANN003
        return "ack"

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"messages": messages, "system": system})
        return self._turns.pop(0)




_COMPLETE_TURN = {
    "draft": {
        "name": "VulnBank Local",
        "allowed_hosts": ["127.0.0.1:5000"],
        "focus_areas": ["authorization", "financial_logic"],
        "availability_impact_allowed": False,
        "notes": ["no data deletion"],
        "max_actions": 100,
    },
    "acknowledgement": "확인했습니다.",
    "follow_up_questions": [],
    "ready": True,
}




async def test_existing_config_accepts_one_line_mission_objective() -> None:
    from redteam.core.config import EngagementConfig

    config = EngagementConfig.model_validate(
        {
            "name": "VulnBank",
            "scope": {"allowed_hosts": ["example.local"]},
            "execution_budget": {"max_actions": 5},
        }
    )
    console = _FakeConsole(["VulnBank 웹 레드티밍 해줘. 인증/인가 중심으로."])

    selected = await interactive._existing_config_menu(
        cast(Console, console), config, _ScriptedIntakeLLM([])
    )

    assert selected == (config, "VulnBank 웹 레드티밍 해줘. 인증/인가 중심으로.")










async def test_fixed_edit_reprompts_invalid_count_without_model() -> None:
    # New creation never asks for a count, so the invalid-count reprompt
    # lives on the edit path (form.edit), where item 7 is still the count.
    from redteam.intake.models import EngagementDraft
    from redteam.intake.serializer import draft_to_engagement_config

    original = draft_to_engagement_config(
        EngagementDraft(
            name="127.0.0.1:5000",
            allowed_hosts=["127.0.0.1:5000"],
            max_actions=50,
            description="http://127.0.0.1:5000 ...",
        )
    ).engagement_config
    llm = _ScriptedIntakeLLM([])
    console = _FakeConsole(["7", "많이", "-3", "30", ""])
    edited = await interactive._edit_engagement(cast(Console, console), llm, original)
    assert edited is not None
    assert edited.execution_budget.max_actions == 30
    assert llm.calls == []


async def test_fixed_edit_preserves_unrelated_policy_and_explicitly_clears_optional_values():
    from redteam.intake.models import EngagementDraft
    from redteam.intake.serializer import draft_to_engagement_config

    original = draft_to_engagement_config(
        EngagementDraft(
            name="bank",
            allowed_hosts=["localhost:8081"],
            max_actions=30,
            description="inspect",
            allowed_capabilities=["recon"],
        )
    ).engagement_config
    original = original.model_copy(
        update={
            "metadata": {**original.metadata, "custom": "preserve"},
            "rate_limit_policy": original.rate_limit_policy.model_copy(
                update={"max_actions_per_minute": 7}
            ),
            "scope": original.scope.model_copy(update={"denied_hosts": ["excluded.local"]}),
            "rules_of_engagement": [
                original.rules_of_engagement[0].model_copy(
                    update={"allowed_operations": ["service_discovery"], "max_parallel_actions": 2}
                )
            ],
        }
    )
    llm = _ScriptedIntakeLLM([])
    console = _FakeConsole(["5", "-", "7", "50", ""])
    edited = await interactive._edit_engagement(cast(Console, console), llm, original)
    assert edited is not None
    assert edited.description == ""
    assert edited.execution_budget.max_actions == 50
    assert edited.scope == original.scope
    assert edited.rate_limit_policy == original.rate_limit_policy
    assert edited.approval_policy == original.approval_policy
    assert edited.metadata == original.metadata
    assert edited.rules_of_engagement == original.rules_of_engagement
    assert llm.calls == []














async def test_fake_provider_is_rejected_by_production_builder() -> None:
    import pytest

    from redteam.app.bootstrap import build_llm_client
    from redteam.core.config import LLMConfig

    with pytest.raises(ValueError, match="real LLM provider"):
        build_llm_client(LLMConfig(provider="fake"))
