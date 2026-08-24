from __future__ import annotations

import json
from dataclasses import dataclass

from hackingBuddyGPT.usecases.agents import ChatAgent
from hackingBuddyGPT.utils.limits import Limits

from cicd_redteam_agent.security.redaction import redact


def _redact_argument_value(value):
    if isinstance(value, dict):
        return {key: _redact_argument_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_argument_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


@dataclass
class CICDChatAgent(ChatAgent):
    prompt: str = ""

    async def system_message(self, limits: Limits) -> str:
        return self.prompt

    async def run_capability_json(
        self,
        message_id: int,
        tool_call_id: str,
        capability_name: str,
        arguments: str,
        capabilities=None,
    ) -> str:
        try:
            safe_arguments = json.dumps(_redact_argument_value(json.loads(arguments)))
        except (json.JSONDecodeError, TypeError):
            safe_arguments = redact(arguments)
        return await super().run_capability_json(
            message_id,
            tool_call_id,
            capability_name,
            safe_arguments,
            capabilities,
        )
