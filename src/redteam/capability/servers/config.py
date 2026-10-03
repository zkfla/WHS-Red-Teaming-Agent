"""Operator-owned scope and credential references; never populated by the model."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class TargetProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    allowed_paths: list[str] = Field(min_length=1)
    allowed_methods: list[HTTPMethod] = Field(default_factory=read_methods)
    allowed_path_methods: dict[str, list[HTTPMethod]] = Field(default_factory=dict)
    replayable_paths: list[str] = Field(default_factory=list)
    mutable_json_fields: dict[str, list[str]] = Field(default_factory=dict)
    allowed_query_keys: dict[str, list[str]] = Field(default_factory=dict)
    allowed_request_headers: list[str] = Field(default_factory=list)

    @field_validator("allowed_request_headers")
    @classmethod
    def valid_headers(cls, values: list[str]) -> list[str]:
        lowered = [name.lower() for name in values]
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)
            or name in PROTECTED_HEADERS
            or name.startswith(("proxy-", "x-forwarded-"))
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
        allowed = set(self.allowed_paths)
        if set(self.allowed_query_keys) - allowed:
            raise ValueError("query rules must reference allowed paths")
        for keys in self.allowed_query_keys.values():
            if len(set(keys)) != len(keys) or any(
                not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", key) for key in keys
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
        return self


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
                or set(session.allowed_paths) - set(self.targets[session.target_ref].allowed_paths)
            ):
                raise ValueError("session paths must be unique allowed target paths")
        return self

    @classmethod
    def load(cls, path: str, *, expected_sha256: str | None = None) -> ServerPolicy:
        content = Path(path).read_bytes()
        if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("MCP policy digest mismatch")
        return cls.model_validate(json.loads(content))
