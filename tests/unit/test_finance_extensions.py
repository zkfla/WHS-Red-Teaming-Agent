"""Request extensions and browser snapshot credentials obey existing target boundaries."""

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from redteam.capability.servers import network
from redteam.capability.servers.config import ServerPolicy, SessionProfile, TargetProfile
from redteam.capability.servers.network import (
    ResolvedTarget,
    ToolInputError,
    request,
    session_header,
)


def policy() -> ServerPolicy:
    return ServerPolicy.model_validate({
        "targets": {"bank": {
            "origin": "https://bank.test",
            "allowed_paths": ["/account", "/account/child", "/other"],
            "allowed_query_keys": {"/account": ["search"]},
            "allowed_request_headers": ["X-Test-Case"],
        }},
        "allowed_networks": ["192.0.2.0/24"],
    })


async def test_query_encoded_once_headers_scoped_and_output_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = policy()

    async def resolve(*args: Any) -> ResolvedTarget:
        return ResolvedTarget(config.targets["bank"], "bank.test", 443, ("192.0.2.1",))

    monkeypatch.setattr(network, "resolve_target", resolve)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "192.0.2.1" and req.headers["host"] == "bank.test:443"
        assert req.url.params["search"] == "한글&secret=1"
        assert list(req.url.params) == ["search"]
        assert req.headers["x-test-case"] == "private-case"
        return httpx.Response(200, content=b"private-body")

    result = await request(
        config, "bank", "/account", query={"search": "한글&secret=1"},
        request_headers={"X-Test-Case": "private-case"}, transport=httpx.MockTransport(handler),
    )
    assert "private" not in str(result.summary()) and "secret" not in str(result.summary())


@pytest.mark.parametrize("kwargs", [
    {"query": {"unapproved": "x"}}, {"query": {"search": "x" * 1025}},
    {"request_headers": {"Host": "elsewhere.test"}},
    {"request_headers": {"Authorization": "secret"}},
    {"request_headers": {"X-Test-Case": "ok\r\nHost: elsewhere"}},
    {"request_headers": {"X-Test-Case": "a", "x-test-case": "b"}},
])
async def test_invalid_request_extensions_fail_before_dns(
    kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: Any) -> None:
        pytest.fail("invalid arguments reached DNS")

    monkeypatch.setattr(network, "resolve_target", fail)
    with pytest.raises(ToolInputError):
        await request(policy(), "bank", "/account", **kwargs)


@pytest.mark.parametrize("name", ["Host", "Cookie", "X-Rewrite-URL", "X-Forwarded-Proto"])
def test_operator_cannot_allow_routing_or_credential_headers(name: str) -> None:
    with pytest.raises(ValidationError):
        TargetProfile(
            origin="https://bank.test", allowed_paths=["/"], allowed_request_headers=[name],
        )


def snapshot_policy(tmp_path: Path, **overrides: Any) -> tuple[ServerPolicy, Path]:
    state = tmp_path / "state.json"
    cookie = {
        "name": "sid", "value": "private-cookie", "domain": "bank.test",
        "path": "/account", "expires": time.time() + 300, "secure": True,
        **overrides,
    }
    state.write_text(json.dumps({"cookies": [cookie], "origins": []}))
    state.chmod(0o600)
    config = policy().model_dump()
    config["sessions"] = {"alice": {
        "target_ref": "bank", "principal_ref": "alice", "storage_state_path": str(state),
    }}
    return ServerPolicy.model_validate(config), state


def test_snapshot_reloaded_without_exposing_cookie(tmp_path: Path) -> None:
    config, state = snapshot_policy(tmp_path)
    assert session_header(config, "bank", "alice", "/account/child") == {
        "Cookie": "sid=private-cookie",
    }
    state.write_text(state.read_text().replace("private-cookie", "refreshed-cookie"))
    assert session_header(config, "bank", "alice", "/account")["Cookie"] == "sid=refreshed-cookie"
    with pytest.raises(ToolInputError):
        session_header(config, "bank", "alice", "/other")
    with pytest.raises(ToolInputError):
        session_header(config, "elsewhere", "alice", "/account")


@pytest.mark.parametrize("override", [
    {"domain": "elsewhere.test"}, {"domain": ".bank.test"}, {"expires": 1},
    {"value": "secret; other=injected"}, {"value": "secret\r\nInjected"},
    {"partitionKey": "https://elsewhere.test"},
])
def test_cookie_boundaries(tmp_path: Path, override: dict[str, Any]) -> None:
    config, _ = snapshot_policy(tmp_path, **override)
    with pytest.raises(ToolInputError):
        session_header(config, "bank", "alice", "/account")


def test_cookie_snapshot_requires_private_fresh_regular_file(tmp_path: Path) -> None:
    config, state = snapshot_policy(tmp_path)
    state.chmod(0o644)
    with pytest.raises(ToolInputError):
        session_header(config, "bank", "alice", "/account")
    state.chmod(0o600)
    os.utime(state, (1, 1))
    with pytest.raises(ToolInputError):
        session_header(config, "bank", "alice", "/account")


def test_bearer_and_cookie_sources_cannot_mix() -> None:
    with pytest.raises(ValidationError):
        SessionProfile(
            target_ref="bank", principal_ref="a", bearer_env="TOKEN", storage_state_path="/state",
        )


async def test_cookie_request_sends_cookie_not_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = snapshot_policy(tmp_path)

    async def resolve(*args: Any) -> ResolvedTarget:
        return ResolvedTarget(config.targets["bank"], "bank.test", 443, ("192.0.2.1",))

    monkeypatch.setattr(network, "resolve_target", resolve)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["cookie"] == "sid=private-cookie"
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"account": "private-account"})

    result = await request(
        config, "bank", "/account", session_ref="alice", transport=httpx.MockTransport(handler),
    )
    assert "private" not in str(result.summary())


def test_secure_cookie_not_sent_to_http_or_symlink(tmp_path: Path) -> None:
    config, state = snapshot_policy(tmp_path)
    insecure = config.model_dump()
    insecure["targets"]["bank"]["origin"] = "http://bank.test"
    with pytest.raises(ToolInputError):
        session_header(ServerPolicy.model_validate(insecure), "bank", "alice", "/account")
    link = tmp_path / "link.json"
    link.symlink_to(state)
    modified = config.model_dump()
    modified["sessions"]["alice"]["storage_state_path"] = str(link)
    with pytest.raises(ToolInputError):
        session_header(ServerPolicy.model_validate(modified), "bank", "alice", "/account")
