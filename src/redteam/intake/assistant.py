"""``IntakeAssistant`` — the only thing in this package that calls an LLM.

Reuses the existing ``core.ports.llm.LLMClient`` Protocol exactly as
``orchestration.agents.planners.LLMPlanner`` does (section 3 of the
spec this module implements): no new LLM abstraction, no Provider SDK
import, no hardcoded model name. Whatever ``LLMConfig``/``bootstrap``
built (LiteLLM or ``FakeLLMClient``) is handed in unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from redteam.core.ports.llm import LLMClient
from redteam.core.redaction import redact_sensitive_text
from redteam.intake.models import EngagementDraft, IntakeTurn
from redteam.intake.prompts import build_edit_context, build_system_prompt

logger = logging.getLogger(__name__)


class IntakeAssistantError(RuntimeError):
    """The LLM call failed or returned something that doesn't parse as
    an ``IntakeTurn``. Callers decide whether to retry or fall back to
    a plain code-driven question — this never silently invents a draft.
    """


@dataclass
class IntakeSession:
    """Turn-by-turn conversation state, held by the CLI layer (not
    persisted) — a fresh session per ``redteam`` setup/edit invocation.
    """

    draft: EngagementDraft = field(default_factory=EngagementDraft)
    history: list[dict[str, Any]] = field(default_factory=list)
    editing: bool = False


class IntakeAssistant:
    def __init__(self, llm: LLMClient, *, reply_language: str | None = None) -> None:
        self._llm = llm
        self._system_prompt = build_system_prompt()
        if reply_language:
            self._system_prompt += (
                f"\n\n`acknowledgement`와 `follow_up_questions`는 {reply_language}로 작성하세요. "
                "한 번에 짧은 질문을 한두 개만 하세요."
            )

    async def step(self, session: IntakeSession, user_message: str) -> IntakeTurn:
        """One conversational turn: user text in, a structured
        :class:`IntakeTurn` out. Never mutates ``session`` itself —
        the caller decides whether/how to merge the result (see
        :func:`redteam.intake.serializer.merge_draft`), matching
        section 12's "patch, don't overwrite" requirement.
        """

        user_message = redact_sensitive_text(user_message)
        system = self._system_prompt
        if session.editing:
            system = f"{system}\n\n{build_edit_context()}"

        context_block = (
            "Current draft (JSON, may be partially filled):\n"
            f"{session.draft.model_dump_json()}\n\n"
            f"User message: {user_message}"
        )
        messages = [*session.history, {"role": "user", "content": context_block}]

        try:
            raw = await self._llm.complete_structured(
                messages=messages,
                response_schema=IntakeTurn.model_json_schema(),
                system=system,
            )
        except Exception as exc:  # noqa: BLE001 - never let a provider error leak a traceback
            # Same rationale as LLMPlanner: message content may include
            # user-provided target/scope text -- log the exception class
            # only, never the payload.
            logger.warning("Intake LLM call failed (%s)", type(exc).__name__)
            raise IntakeAssistantError(f"LLM call failed: {type(exc).__name__}") from exc

        try:
            turn = IntakeTurn.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - malformed structured output, not our bug to hide
            logger.warning("Intake LLM returned an invalid IntakeTurn shape")
            raise IntakeAssistantError(
                "the assistant's response did not match the expected schema"
            ) from exc

        session.history = [
            *messages,
            {"role": "assistant", "content": json.dumps(turn.model_dump())},
        ]
        return turn
