"""Classify before mutation. Model output never supplies runtime identities."""

from __future__ import annotations

from redteam.app.cli.conversation import ConversationAssistant, ConversationDecision

CONTROL_COMMANDS = {
    "stop",
    "pause",
    "resume",
    "approve",
    "deny",
    "exit",
    "quit",
    "승인",
    "거부",
    "중단",
    "종료",
    "focus",
    "exclude",
    "reprioritize",
}
LOCAL_COMMANDS = {
    "status",
    "query",
    "why",
    "explain",
    "help",
    "trace",
    "pending",
    "cancel",
    "상태",
    "현황",
    "설명",
}
ALIASES = {
    "승인": "approve",
    "거부": "deny",
    "중단": "stop",
    "종료": "exit",
    "quit": "exit",
    "query": "status",
    "상태": "status",
    "현황": "status",
    "why": "explain",
    "설명": "explain",
}


def command(text: str) -> tuple[str, str] | None:
    head, _, rest = text.strip().partition(" ")
    name = head.lstrip("/").lower()
    # Bare commands must be exact: 'stop investigating X?' is a chat, not an exit.
    if head.startswith("/") or name in CONTROL_COMMANDS | LOCAL_COMMANDS:
        if (
            rest
            and not head.startswith("/")
            and name
            not in {"approve", "deny", "승인", "거부", "cancel", "focus", "exclude", "reprioritize"}
        ):
            return None
        return ALIASES.get(name, name), rest.strip()
    return None


class InputRouter:
    def __init__(self, conversation: ConversationAssistant) -> None:
        self.conversation = conversation

    async def route(
        self, text: str, *, context: str, active: bool, engagement_name: str, scope: list[str]
    ) -> ConversationDecision:
        decision = await self.conversation.decide(
            text,
            engagement_name=engagement_name,
            scope=scope,
            session_context=context,
            run_active=active,
        )
        # Old schema remains readable, but an ambiguous 'execute' on an active
        # investigation must never be silently converted into guidance.
        if decision.mode == "execute" and active and decision.investigation is None:
            return ConversationDecision(
                mode="clarify", response="현재 조사에 대한 지시인가요, 별도의 새 조사인가요?"
            )
        return decision
