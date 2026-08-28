"""Engagement configuration loading and validation."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from whs_redteam.exceptions import ConfigurationError
from whs_redteam.models import Capability

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ALLOWED_ACTOR_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
KNOWN_CAPABILITIES = frozenset(capability.value for capability in Capability)
MANDATORY_APPROVAL_CAPABILITIES = frozenset(
    {
        "input_mutation", "sqli_safe", "xss_safe", "idor_safe", "csrf_safe",
        "ssrf_canary", "file_upload_benign", "path_traversal_canary",
        "business_logic", "poc_safe",
    }
)


class ActorConfig(BaseModel):
    """Reference authentication headers through environment variable names only."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    actor_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    header_env: dict[str, str]

    @field_validator("header_env")
    @classmethod
    def validate_header_env(cls, mapping: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for header, variable in mapping.items():
            normalized_header = header.lower()
            if normalized_header not in ALLOWED_ACTOR_HEADERS:
                raise ValueError(f"actor header is not allowed: {header}")
            if normalized_header in normalized:
                raise ValueError(f"duplicate actor header: {header}")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", variable):
                raise ValueError(f"invalid actor environment variable name: {variable}")
            normalized[normalized_header] = variable
        return normalized


class AuthorizationFixture(BaseModel):
    """Explicit, authorized owner/non-owner GET comparison fixture."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fixture_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
    approval_scope_url: str
    target_url: str
    negative_control_url: str
    owner_actor: str
    non_owner_actors: tuple[str, ...] = Field(min_length=1)

    @field_validator(
        "approval_scope_url", "target_url", "negative_control_url"
    )
    @classmethod
    def validate_fixture_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("authorization fixture URL must be an absolute HTTP(S) URL")
        return value

    @field_validator("approval_scope_url")
    @classmethod
    def validate_approval_scope_url(cls, value: str) -> str:
        if urlsplit(value).query:
            raise ValueError("approval_scope_url must not contain a query")
        return value.rstrip("/")

    @field_validator("non_owner_actors")
    @classmethod
    def validate_non_owner_actors(
        cls, actors: tuple[str, ...]
    ) -> tuple[str, ...]:
        if len(set(actors)) != len(actors):
            raise ValueError("non-owner actor identifiers must be unique")
        return actors


class InjectionFixture(BaseModel):
    """Explicit GET query parameter approved for bounded injection probes."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    fixture_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
    approval_scope_url: str
    target_url: str
    parameter: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\[\]-]{0,127}$")
    baseline_value: str = Field(min_length=1, max_length=128)
    parameter_kind: Literal["string", "integer"] = "string"
    checks: frozenset[Literal["sqli", "xss_reflection"]] = frozenset(
        {"sqli", "xss_reflection"}
    )
    actor_id: str | None = None
    source: Literal["explicit", "discovered_query_schema"] = "explicit"

    @field_validator("approval_scope_url", "target_url")
    @classmethod
    def validate_fixture_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("injection fixture URL must be an absolute HTTP(S) URL")
        return value

    @field_validator("approval_scope_url")
    @classmethod
    def validate_scope_url(cls, value: str) -> str:
        if urlsplit(value).query:
            raise ValueError("approval_scope_url must not contain a query")
        return value.rstrip("/")

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        if urlsplit(value).query:
            raise ValueError("injection target_url must not contain a query")
        return value

    @field_validator("baseline_value")
    @classmethod
    def validate_baseline_value(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("baseline_value must not contain control characters")
        return value

    @field_validator("checks")
    @classmethod
    def validate_checks(
        cls, checks: frozenset[str]
    ) -> frozenset[str]:
        if not checks:
            raise ValueError("injection fixture must enable at least one check")
        return checks

    @model_validator(mode="after")
    def validate_parameter_kind(self) -> Self:
        if self.parameter_kind == "integer" and not re.fullmatch(
            r"-?[0-9]+", self.baseline_value
        ):
            raise ValueError("integer injection fixture requires an integer baseline_value")
        return self


class AIConfig(BaseModel):
    """Optional OpenAI-compatible model settings; secrets stay in the environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["none", "openai_compatible"] = "none"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = Field(default="", max_length=200)
    api_key_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
    )
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_output_tokens: int = Field(default=1200, ge=128, le=8_192)
    max_api_calls_per_run: int = Field(default=4, ge=1, le=20)
    retry_attempts: int = Field(default=2, ge=0, le=4)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    max_input_characters: int = Field(default=40_000, ge=1_000, le=200_000)
    max_planning_rounds: int = Field(default=3, ge=1, le=5)
    fallback_to_rules: bool = True
    report_writer_enabled: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AI base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("AI base_url must not contain credentials, query, or fragment")
        return value.rstrip("/")


class EngagementConfig(BaseModel):
    """Safety boundaries and budgets for one authorized engagement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    targets: tuple[str, ...] = Field(min_length=1)
    excluded_paths: tuple[str, ...] = ()
    allowed_methods: frozenset[str] = SAFE_METHODS
    max_requests: int = Field(default=300, ge=1, le=10_000)
    requests_per_second: int = Field(default=2, ge=1, le=20)
    max_runtime_minutes: float = Field(default=15, gt=0, le=240)
    max_pages: int = Field(default=100, ge=1, le=5_000)
    max_depth: int = Field(default=3, ge=0, le=10)
    test_level: str = "safe_active"
    allow_state_change: bool = False
    audit_log_path: Path = Path("outputs/events.jsonl")
    inventory_db_path: Path = Path("outputs/inventory.db")
    screenshots_dir: Path = Path("outputs/screenshots")
    capture_screenshots: bool = False
    browser_headless: bool = True
    discovery_mode: Literal["browser", "http"] = "browser"
    openapi_spec_path: Path | None = None
    follow_discovered_links: bool = False
    allow_private_dns: bool = False
    risky_path_keywords: tuple[str, ...] = (
        "delete",
        "remove",
        "logout",
        "signout",
        "approve",
        "invite",
        "send",
        "payment",
        "purchase",
        "unsubscribe",
    )
    actors: tuple[ActorConfig, ...] = ()
    authorization_fixtures: tuple[AuthorizationFixture, ...] = ()
    injection_fixtures: tuple[InjectionFixture, ...] = ()
    auto_injection_discovery: bool = False
    auto_injection_checks: frozenset[Literal["sqli", "xss_reflection"]] = frozenset(
        {"sqli", "xss_reflection"}
    )
    max_auto_injection_fixtures: int = Field(default=20, ge=1, le=100)
    auto_injection_actor_id: str | None = None
    xss_browser_confirmation: bool = False
    planner_mode: Literal["rules", "hybrid", "llm"] = "rules"
    ai: AIConfig = Field(default_factory=AIConfig)
    tool_backend: Literal["local", "mcp_in_memory"] = "local"
    validation_repetitions: int = Field(default=2, ge=2, le=3)
    enabled_capabilities: frozenset[str] = frozenset(
        {
            "discovery",
            "api_schema",
            "auth_session",
            "resource_model",
            "finding_validation",
        }
    )
    approval_required_capabilities: frozenset[str] = frozenset(
        MANDATORY_APPROVAL_CAPABILITIES
    )
    max_mutations_per_endpoint: int = Field(default=10, ge=1, le=100)
    approval_hmac_secret_env: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]{1,127}$",
    )

    @field_validator("actors")
    @classmethod
    def validate_unique_actors(cls, actors: tuple[ActorConfig, ...]) -> tuple[ActorConfig, ...]:
        identifiers = [actor.actor_id for actor in actors]
        if any(identifier.lower() == "anonymous" for identifier in identifiers):
            raise ValueError("actor identifier 'anonymous' is reserved")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("actor identifiers must be unique")
        return actors

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, targets: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for target in targets:
            parsed = urlsplit(target)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"target must be an absolute HTTP(S) URL: {target}")
            if parsed.username or parsed.password:
                raise ValueError("credentials must not be embedded in target URLs")
            if parsed.query or parsed.fragment:
                raise ValueError("target URLs must not contain query strings or fragments")
            hostname = parsed.hostname
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                hostname_pattern = (
                    r"(?=.{1,253}$)"
                    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                )
                if not re.fullmatch(hostname_pattern, hostname):
                    raise ValueError(
                        f"target contains an invalid hostname: {hostname}"
                    ) from None
            normalized.append(target.rstrip("/"))
        if len(set(normalized)) != len(normalized):
            raise ValueError("targets must be unique")
        return tuple(normalized)

    @field_validator("excluded_paths")
    @classmethod
    def validate_excluded_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            if not path.startswith("/"):
                raise ValueError(f"excluded path must start with '/': {path}")
        return paths

    @field_validator("allowed_methods")
    @classmethod
    def normalize_methods(cls, methods: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(method.upper() for method in methods)
        if not normalized:
            raise ValueError("at least one HTTP method must be allowed")
        return normalized

    @field_validator("enabled_capabilities", "approval_required_capabilities")
    @classmethod
    def validate_capabilities(cls, capabilities: frozenset[str]) -> frozenset[str]:
        unknown = capabilities - KNOWN_CAPABILITIES
        if unknown:
            raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")
        return capabilities

    @model_validator(mode="after")
    def enforce_state_change_policy(self) -> Self:
        unsafe = self.allowed_methods - SAFE_METHODS
        if unsafe and not self.allow_state_change:
            methods = ", ".join(sorted(unsafe))
            raise ValueError(f"state-changing methods require allow_state_change=true: {methods}")
        actor_ids = {actor.actor_id for actor in self.actors}
        references: dict[str, str] = {}
        for actor in self.actors:
            for variable in actor.header_env.values():
                previous = references.get(variable)
                if previous is not None and previous != actor.actor_id:
                    raise ValueError(
                        "actors must not reference the same credential environment "
                        f"variable: {previous}, {actor.actor_id}"
                    )
                references[variable] = actor.actor_id
        fixture_ids = [
            fixture.fixture_id
            for fixture in (*self.authorization_fixtures, *self.injection_fixtures)
        ]
        if len(set(fixture_ids)) != len(fixture_ids):
            raise ValueError("fixture identifiers must be globally unique")
        for fixture in self.authorization_fixtures:
            fixture_actors = {fixture.owner_actor, *fixture.non_owner_actors}
            unknown = fixture_actors - actor_ids
            if unknown:
                raise ValueError(
                    "authorization fixture references unknown actors: "
                    + ", ".join(sorted(unknown))
                )
            if fixture.owner_actor in fixture.non_owner_actors:
                raise ValueError("fixture owner cannot also be a non-owner")
            for url in (
                fixture.approval_scope_url,
                fixture.target_url,
                fixture.negative_control_url,
            ):
                if not any(self._fixture_url_in_target(url, target) for target in self.targets):
                    raise ValueError("authorization fixture URL is outside configured targets")
            for url in (fixture.target_url, fixture.negative_control_url):
                if not self._fixture_url_in_target(
                    url, fixture.approval_scope_url
                ):
                    raise ValueError(
                        "fixture requests must be within approval_scope_url"
                    )
        if self.authorization_fixtures:
            if Capability.IDOR_SAFE.value not in self.enabled_capabilities:
                raise ValueError("authorization fixtures require idor_safe capability")
            if self.approval_hmac_secret_env is None:
                raise ValueError(
                    "authorization fixtures require approval_hmac_secret_env"
                )
            if self.tool_backend != "local":
                raise ValueError(
                    "authorization fixtures currently require the local tool backend"
                )
        for fixture in self.injection_fixtures:
            if fixture.actor_id is not None and fixture.actor_id not in actor_ids:
                raise ValueError(
                    f"injection fixture references unknown actor: {fixture.actor_id}"
                )
            for url in (fixture.approval_scope_url, fixture.target_url):
                if not any(self._fixture_url_in_target(url, target) for target in self.targets):
                    raise ValueError("injection fixture URL is outside configured targets")
            if not self._fixture_url_in_target(
                fixture.target_url, fixture.approval_scope_url
            ):
                raise ValueError(
                    "injection target_url must be within approval_scope_url"
                )
            required = {
                "sqli": Capability.SQLI_SAFE.value,
                "xss_reflection": Capability.XSS_SAFE.value,
            }
            missing = {
                capability
                for check, capability in required.items()
                if check in fixture.checks and capability not in self.enabled_capabilities
            }
            if missing:
                raise ValueError(
                    "injection fixtures require enabled capabilities: "
                    + ", ".join(sorted(missing))
                )
        if self.injection_fixtures:
            if self.approval_hmac_secret_env is None:
                raise ValueError(
                    "injection fixtures require approval_hmac_secret_env"
                )
            if self.tool_backend != "local":
                raise ValueError(
                    "injection fixtures currently require the local tool backend"
                )
        if self.auto_injection_discovery:
            if not self.auto_injection_checks:
                raise ValueError("automatic injection discovery requires at least one check")
            if self.auto_injection_actor_id is not None:
                if self.auto_injection_actor_id not in actor_ids:
                    raise ValueError(
                        "automatic injection discovery references an unknown actor"
                    )
            required = {
                "sqli": Capability.SQLI_SAFE.value,
                "xss_reflection": Capability.XSS_SAFE.value,
            }
            missing = {
                capability
                for check, capability in required.items()
                if check in self.auto_injection_checks
                and capability not in self.enabled_capabilities
            }
            if missing:
                raise ValueError(
                    "automatic injection discovery requires enabled capabilities: "
                    + ", ".join(sorted(missing))
                )
            if self.approval_hmac_secret_env is None:
                raise ValueError(
                    "automatic injection discovery requires approval_hmac_secret_env"
                )
            if self.tool_backend != "local":
                raise ValueError(
                    "automatic injection discovery currently requires the local backend"
                )
        if self.xss_browser_confirmation:
            if Capability.XSS_SAFE.value not in self.enabled_capabilities:
                raise ValueError(
                    "XSS browser confirmation requires the xss_safe capability"
                )
            if self.tool_backend != "local":
                raise ValueError(
                    "XSS browser confirmation currently requires the local backend"
                )
        if self.planner_mode in {"hybrid", "llm"}:
            if Capability.LLM_PLANNING.value not in self.enabled_capabilities:
                raise ValueError(
                    "hybrid or llm planner mode requires llm_planning capability"
                )
            if self.planner_mode == "llm" and self.ai.provider == "none":
                raise ValueError("llm planner mode requires an AI provider")
        return self

    @staticmethod
    def _fixture_url_in_target(url: str, target: str) -> bool:
        candidate = urlsplit(url)
        root = urlsplit(target)
        candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
        root_port = root.port or (443 if root.scheme == "https" else 80)
        root_path = (root.path or "/").rstrip("/") + "/"
        candidate_path = (candidate.path or "/").rstrip("/") + "/"
        return (
            candidate.scheme.lower(),
            (candidate.hostname or "").lower(),
            candidate_port,
        ) == (
            root.scheme.lower(),
            (root.hostname or "").lower(),
            root_port,
        ) and candidate_path.startswith(root_path)


class ProjectConfig(BaseModel):
    """Root configuration object."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    engagement: EngagementConfig


def load_config(path: str | Path) -> ProjectConfig:
    """Load and validate a YAML engagement file."""

    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"cannot read config: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"invalid YAML: {config_path}") from exc
    if raw is None:
        raise ConfigurationError(f"config is empty: {config_path}")
    try:
        return ProjectConfig.model_validate(raw)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
