"""AppConfig -> bootstrap wiring test (explicit test-LLM injection).

Covers: every AppConfig section reaches its component, and every
dependency ``build_application`` would otherwise build can be
overridden with a Fake (instruction.md section 1/21 of the follow-up
brief).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from redteam.app import bootstrap
from redteam.core.config import CapabilityConfig, LLMConfig


def test_build_llm_client_rejects_fake_provider() -> None:
    with pytest.raises(ValueError, match="real LLM provider"):
        bootstrap.build_llm_client(LLMConfig(provider="fake"))


def test_llm_config_requires_explicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="REDTEAM_LLM_PROVIDER"):
        LLMConfig.from_environment()


def test_termination_receipt_trust_comes_only_from_verified_role_metadata(monkeypatch):
    from types import SimpleNamespace

    from redteam.capability import server_roles

    definitions = {
        "web-http": SimpleNamespace(supports_execution_termination=True),
        "recon-passive": SimpleNamespace(supports_execution_termination=False),
    }
    monkeypatch.setattr(server_roles, "role_spec", definitions.get)
    verified = SimpleNamespace(spec=SimpleNamespace(roles=("web-http", "recon-passive")))

    assert bootstrap._trusted_termination_servers(None) == ()
    assert bootstrap._trusted_termination_servers(verified) == ("web-http",)


async def test_default_capability_passes_verified_termination_trust(monkeypatch):
    sentinel = object()
    build = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(bootstrap, "build_mcp_tool_gateway", build)
    result = await bootstrap._build_default_capability(
        CapabilityConfig(mcp_servers=["web-http"]),
        trusted_termination_servers=("web-http",),
    )

    assert result == (sentinel, sentinel)
    assert build.await_args.kwargs["trusted_termination_servers"] == ("web-http",)
