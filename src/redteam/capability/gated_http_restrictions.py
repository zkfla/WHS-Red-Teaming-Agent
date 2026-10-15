"""Typed HTTP origin restriction projection of a verified MCP deployment.

The projection records which origin host and port carry HTTP restrictions that a
raw gated TCP or UDP route cannot enforce. It is derived only from a deployment
spec that the app layer has verified in full. It never reads role names,
command arguments, or model input.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.capability.deployment import DeploymentSpec
from redteam.capability.servers.config import TargetProfile
from redteam.core.convert import digest_payload

_SHA256 = r"^[a-f0-9]{64}$"


def _sorted_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _sorted_mapping(
    mapping: Mapping[str, Iterable[str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((key, _sorted_unique(values)) for key, values in sorted(mapping.items()))


class OriginRestriction(BaseModel):
    """Canonical, immutable copy of one target's HTTP restriction fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allow_all_paths: bool
    allowed_paths: tuple[str, ...]
    allowed_methods: tuple[str, ...]
    allowed_path_methods: tuple[tuple[str, tuple[str, ...]], ...]
    allowed_query_keys: tuple[tuple[str, tuple[str, ...]], ...]
    allow_all_query_keys: bool
    allowed_global_query_keys: tuple[str, ...]
    allowed_request_headers: tuple[str, ...]
    allow_all_request_headers: bool
    request_body_enabled: bool
    mutable_json_fields: tuple[tuple[str, tuple[str, ...]], ...]
    replayable_paths: tuple[str, ...]
    response_body_max_chars: int = Field(ge=0)

    @classmethod
    def from_profile(cls, profile: TargetProfile) -> OriginRestriction:
        return cls(
            allow_all_paths=profile.allow_all_paths,
            allowed_paths=_sorted_unique(profile.allowed_paths),
            allowed_methods=_sorted_unique(profile.allowed_methods),
            allowed_path_methods=_sorted_mapping(profile.allowed_path_methods),
            allowed_query_keys=_sorted_mapping(profile.allowed_query_keys),
            allow_all_query_keys=profile.allow_all_query_keys,
            allowed_global_query_keys=_sorted_unique(profile.allowed_global_query_keys),
            allowed_request_headers=_sorted_unique(profile.allowed_request_headers),
            allow_all_request_headers=profile.allow_all_request_headers,
            request_body_enabled=profile.request_body_enabled,
            mutable_json_fields=_sorted_mapping(profile.mutable_json_fields),
            replayable_paths=_sorted_unique(profile.replayable_paths),
            response_body_max_chars=profile.response_body_max_chars,
        )


class HttpRestrictedOrigin(BaseModel):
    """One verified operator origin whose HTTP restrictions raw routes cannot enforce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    scheme: Literal["http", "https"]
    restriction: OriginRestriction

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.host, self.port, self.scheme)


class HttpRestrictionProjection(BaseModel):
    """Verified applicability projection; its digest enters component identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sha256: str = Field(pattern=_SHA256)
    policy_sha256: str = Field(pattern=_SHA256)
    origins: tuple[HttpRestrictedOrigin, ...] = Field(min_length=1)
    verified: bool

    @model_validator(mode="after")
    def canonical_origins(self) -> HttpRestrictionProjection:
        keys = [origin.key for origin in self.origins]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError("http_restriction_origins_not_canonical")
        return self

    @property
    def digest(self) -> str:
        return digest_payload(self.model_dump(mode="json"))

    def matches(self, host: str, port: int) -> tuple[HttpRestrictedOrigin, ...]:
        """Origins whose host and effective port equal a raw route's host and port."""
        wanted = host.lower().rstrip(".")
        return tuple(
            origin for origin in self.origins
            if origin.host == wanted and origin.port == port
        )


def effective_port(scheme: str, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    return 443 if scheme == "https" else 80


def _scheme(value: str) -> Literal["http", "https"]:
    if value == "http":
        return "http"
    if value == "https":
        return "https"
    raise ValueError("http_restriction_origin_scheme_invalid")


def project_deployment(
    spec: DeploymentSpec, *, source_sha256: str, policy_sha256: str,
) -> HttpRestrictionProjection:
    """Project every verified host binding. Roles and request permission do not filter."""
    origins: list[HttpRestrictedOrigin] = []
    for host, ref in spec.host_bindings.items():
        profile = spec.policy.targets[ref]
        parts = urlsplit(profile.origin)
        origins.append(
            HttpRestrictedOrigin(
                host=host.lower().rstrip("."),
                port=effective_port(parts.scheme, parts.port),
                scheme=_scheme(parts.scheme),
                restriction=OriginRestriction.from_profile(profile),
            )
        )
    origins.sort(key=lambda origin: origin.key)
    return HttpRestrictionProjection(
        source_sha256=source_sha256,
        policy_sha256=policy_sha256,
        origins=tuple(origins),
        verified=True,
    )
