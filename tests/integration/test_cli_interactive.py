"""``redteam`` with no subcommand (sections 9-12, 26): in-process so the
Intake Assistant's LLM call and the terminal's stdin can both be
scripted deterministically, instead of a real network/tty.

Real ``run_command.build``/``execute`` still runs for real underneath
(same ``ExampleMCPAdapter``/``FakeLLMClient`` path
``tests/integration/test_cli_smoke.py`` already exercises) -- only the
Intake Assistant's own LLM client and the terminal are test doubles.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
import yaml

from redteam.app.cli import _shared
from redteam.app.cli.commands import interactive
from redteam.core.config import load_engagement_config

pytestmark = pytest.mark.asyncio

_APP_YAML = """
llm:
  provider: fake
  default_model: gpt-4o-mini
orchestration:
  max_retries: 3
  max_steps: 20
capability:
  mcp_servers: []
logging:
  level: INFO
"""


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[str], llm  # noqa: ANN001
) -> _FakeConsole:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.yaml").write_text(_APP_YAML, encoding="utf-8")
    console = _FakeConsole(answers)
    monkeypatch.setattr(interactive, "Console", lambda: console)
    monkeypatch.setattr(interactive, "build_llm_client", lambda config: llm)  # noqa: ARG005
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


async def test_no_config_intake_completes_preflight_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([dict(_COMPLETE_TURN)])
    _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "127.0.0.1:5000의 VulnBank 테스트, 인증/금융 로직 위주, "
            "DoS/데이터삭제 금지, 최대 100개",
            "",  # existing-config menu -> Start (config was just created)
            "",  # preflight -> Start
        ],
        llm=llm,
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    saved = load_engagement_config(tmp_path / "engagement.yaml")
    assert saved.name == "VulnBank Local"
    # Section 6: exactly what the LLM/user produced, nothing widened.
    assert saved.scope.allowed_hosts == ["127.0.0.1:5000"]
    assert saved.scope.allowed_domains == []
    assert saved.scope.allowed_networks == []
    assert saved.metadata["source"] == "intake-assistant"


async def test_intake_llm_never_receives_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([dict(_COMPLETE_TURN)])
    _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "127.0.0.1:5000의 VulnBank 테스트, 인증/금융 로직 위주, "
            "DoS/데이터삭제 금지, 최대 100개",
            "",
            "",
        ],
        llm=llm,
    )

    await interactive.run(_args())

    for call in llm.calls:
        blob = str(call["messages"]) + str(call["system"])
        assert "sk-" not in blob
        assert "Bearer" not in blob
        assert "api_key" not in blob


async def test_blank_first_answer_aborts_without_writing_engagement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _ScriptedIntakeLLM([])
    _patch(monkeypatch, tmp_path, answers=[""], llm=llm)

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert not (tmp_path / "engagement.yaml").exists()


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
            "최대 action을 200으로 바꿔줘",
            "",  # back at existing-config menu after the edit -> Start
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


def _args():  # noqa: ANN202
    import argparse

    return argparse.Namespace(verbose=False)
