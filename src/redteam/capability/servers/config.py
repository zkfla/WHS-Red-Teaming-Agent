"""Operator-owned scope and credential references; never populated by the model."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

HTTPMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]

PROTECTED_HEADERS = {
    "host",
    "authorization",
    "proxy-authorization",
    "cookie",
    "content-length",
    "transfer-encoding",
    "connection",
    "upgrade",
    "te",
    "trailer",
    "accept-encoding",
    "content-type",
    "forwarded",
    "x-forwarded-host",
    "x-forwarded-for",
    "proxy-connection",
    "x-original-url",
    "x-rewrite-url",
}


def read_methods() -> list[HTTPMethod]:
    return ["GET", "HEAD", "OPTIONS"]


def valid_query_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", key))


def valid_request_header(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)) and (
        name not in PROTECTED_HEADERS and not name.startswith(("proxy-", "x-forwarded-"))
    )


class TargetProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    allowed_paths: list[str] = Field(default_factory=list)
    allow_all_paths: bool = False
    allowed_methods: list[HTTPMethod] = Field(default_factory=read_methods)
    allowed_path_methods: dict[str, list[HTTPMethod]] = Field(default_factory=dict)
    replayable_paths: list[str] = Field(default_factory=list)
    mutable_json_fields: dict[str, list[str]] = Field(default_factory=dict)
    allowed_query_keys: dict[str, list[str]] = Field(default_factory=dict)
    allowed_request_headers: list[str] = Field(default_factory=list)
    allow_all_query_keys: bool = False
    allowed_global_query_keys: list[str] = Field(default_factory=list, max_length=100)
    allow_all_request_headers: bool = False
    request_body_enabled: bool = True  # Historical JSON callers retain their contract.
    response_body_max_chars: int = Field(default=0, ge=0, le=16384)

    @field_validator("allowed_request_headers")
    @classmethod
    def valid_headers(cls, values: list[str]) -> list[str]:
        lowered = [name.lower() for name in values]
        if any(
            not valid_request_header(name)
            for name in lowered
        ) or len(set(lowered)) != len(lowered):
            raise ValueError("invalid, protected or duplicate request header")
        return lowered

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin must be http(s) scheme://host[:port], without credentials")
        _ = parsed.port
        return value.rstrip("/")

    @field_validator("allowed_paths")
    @classmethod
    def valid_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith("/") or any(c in value for c in "?#%\\\r\n"):
                raise ValueError("paths must be literal absolute URL paths")
            if "//" in value or any(part in {".", ".."} for part in value.split("/")):
                raise ValueError("ambiguous path")
        return values

    @model_validator(mode="after")
    def proxy_paths_are_explicitly_scoped(self) -> TargetProfile:
        if not self.allow_all_paths and not self.allowed_paths:
            raise ValueError("allowed_paths must be non-empty unless allow_all_paths is explicit")
        allowed = set(self.allowed_paths)
        if set(self.allowed_query_keys) - allowed:
            raise ValueError("query rules must reference allowed paths")
        for keys in self.allowed_query_keys.values():
            if len(set(keys)) != len(keys) or any(
                not valid_query_key(key) for key in keys
            ):
                raise ValueError("invalid or duplicate query key")
        if set(self.allowed_path_methods) - allowed:
            raise ValueError("allowed_path_methods keys must be allowed paths")
        if set(self.replayable_paths) - allowed:
            raise ValueError("replayable_paths must be allowed paths")
        if set(self.mutable_json_fields) - set(self.replayable_paths):
            raise ValueError("mutable_json_fields keys must be replayable paths")
        for methods in self.allowed_path_methods.values():
            if not methods or len(methods) != len(set(methods)):
                raise ValueError("path method lists must be non-empty and unique")
        for fields in self.mutable_json_fields.values():
            if (
                not fields
                or len(fields) != len(set(fields))
                or any(not field or len(field) > 100 for field in fields)
            ):
                raise ValueError("mutable JSON field lists must be non-empty and unique")
        if not self.allowed_methods or len(set(self.allowed_methods)) != len(self.allowed_methods):
            raise ValueError("method lists must be non-empty and unique")
        if len(set(self.allowed_global_query_keys)) != len(self.allowed_global_query_keys) or any(
            not valid_query_key(key) for key in self.allowed_global_query_keys
        ):
            raise ValueError("invalid or duplicate global query key")
        return self

    def allows_query_key(self, path: str, key: str) -> bool:
        return valid_query_key(key) and (
            self.allow_all_query_keys or key in self.allowed_global_query_keys
            or key in self.allowed_query_keys.get(path, [])
        )

    def allows_request_header(self, name: str) -> bool:
        return valid_request_header(name) and (
            self.allow_all_request_headers or name in self.allowed_request_headers
        )

    def allows_path(self, path: str) -> bool:
        """Whole-origin permission still accepts only literal local URL paths."""
        try:
            self.valid_paths([path])
        except ValueError:
            return False
        return self.allow_all_paths or path in self.allowed_paths

    @model_serializer(mode="wrap")
    def original_path_payload(self, handler):
        # Keep existing generated policy bytes/digests stable for exact-path
        # profiles. Whole-origin permission is a new explicit serialized value.
        payload = handler(self)
        if not self.allow_all_paths:
            payload.pop("allow_all_paths", None)
        # New permissions must not change bytes/digests of existing policies.
        for name, default in {
            "allow_all_query_keys": False, "allowed_global_query_keys": [],
            "allow_all_request_headers": False, "request_body_enabled": True,
            "response_body_max_chars": 0,
        }.items():
            if payload.get(name) == default:
                payload.pop(name, None)
        return payload


class SessionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_ref: str
    principal_ref: str
    bearer_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    storage_state_path: str | None = None
    storage_state_max_age_seconds: int = Field(default=3600, ge=1, le=86400)
    # None preserves existing operator profiles. Generated deployments always set this.
    allowed_paths: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_credential(self) -> SessionProfile:
        if bool(self.bearer_env) == bool(self.storage_state_path):
            raise ValueError("configure exactly one bearer_env or storage_state_path")
        if self.storage_state_path and not Path(self.storage_state_path).is_absolute():
            raise ValueError("storage_state_path must be absolute")
        return self


class ServerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    targets: dict[str, TargetProfile] = Field(min_length=1)
    allowed_networks: list[str] = Field(min_length=1)
    sessions: dict[str, SessionProfile] = Field(default_factory=dict)
    max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    timeout_seconds: float = Field(default=5, gt=0, le=15)
    active_enabled: bool = False
    active_ports: list[int] = Field(default_factory=lambda: [80, 443], max_length=16)
    proxy_replay_enabled: bool = False
    proxy_max_flows: int = Field(default=500, ge=1, le=1000)

    @field_validator("allowed_networks")
    @classmethod
    def valid_networks(cls, values: list[str]) -> list[str]:
        for value in values:
            ipaddress.ip_network(value, strict=True)
        return values

    @field_validator("active_ports")
    @classmethod
    def valid_ports(cls, values: list[int]) -> list[int]:
        if not values or len(values) != len(set(values)) or any(p < 1 or p > 65535 for p in values):
            raise ValueError("active_ports must be 1-16 unique ports in 1..65535")
        return values

    @model_validator(mode="after")
    def session_targets_exist(self) -> ServerPolicy:
        if any(session.target_ref not in self.targets for session in self.sessions.values()):
            raise ValueError("session target_ref does not exist")
        for session in self.sessions.values():
            if session.allowed_paths is not None and (
                len(set(session.allowed_paths)) != len(session.allowed_paths)
                or any(not self.targets[session.target_ref].allows_path(path)
                       for path in session.allowed_paths)
            ):
                raise ValueError("session paths must be unique allowed target paths")
        return self

    @classmethod
    def load(cls, path: str, *, expected_sha256: str | None = None) -> ServerPolicy:
        content = Path(path).read_bytes()
        if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("MCP policy digest mismatch")
        return cls.model_validate(json.loads(content))
