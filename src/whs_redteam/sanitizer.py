"""Central sanitization for logs and persisted metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
SENSITIVE_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "code",
    "cookie",
    "email",
    "jwt",
    "password",
    "phone",
    "refresh_token",
    "reset_token",
    "secret",
    "session",
    "set_cookie",
    "token",
}
URL_PATTERN = re.compile(r"(?:https?|wss?)://[^\s\"'<>]+", re.IGNORECASE)


def _is_sensitive(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    return lowered in SENSITIVE_NAMES or any(
        marker in lowered for marker in ("token", "secret", "password", "api_key")
    )


def sanitize_url(url: str, *, redact_all_query_values: bool = False) -> str:
    """Remove userinfo and redact sensitive query values without changing key order."""

    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            return url
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc += f":{parsed.port}"
        query = urlencode(
            [
                (
                    key,
                    REDACTED
                    if redact_all_query_values or _is_sensitive(key)
                    else value,
                )
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            safe="[]",
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except ValueError:
        return "[INVALID_URL]"


def sanitize_data(value: Any, key: str | None = None) -> Any:
    """Recursively sanitize common secret fields and URL-shaped strings."""

    if key is not None and _is_sensitive(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_data(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str):
        return URL_PATTERN.sub(
            lambda match: sanitize_url(
                match.group(0), redact_all_query_values=True
            ),
            value,
        )
    return value
