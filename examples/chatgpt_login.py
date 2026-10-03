"""Trigger litellm's ChatGPT (Codex-style) OAuth device-flow login once.

litellm's ``chatgpt/`` provider authenticates with a ChatGPT Plus/Pro
subscription via an OAuth device flow and caches the token under
``~/.config/litellm/chatgpt/auth.json`` (override with CHATGPT_TOKEN_DIR).
No API key is used for this provider.

Run this once on a machine with a browser and internet access:

    REDTEAM_LLM_MODEL=chatgpt/gpt-5.5 python examples/chatgpt_login.py

On first run it prints a URL + device code. Sign in with your ChatGPT
account, enter the code, and the token is cached for subsequent real runs.
"""

from __future__ import annotations

import asyncio
import os

from redteam.llm.litellm import LiteLLMClient


async def main() -> None:
    model = os.environ.get("REDTEAM_LLM_MODEL", "chatgpt/gpt-5.5")
    if not model.startswith("chatgpt/"):
        raise SystemExit(
            f"REDTEAM_LLM_MODEL={model!r} is not a chatgpt/ model; this login "
            "helper only applies to the ChatGPT subscription OAuth provider."
        )
    client = LiteLLMClient(model=model)
    print(f"Authenticating {model} (device flow prints a URL + code if not cached)...", flush=True)
    reply = await client.complete(
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    print(f"OK: model responded ({reply.strip()[:40]!r}). Token cached for real runs.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
