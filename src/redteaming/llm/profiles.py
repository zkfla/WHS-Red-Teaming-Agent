from __future__ import annotations

import os

from .client import LLMConfig


def oauth_config() -> LLMConfig:
    os.environ["REDTEAM_LLM_BACKEND"] = "litellm"
    os.environ["REDTEAM_LLM_MODEL"] = os.getenv("REDTEAM_OAUTH_MODEL", "chatgpt/gpt-5.5")
    os.environ.pop("REDTEAM_LLM_API_KEY", None)
    return LLMConfig.from_env()  # type: ignore[return-value]
