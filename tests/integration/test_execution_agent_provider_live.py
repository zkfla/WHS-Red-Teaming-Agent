"""Opt-in real-provider proof for the fixed execution Agent bridge."""

from __future__ import annotations

import os


def _provider_skip_reason() -> str | None:
    if os.environ.get("REDTEAM_TEST_EXECUTION_LLM") != "1":
        return "set REDTEAM_TEST_EXECUTION_LLM=1 to enable the real-provider proof"
    if os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1":
        return "set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker"
    if not os.environ.get("REDTEAM_TEST_EXECUTION_IMAGE"):
        return "set REDTEAM_TEST_EXECUTION_IMAGE to the digest-pinned text-tools image"
    if os.environ.get("REDTEAM_LLM_PROVIDER") != "litellm":
        return "set REDTEAM_LLM_PROVIDER=litellm"
    if not os.environ.get("REDTEAM_LLM_MODEL"):
        return "set REDTEAM_LLM_MODEL to the provider/model identifier"
    key_name = os.environ.get("REDTEAM_LLM_API_KEY_ENV_VAR", "LLM_API_KEY")
    if not os.environ.get(key_name):
        return f"set the API-key environment variable named by {key_name}"
    return None


PROVIDER_SKIP_REASON = _provider_skip_reason()
