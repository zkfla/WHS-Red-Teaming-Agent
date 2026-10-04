"""Persistent interactive session (asd.md / prior pasted spec sections
4-5, 12-15, 19): a Run ending must not end the CLI process, slash
commands must reuse the same Application Services as the standalone
subcommands, and a second natural-language request must go through the
same Runtime path as the first -- all through the real
``redteam.app.cli.commands.interactive.run()`` entry point, exactly like
``tests/integration/test_cli_interactive.py`` (same in-process
terminal and runtime conversation/model doubles -- the Agent
Runtime, Control Plane, and Capability Gateway run for real against an
honest empty catalog (no MCP servers configured). The execution model is
an explicitly injected hold plan and the Intake Assistant uses its own
explicit scripted double; the production LLM builder is never patched).

Covers section 29's "Integration"/"CLI E2E" requirements for this
feature: Interactive -> Application, Slash command -> Shared Service,
Run lifecycle -> Session lifecycle, and the full
``/help`` -> natural language -> ``/status`` -> ``/tools`` -> ``/exit``
scenario in one process.
"""

from __future__ import annotations

import contextlib
import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from redteam.app.cli import _shared
from redteam.app.cli.commands import interactive
from redteam.app.cli.commands import run as run_command

pytestmark = pytest.mark.asyncio

_COMPLETE_TURN = {
    "draft": {
        "name": "VulnBank Local",
        "allowed_hosts": ["127.0.0.1:5000"],
        "max_actions": 100,
    },
    "acknowledgement": "확인했습니다.",
    "follow_up_questions": [],
    "ready": True,
}


class _FakeConsole:
    """Same shape as ``test_cli_interactive.py``'s own fake."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.transcript: list[str] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        for arg in args:
            if isinstance(arg, str):
                self.transcript.append(arg)
            else:
                output = StringIO()
                Console(file=output, width=140, color_system=None).print(arg)
                self.transcript.append(output.getvalue())

    def input(self, prompt: str = "") -> str:
        self.transcript.append(str(prompt))
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)

    def status(self, *_args: object, **_kwargs: object):  # noqa: ANN201
        return contextlib.nullcontext()

    def clear(self) -> None:
        self.transcript.append("<cleared>")


class _ScriptedIntakeLLM:
    """Only ever used for the Intake Assistant -- the orchestration
    Runtime resolves its own ``FakeLLMClient`` independently from
    ``full_config.llm.provider`` (``app.bootstrap.build_llm_client``,
    not the module-level function this test monkeypatches), so scripting
    exactly one Intake turn here never starves the planner.
    """

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)

    async def complete(self, *, messages, system=None, **kwargs):  # noqa: ANN001, ANN003
        return "ack"

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):  # noqa: ANN001, ANN003
        return self._turns.pop(0)


class _ConversationLLM:
    """Explicit conversation decisions, independent of intake and engine scripts."""

    async def complete_structured(self, *, messages, **kwargs):
        payload = json.loads(messages[0]["content"])
        message = payload["user_message"]
        if message in {"방금 왜 실패했어?", "결과에서 확인된 내용이 뭐야?"}:
            return {
                "mode": "answer",
                "response": "[Agent] Run summary because " + payload["session_context"],
            }
        return {"mode": "execute", "response": "등록된 범위를 조사합니다.", "objective": message}

    async def complete(self, **kwargs):
        raise AssertionError("conversation uses structured decisions only")


class _HoldLLM:
    """Execution model: valid draft/review, then hold forever (no tools)."""

    def __init__(self):
        self.calls = []

    async def complete(self, *, messages, system=None, **kwargs):
        return ""

    @staticmethod
    def _route_key(schema):
        props = schema.get("properties", {})
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    @staticmethod
    def _find_host(messages):
        import re

        blob = str([m.get("content") if isinstance(m, dict) else "" for m in (messages or [])])
        found = re.search(r"host:[A-Za-z0-9.\-_:]+", blob)
        if found:
            return found.group(0).rstrip(".,;:")
        return "host:127.0.0.1:5000"

    def _draft(self, node_id):
        # Minimal model draft: purpose + steps only. Runtime identity is code-owned.
        return {
            "reason": "session hold plan",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "hold",
                    "agent_id": "recon-agent",
                    "target_node_ids": [node_id],
                    "principal_refs": [],
                    "allowed_operations": ["recon.service_discovery"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [
                        {"kind": "evidence", "description": "hold", "min_evidence": 1}
                    ],
                    "required": True,
                }
            ],
        }

    @staticmethod
    def _hold():
        return {
            "mode": "hold",
            "reason": "no tools configured",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        route = self._route_key(response_schema)
        self.calls.append(route)
        if route == "path_draft":
            return self._draft(self._find_host(messages))
        if route == "path_review":
            return {"decision": "accept", "reason": "session hold", "path": None}
        if route == "next_action":
            return self._hold()
        if route == "completion_review":
            # Step review keeps the old accepted/reason shape (no claims).
            return {"accepted": True, "reason": "session hold"}
        if route == "task_completion_review":
            # Task review needs claims; hold flow never completes so this stays unproven.
            return {"accepted": False, "reason": "session hold", "claims": []}
        if route == "goal_completion_review":
            import json as _json2

            payload = _json2.loads(messages[0]["content"]) if messages else {}
            criteria = (payload.get("mission") or {}).get("success_criteria", []) or []
            return {
                "accepted": True,
                "reason": "session hold",
                "criteria": [
                    {
                        "criterion": c,
                        "met": False,
                        "evidence_ids": [],
                        "claims": [],
                    }
                    for c in criteria
                ],
            }
        if route == "analysis_verify":
            return {
                "meaning": "hold",
                "outcome": "success",
                "reason": "hold",
                "confidence": 0.8,
                "claim_supported": True,
            }
        raise AssertionError(route)


def _patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[str]) -> _FakeConsole:
    monkeypatch.chdir(tmp_path)
    # REDTEAM_LLM_PROVIDER already set (section 4/20's canonical env source)
    # means `_ensure_runtime_config` skips its one-time prompt entirely --
    # no application-config file involved.
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("REDTEAM_LLM_MODEL", "gpt-4o-mini")
    # Explicit adapter choice avoids consuming the session's answers in MCP setup.
    # The separate MCP tests cover opt-in prompts and declining registration.
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "example")
    console = _FakeConsole(answers)
    monkeypatch.setattr(interactive, "Console", lambda: console)
    monkeypatch.setattr(interactive, "build_llm_client", lambda config: _ConversationLLM())
    hold_llm = _HoldLLM()
    original_build = run_command.build_application

    async def _build_with_explicit_llm(config, **kwargs):
        kwargs.setdefault("llm", hold_llm)
        return await original_build(config, **kwargs)

    monkeypatch.setattr(run_command, "build_application", _build_with_explicit_llm)
    return console


def _args():  # noqa: ANN202
    import argparse

    return argparse.Namespace(verbose=False)


# --- Gate F / A: RunFinished returns to prompt, session stays alive -------


async def test_run_finished_returns_to_prompt_and_accepts_slash_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",  # fixed setup answers
            "",  # existing-config menu -> Start
            "",  # preflight -> Start
            "/help",
            "/status",
            "/tools",
            "/exit",
        ],
    )

    exit_code = await interactive.run(_args())
    # /status and /tools call the exact same `commands.status.run`/
    # `commands.tools.run_list` the standalone subcommands use (Gate D) --
    # those print via plain `print()`, captured by pytest's `capsys`, not
    # `console.print` (the Rich-rendered event/panel output).
    capsys.readouterr()

    assert exit_code == _shared.EXIT_OK
    # RunFinished happened (a report/summary was rendered)...
    assert any("RunId:" in line for line in console.transcript)
    # ...and the session did NOT end there: /help ran afterward.
    assert any("Commands" in line for line in console.transcript)
    # /status (no argument) resolved against the session's own last run id --
    # never a hardcoded/faked run id.
    assert any("Phase: finished" in line for line in console.transcript)
    # A missing MCP env now gets the same safe built-in baseline the Run used.
    assert "recon.ping" in "\n".join(console.transcript)
    assert "web.http_request" not in "\n".join(
        console.transcript
    )  # no implicit web server registration


async def test_natural_language_second_request_runs_a_second_agent_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate B + Gate D: a second plain-text line at the persistent prompt
    is routed to Intake -> the exact same Application/Runtime path the
    first request used -- not a second, parallel implementation."""
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            "127.0.0.1:5000 다시 한 번 점검해줘",  # second objective, plain text
            "/exit",
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    run_id_lines = [line for line in console.transcript if line.startswith("실행 종료 · RunId:")]
    # Two separate Agent Runs actually executed through the Runtime --
    # not one Run reused/replayed for the second request.
    assert len(run_id_lines) == 2
    assert run_id_lines[0] != run_id_lines[1]


async def test_natural_language_explanation_does_not_start_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentence containing ``왜`` is a query even when it is not the
    first token; it must describe the last Run instead of becoming a Mission
    objective."""
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            "방금 왜 실패했어?",
            "/exit",
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert len([line for line in console.transcript if line.startswith("실행 종료 · RunId:")]) == 1
    assert any("Run summary because" in line for line in console.transcript)


async def test_free_form_question_is_answered_without_starting_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            "결과에서 확인된 내용이 뭐야?",
            "/exit",
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert len([line for line in console.transcript if line.startswith("실행 종료 · RunId:")]) == 1
    assert console.transcript.count("\n[cyan]❯[/cyan] ") >= 2


async def test_bare_exit_ends_session_without_starting_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            "exit",
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert len([line for line in console.transcript if line.startswith("실행 종료 · RunId:")]) == 1
    assert any(line.strip() == "❯ exit" for line in console.transcript)


# --- Gate I: error recovery ------------------------------------------------


async def test_unknown_slash_command_does_not_end_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            "/this-command-does-not-exist",
            "/exit",
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert any("Unknown command" in line for line in console.transcript)
    # Reached /exit's own dispatch (not an EOF/forced fallback): the
    # prompt after the bad command was still answered and consumed.
    assert console.transcript.count("\n[cyan]❯[/cyan] ") >= 2


# --- Gate K: clean exit on EOF while idle at the prompt --------------------


async def test_eof_at_idle_prompt_ends_the_session_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _patch(
        monkeypatch,
        tmp_path,
        answers=[
            "VulnBank Local",
            "127.0.0.1:5000",
            "",
            "",
            "inspect",
            "recon",
            "100",
            "n",
            "",
            "",
            # nothing after the first run -- console.input() raises EOFError
        ],
    )

    exit_code = await interactive.run(_args())

    assert exit_code == _shared.EXIT_OK
    assert any("Session ended" in line for line in console.transcript)
