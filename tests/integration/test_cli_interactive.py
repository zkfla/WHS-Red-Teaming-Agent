"""``redteam`` with no subcommand (sections 9-12, 26): in-process so the
fixed configuration prompts and terminal input are exercised without a real network/tty.

Real ``run_command.build``/``execute`` still runs for real underneath
(same ``ExampleMCPAdapter``/``FakeLLMClient`` path
``tests/integration/test_cli_smoke.py`` already exercises) -- only the
Intake Assistant's own LLM client and the terminal are test doubles.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import cast

import pytest
import yaml
from rich.console import Console

from redteam.app.cli import _shared
from redteam.app.cli.commands import interactive
from redteam.core.config import load_engagement_config

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


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answers: list[str],
    llm,  # noqa: ANN001
) -> _FakeConsole:
    monkeypatch.chdir(tmp_path)
    # REDTEAM_LLM_PROVIDER already set (section 4/20's canonical env source)
    # means `_ensure_runtime_config` skips its one-time prompt entirely --
    # no application-config file involved.
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("REDTEAM_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    console = _FakeConsole(answers)
    monkeypatch.setattr(interactive, "Console", lambda: console)
    from tests.integration.test_cli_session import _ConversationLLM

    monkeypatch.setattr(interactive, "build_llm_client", lambda config: _ConversationLLM())
    # Setup/UI tests must not call a real provider. Exercise the canonical
    # application runtime with the same explicit hold model as session tests.
    from redteam.app.cli.commands import run as run_command
    from tests.integration.test_cli_session import _HoldLLM

    original_build = run_command.build_application

    class NoToolsHoldLLM(_HoldLLM):
        def _draft(self, node_id):
            draft = super()._draft(node_id)
            # Intake/UI fixtures deliberately have no executable tools. Holding
            # is valid, but the model must not invent a required execution step.
            draft["steps"][0]["required"] = False
            return draft

    async def build_with_model(config, **kwargs):
        kwargs.setdefault("llm", NoToolsHoldLLM())
        return await original_build(config, **kwargs)

    monkeypatch.setattr(run_command, "build_application", build_with_model)
    return console


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


async def test_no_config_fixed_setup_runs_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ARTEX-style minimal setup: description + objective only. New creation
    # skips the existing-config menu and the preflight start question and
    # proceeds automatically when preflight is clean. The trailing "n"
    # answers the one-time MCP opt-in prompt (no servers configured).
    llm = _ScriptedIntakeLLM([])
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "http://127.0.0.1:5000 ...",
            "inspect",
            "n",
        ],
        llm=llm,
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    saved = load_engagement_config(tmp_path / "engagement.yaml")
    # Name is derived from the first host:port found in the description.
    assert saved.name == "127.0.0.1:5000"
    assert saved.scope.allowed_hosts == ["127.0.0.1:5000"]
    assert saved.scope.allowed_domains == []
    assert saved.scope.allowed_networks == []
    assert saved.description == "http://127.0.0.1:5000 ..."
    assert saved.metadata["objective"] == "inspect"
    # Empty allow list keeps the existing ROE meaning (no restriction).
    assert saved.rules_of_engagement[0].allowed_capabilities == []
    from redteam.core.config import OrchestrationConfig

    assert saved.execution_budget.max_actions == OrchestrationConfig.from_environment().max_steps
    assert llm.calls == []
    assert any("RunId:" in line for line in console.transcript)


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


async def test_existing_config_quick_start_needs_only_objective_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redteam.core.config import EngagementConfig

    config = EngagementConfig.model_validate(
        {
            "name": "VulnBank",
            "scope": {"allowed_hosts": ["example.local"]},
            "execution_budget": {"max_actions": 5},
        }
    )
    (tmp_path / "engagement.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
    )
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=["VulnBank 웹 레드티밍 해줘. 인증/인가 중심으로.", "n"],
        llm=_ScriptedIntakeLLM([]),
    )

    assert await interactive.run(_args()) == _shared.EXIT_OK
    assert any("RunId:" in line for line in console.transcript)


async def test_existing_config_enter_reuses_stored_objective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Enter on an existing setting uses the stored goal instead of asking
    # for a new one. No objective typing, no preflight start question.
    from redteam.core.config import EngagementConfig

    config = EngagementConfig.model_validate(
        {
            "name": "127.0.0.1:5000",
            "description": "http://127.0.0.1:5000 ...",
            "scope": {"allowed_hosts": ["127.0.0.1:5000"]},
            "execution_budget": {"max_actions": 50},
            "metadata": {"objective": "inspect"},
        }
    )
    (tmp_path / "engagement.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8"
    )
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=["", "n"],
        llm=_ScriptedIntakeLLM([]),
    )

    assert await interactive.run(_args()) == _shared.EXIT_OK
    assert any("RunId:" in line for line in console.transcript)


async def test_existing_menu_new_goes_directly_to_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Choosing New from the existing-setting menu goes straight to
    # preflight: no return to the menu and no extra start question.
    from redteam.core.config import EngagementConfig

    old = EngagementConfig.model_validate(
        {
            "name": "Old",
            "scope": {"allowed_hosts": ["old.local"]},
            "execution_budget": {"max_actions": 5},
        }
    )
    (tmp_path / "engagement.yaml").write_text(
        yaml.safe_dump(old.model_dump(mode="json")), encoding="utf-8"
    )
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=["n", "http://127.0.0.1:5000 ...", "inspect", "n"],
        llm=_ScriptedIntakeLLM([]),
    )

    assert await interactive.run(_args()) == _shared.EXIT_OK
    saved = load_engagement_config(tmp_path / "engagement.yaml")
    assert saved.name == "127.0.0.1:5000"
    assert saved.metadata["objective"] == "inspect"
    assert any("RunId:" in line for line in console.transcript)


async def test_fixed_setup_derives_name_and_defaults_without_model() -> None:
    llm = _ScriptedIntakeLLM([])
    console = _FakeConsole(
        [
            "http://127.0.0.1:5000 권한 검증",
            "inspect",
        ]
    )
    config = await interactive._new_engagement(cast(Console, console), llm)
    assert config is not None
    # Name is automatic from the first host:port in the description.
    assert config.name == "127.0.0.1:5000"
    assert config.description == "http://127.0.0.1:5000 권한 검증"
    assert config.scope.allowed_hosts == ["127.0.0.1:5000"]
    assert config.metadata["objective"] == "inspect"
    # Empty allow list keeps the existing ROE meaning (no restriction).
    assert config.rules_of_engagement[0].allowed_capabilities == []
    from redteam.core.config import OrchestrationConfig

    assert config.execution_budget.max_actions == OrchestrationConfig.from_environment().max_steps
    assert llm.calls == []


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


async def test_intake_llm_never_receives_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([])
    _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "http://127.0.0.1:5000 ...",
            "inspect",
            "n",  # one-time MCP auto-setup prompt -> decline
        ],
        llm=llm,
    )

    await interactive.run(_args())

    assert llm.calls == []


async def test_q_cancels_without_writing_engagement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([])
    _patch(monkeypatch, tmp_path, answers=["q"], llm=llm)

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert not (tmp_path / "engagement.yaml").exists()


async def test_fixed_setup_asks_hosts_only_when_description_has_no_address() -> None:
    llm = _ScriptedIntakeLLM([])
    console = _FakeConsole(["내부망 점검", "inspect", "127.0.0.1:5000"])
    config = await interactive._new_engagement(cast(Console, console), llm)
    assert config is not None
    assert config.scope.allowed_hosts == ["127.0.0.1:5000"]
    assert config.name == "127.0.0.1:5000"
    assert config.description == "내부망 점검"
    assert config.metadata["objective"] == "inspect"
    assert llm.calls == []


async def test_eof_exits_with_config_error_and_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([])
    console = _patch(monkeypatch, tmp_path, answers=[], llm=llm)

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_CONFIG_ERROR
    assert any("non-interactive" in line for line in console.transcript)


async def test_edit_patches_only_the_requested_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redteam.intake.models import EngagementDraft
    from redteam.intake.serializer import draft_to_engagement_config

    original = draft_to_engagement_config(
        EngagementDraft(name="VulnBank Local", allowed_hosts=["127.0.0.1:5000"], max_actions=100)
    ).engagement_config
    (tmp_path / "engagement.yaml").write_text(
        yaml.safe_dump(original.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )

    edit_turn = {
        "draft": {"max_actions": 200},
        "acknowledgement": "반영했습니다.",
        "follow_up_questions": [],
        "ready": True,
    }
    llm = _ScriptedIntakeLLM([edit_turn])
    _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "e",  # existing-config menu -> Edit
            "7",
            "200",
            "",  # choose max_actions, enter value, save
            "",  # back at existing-config menu after the edit -> Start
            "n",  # one-time MCP auto-setup prompt -> decline
            "",  # preflight -> Start
        ],
        llm=llm,
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    saved = load_engagement_config(tmp_path / "engagement.yaml")
    assert saved.execution_budget.max_actions == 200
    assert saved.scope.allowed_hosts == ["127.0.0.1:5000"]  # untouched by the edit
    assert saved.name == "VulnBank Local"


async def test_edit_refuses_a_hand_authored_engagement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "engagement.yaml").write_text(
        """
        name: "Hand Written"
        scope:
          allowed_hosts: ["a.local"]
        execution_budget:
          max_actions: 5
        """,
        encoding="utf-8",
    )
    llm = _ScriptedIntakeLLM([])
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "e",  # existing-config menu -> Edit (should be refused)
            "q",  # back at the menu, quit
        ],
        llm=llm,
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    unchanged = load_engagement_config(tmp_path / "engagement.yaml")
    assert unchanged.name == "Hand Written"  # never overwritten
    assert any("not generated by the Intake Assistant" in line for line in console.transcript)


async def test_fake_provider_is_rejected_by_production_builder() -> None:
    import pytest

    from redteam.app.bootstrap import build_llm_client
    from redteam.core.config import LLMConfig

    with pytest.raises(ValueError, match="real LLM provider"):
        build_llm_client(LLMConfig(provider="fake"))


def _args():  # noqa: ANN202
    import argparse

    return argparse.Namespace(verbose=False)
