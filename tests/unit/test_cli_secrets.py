"""Section 22: the CLI must never print bearer tokens/API keys/etc."""

from __future__ import annotations

from redteam.app.cli._shared import mask_secrets


def test_masks_known_secret_key_names() -> None:
    masked = mask_secrets(
        {
            "api_key": "sk-live-abc123",
            "Authorization": "Bearer eyJhbGciOi...",
            "password": "hunter2",
            "mcp_bearer_token": "tok_abc",
            "note": "this is fine",
        }
    )
    assert masked["api_key"] == "***REDACTED***"
    assert masked["Authorization"] == "***REDACTED***"
    assert masked["password"] == "***REDACTED***"
    assert masked["mcp_bearer_token"] == "***REDACTED***"
    assert masked["note"] == "this is fine"


def test_masks_nested_structures() -> None:
    masked = mask_secrets(
        {
            "capability": {"llm_api_key": "sk-abc"},
            "servers": [{"token": "t1"}, {"host": "a.local"}],
        }
    )
    assert masked["capability"]["llm_api_key"] == "***REDACTED***"
    assert masked["servers"][0]["token"] == "***REDACTED***"
    assert masked["servers"][1]["host"] == "a.local"


def test_leaves_non_secret_data_untouched() -> None:
    payload = {"tool_id": "recon.ping", "risk_level": "low", "count": 3}
    assert mask_secrets(payload) == payload
