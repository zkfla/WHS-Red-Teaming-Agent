"""The real ``run_session`` wiring driven through the web frontend (no LLM, MCP or target)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from redteam.app.web.host import RealLauncher, SessionHost
from redteam.app.web.settings import (
    ENV_MODEL,
    ENV_POLICY,
    ENV_PROFILE,
    ENV_PROVIDER,
    SettingsStore,
)
from redteam.core.config import AppConfig, LLMConfig, load_engagement_config

ENGAGEMENT = Path(__file__).parents[2] / "examples" / "engagement.yaml"


class OfflineLauncher(RealLauncher):
    def build_config(self):
        return AppConfig(
            engagement=load_engagement_config(ENGAGEMENT), llm=LLMConfig(default_model="x/model")
        )


async def wait_for(predicate, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_local_commands_run_through_the_real_session_and_stdout_is_restored(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("redteam.app.bootstrap.build_llm_client", lambda config: object())
    env = {
        ENV_PROVIDER: "litellm",
        ENV_MODEL: "x/model",
        "LLM_API_KEY": "sk-test-not-real",
        ENV_PROFILE: "config/agents/example.yaml",
        ENV_POLICY: json.dumps(
            {"mode": "bounded", "max_requests": 5, "max_concurrent_requests": 1}
        ),
    }
    settings = SettingsStore(env=env, state_path=tmp_path / "s.json", profile_dir=tmp_path)
    host = SessionHost(
        settings=settings,
        launcher=OfflineLauncher(ENGAGEMENT),
        engagement_name="Example",
        scope=("example.local",),
    )
    original_stdout = sys.stdout
    await host.start()
    await wait_for(lambda: host.state == "ready")

    host.submit_input("/help", request_id=None, binding=None)
    await wait_for(
        lambda: any(
            item["kind"] == "console" and "/new" in item["data"]["text"]
            for item in host.log.tail()
        )
    )
    state = host.snapshot()["state"]
    assert state["phase"] == "idle"
    assert state["engagement"] == load_engagement_config(ENGAGEMENT).name

    host.submit_input("/exit", request_id=None, binding=None)
    await wait_for(lambda: host.state == "stopped")
    assert sys.stdout is original_stdout
    await host.shutdown()
