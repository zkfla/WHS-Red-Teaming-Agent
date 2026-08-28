"""Deterministic response comparison without storing response bodies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from whs_redteam.tools.http_client import SafeHttpResponse


@dataclass(frozen=True, slots=True)
class ResponseFingerprint:
    status_code: int
    content_type: str
    body_length: int
    body_sha256: str
    json_shape: Any | None


def _json_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_json_shape(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def fingerprint(response: SafeHttpResponse) -> ResponseFingerprint:
    shape: Any | None = None
    if "json" in response.headers.get("content-type", "").lower():
        try:
            shape = _json_shape(json.loads(response.text))
        except json.JSONDecodeError:
            shape = None
    return ResponseFingerprint(
        status_code=response.status_code,
        content_type=response.headers.get("content-type", "").split(";", 1)[0].lower(),
        body_length=len(response.body),
        body_sha256=hashlib.sha256(response.body).hexdigest(),
        json_shape=shape,
    )


def compare_responses(
    first: SafeHttpResponse,
    second: SafeHttpResponse,
) -> dict[str, bool | int | str | Any | None]:
    left = fingerprint(first)
    right = fingerprint(second)
    return {
        "status_equal": left.status_code == right.status_code,
        "content_type_equal": left.content_type == right.content_type,
        "body_equal": left.body_sha256 == right.body_sha256,
        "body_length_delta": right.body_length - left.body_length,
        "json_shape_equal": left.json_shape == right.json_shape,
        "first_json_shape": left.json_shape,
        "second_json_shape": right.json_shape,
    }
