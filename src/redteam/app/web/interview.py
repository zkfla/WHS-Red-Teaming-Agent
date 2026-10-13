"""LLM-led Engagement interview for the web UI.

The existing ``IntakeAssistant`` asks the questions and proposes a draft. Here the code
decides what is kept: every proposed value goes through the same strict parsing as the
form, and a scope entry the model adds must appear in what the operator actually typed
(or in the draft they started from). The model never saves anything: the result is a
draft the operator reviews and confirms through the normal preview/save path.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from redteam.app.cli.display import safe_text
from redteam.app.web.engagement import (
    SCOPE_FIELDS,
    WEB_PERMISSION_FIELDS,
    EngagementError,
    coerce_field,
    parse_draft,
)
from redteam.app.web.settings import SettingsStore
from redteam.core.config import LLMConfig
from redteam.core.ports.llm import LLMClient, LLMProviderFailure
from redteam.intake.assistant import IntakeAssistant, IntakeAssistantError, IntakeSession
from redteam.intake.models import EngagementDraft, missing_required_fields
from redteam.intake.serializer import merge_draft
from redteam.intake.targets import explicit_target_origins

MAX_MESSAGE_CHARS = 2000
MAX_TURNS = 30
MAX_SESSIONS = 8
SESSION_TTL_SECONDS = 1800.0
TURN_TIMEOUT_SECONDS = 120.0
_REPLY_LANGUAGE = "Korean"
_MODEL_FIELDS = frozenset({"provider", "model", "api_key"})


class InterviewError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail  # structured, bounded diagnostic; never provider bodies


def _diagnostic(exc: BaseException) -> str:
    cause = exc.__cause__
    if isinstance(cause, LLMProviderFailure):
        return safe_text(
            f"{cause.kind} http={cause.http_status or '-'} code={cause.provider_code}", limit=120
        )
    if cause is None or isinstance(cause, ValidationError):
        return "invalid_response"  # the reply did not match the expected shape
    return safe_text(type(cause).__name__, limit=80)


@dataclass
class _Conversation:
    intake: IntakeSession
    base_scope: set[str]
    typed: list[str] = field(default_factory=list)
    turns: int = 0
    touched: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    web_options: dict[str, Any] = field(default_factory=dict)
    target_origins: dict[str, str] = field(default_factory=dict)


def _scope_values(draft: EngagementDraft) -> set[str]:
    return {value.lower() for name in SCOPE_FIELDS for value in getattr(draft, name)}


def _supported(name: str, value: str, typed: str, origins: set[str], base: set[str]) -> bool:
    lowered = value.lower()
    if lowered in base:
        return True
    if name == "allowed_hosts":
        return lowered in origins  # exact host[:port] the operator wrote, nothing widened
    # A whole token only: "example.com" must not be read out of "sub.example.com".
    return re.search(rf"(?<![a-z0-9.-]){re.escape(lowered)}(?![a-z0-9.-])", typed) is not None


def default_llm_factory(settings: SettingsStore) -> Callable[[], LLMClient]:
    def build() -> LLMClient:
        from redteam.app.bootstrap import build_llm_client

        if any(p["field"] in _MODEL_FIELDS for p in settings.problems()):
            raise InterviewError("settings_required")
        try:
            return build_llm_client(LLMConfig.from_environment(settings.env))
        except ValueError:
            raise InterviewError("settings_required") from None

    return build


class InterviewService:
    def __init__(
        self,
        llm_factory: Callable[[], LLMClient],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm_factory = llm_factory
        self._clock = clock
        self._conversations: OrderedDict[str, _Conversation] = OrderedDict()

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, c in self._conversations.items()
                    if now - c.touched > SESSION_TTL_SECONDS]:
            del self._conversations[key]

    def _open(self, base: EngagementDraft, *, editing: bool) -> tuple[str, _Conversation]:
        self._expire()
        while len(self._conversations) >= MAX_SESSIONS:
            self._conversations.popitem(last=False)
        key = secrets.token_urlsafe(16)
        conversation = _Conversation(
            intake=IntakeSession(draft=base, editing=editing),
            base_scope=_scope_values(base),
            touched=self._clock(),
            web_options={key: value for key, value in base.model_dump().items()
                         if key in {"web_policy_mode", "web_paths", *WEB_PERMISSION_FIELDS}},
            target_origins=explicit_target_origins(
                ",".join(getattr(base, "web_origins", [])), address_list=True,
            ) if getattr(base, "web_origins", []) else {},
        )
        self._conversations[key] = conversation
        return key, conversation

    def _find(self, session_id: object) -> _Conversation:
        self._expire()
        conversation = self._conversations.get(session_id) if isinstance(session_id, str) else None
        if conversation is None:
            raise InterviewError("session_expired")
        return conversation

    def discard(self, session_id: object) -> None:
        if isinstance(session_id, str):
            self._conversations.pop(session_id, None)

    async def turn(
        self,
        *,
        session_id: object,
        message: object,
        base: EngagementDraft | None = None,
        editing: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise InterviewError("message_required")
        if len(message) > MAX_MESSAGE_CHARS:
            raise InterviewError("message_too_long")
        if session_id in (None, ""):
            session_id, conversation = self._open(base or EngagementDraft(), editing=editing)
        else:
            conversation = self._find(session_id)
        async with conversation.lock:
            if conversation.turns >= MAX_TURNS:
                raise InterviewError("turn_limit")
            llm = self._llm_factory()
            assistant = IntakeAssistant(llm, reply_language=_REPLY_LANGUAGE)
            conversation.typed.append(message)
            try:
                turn = await asyncio.wait_for(
                    assistant.step(conversation.intake, message), TURN_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                conversation.typed.pop()
                raise InterviewError("assistant_failed", "timeout") from None
            except IntakeAssistantError as exc:
                conversation.typed.pop()
                raise InterviewError("assistant_failed", _diagnostic(exc)) from None
            conversation.turns += 1
            conversation.touched = self._clock()
            return self._apply(str(session_id), conversation, turn)

    def _apply(self, session_id: str, conversation: _Conversation, turn: Any) -> dict[str, Any]:
        typed = "\n".join(conversation.typed).lower()
        stated_origins = explicit_target_origins(typed)
        conversation.target_origins.update(stated_origins)
        origins = {key.lower() for key in stated_origins}
        rejected: list[dict[str, str]] = []
        accepted: dict[str, Any] = {}
        for name, value in turn.draft.model_dump().items():
            if value is None or value == []:
                continue
            try:
                value = coerce_field(name, value)
            except ValueError as exc:
                rejected.append({"field": name, "code": str(exc)})
                continue
            if name in SCOPE_FIELDS:
                kept = []
                for entry in value:
                    if _supported(name, entry, typed, origins, conversation.base_scope):
                        kept.append(entry)
                    else:
                        rejected.append({"field": name, "code": "not_stated_by_operator"})
                value = kept
            accepted[name] = value
        proposed: EngagementDraft
        try:
            proposed = parse_draft(accepted)
        except EngagementError as exc:
            rejected.extend(exc.problems)
            proposed = EngagementDraft()
        merged = merge_draft(conversation.intake.draft, proposed)
        conversation.intake.draft = merged
        missing = missing_required_fields(merged)
        return {
            "session_id": session_id,
            "acknowledgement": safe_text(turn.acknowledgement, limit=600),
            "questions": [safe_text(q, limit=300) for q in turn.follow_up_questions[:5]],
            "draft": {**merged.model_dump(), **conversation.web_options},
            "target_origins": {host: conversation.target_origins[host]
                               for host in merged.allowed_hosts
                               if host in conversation.target_origins},
            "missing": missing,
            "complete": not missing,
            "rejected": rejected[:20],
            "turns_left": MAX_TURNS - conversation.turns,
        }
