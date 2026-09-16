"""Operator-owned scope and credential references; never populated by the model."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HTTPMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]


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
    bearer_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        return self

    @classmethod
    def load(cls, path: str) -> ServerPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
