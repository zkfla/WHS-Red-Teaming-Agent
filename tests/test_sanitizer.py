import json
from pathlib import Path

from whs_redteam.audit import AuditLogger
from whs_redteam.sanitizer import REDACTED, sanitize_data, sanitize_url


def test_sanitize_url_redacts_secrets_and_removes_userinfo() -> None:
    result = sanitize_url(
        "https://user:pass@example.test/reset?token=secret-value&view=compact#fragment"
    )
    assert result == f"https://example.test/reset?token={REDACTED}&view=compact"
    assert "secret-value" not in result
    assert "user" not in result


def test_inventory_url_mode_redacts_every_query_value() -> None:
    result = sanitize_url(
        "https://example.test/search?q=alice&page=2",
        redact_all_query_values=True,
    )
    assert result == (
        f"https://example.test/search?q={REDACTED}&page={REDACTED}"
    )
    assert "alice" not in result


def test_sanitize_data_recurses_through_nested_fields() -> None:
    result = sanitize_data({"headers": {"Authorization": "Bearer secret"}, "safe": "value"})
    assert result["headers"]["Authorization"] == REDACTED
    assert result["safe"] == "value"


def test_sanitize_data_redacts_url_embedded_in_error_text() -> None:
    result = sanitize_data(
        "navigation failed for https://example.test/reset?token=secret-value after timeout"
    )
    assert "secret-value" not in result
    assert "token=[REDACTED]" in result


def test_audit_logger_never_writes_secret_query_value(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    AuditLogger(path).emit("request", url="https://example.test/?api_key=top-secret")
    raw = path.read_text(encoding="utf-8")
    assert "top-secret" not in raw
    assert json.loads(raw)["url"].endswith(f"api_key={REDACTED}")


def test_audit_logger_redacts_nonsecret_query_values_too(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    AuditLogger(path).emit(
        "request", url="https://example.test/orders?customer=alice&page=2"
    )
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)

    assert "alice" not in raw
    assert event["url"].endswith(f"customer={REDACTED}&page={REDACTED}")
