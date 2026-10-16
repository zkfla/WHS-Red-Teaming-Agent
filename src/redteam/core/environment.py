"""Low common environment input boundary (plan 4.4 / R07).

Collects raw environment material with SDK env sources.
Exact uppercase names only; case_sensitive stays True so a new
lowercase variant is never quietly accepted. Explicit snapshots never
touch the live process env. Dynamic prefix reads intentionally stay a
same-snapshot manual filter so static and dynamic checks see one input
without a new generic resolver framework.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypeVar

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict

DEFAULT_ENV_PATH = Path(".env")
_SESSION_ENV: ContextVar[Mapping[str, str] | None] = ContextVar("redteam_environment", default=None)

__all__ = [
    "DEFAULT_ENV_PATH",
    "LLMEnvInput",
    "OrchestrationEnvInput",
    "MethodologyEnvInput",
    "AppSelectionEnvInput",
    "MCPListEnvInput",
    "env_snapshot",
    "environment_context",
    "env_names_for",
    "fill_missing_from_dotenv",
    "load_dotenv_if_present",
    "llm_env_values",
    "orchestration_env_values",
    "methodology_env_raw",
    "extensions_env_raw",
    "planning_provider_env_raw",
    "app_selection_env_values",
    "mcp_server_list",
    "read_prefixed_values",
]


class _SnapshotSettings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # The SDK source below has already collected this request's input.
        # An explicit empty snapshot must never be supplemented from live env.
        return (init_settings,)


class LLMEnvInput(_SnapshotSettings):
    """Raw model-connection input. Exact uppercase names only."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    provider: str | None = Field(default=None, validation_alias="REDTEAM_LLM_PROVIDER")
    model: str | None = Field(default=None, validation_alias="REDTEAM_LLM_MODEL")
    api_key_env_var: str | None = Field(
        default=None, validation_alias="REDTEAM_LLM_API_KEY_ENV_VAR"
    )
    reasoning_effort: str | None = Field(
        default=None, validation_alias="REDTEAM_LLM_REASONING_EFFORT"
    )


class OrchestrationEnvInput(_SnapshotSettings):
    """Raw run-limit input. Strings only; the run-limit shape validates."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    max_steps: str | None = Field(default=None, validation_alias="REDTEAM_ORCHESTRATION_MAX_STEPS")
    max_retries: str | None = Field(
        default=None, validation_alias="REDTEAM_ORCHESTRATION_MAX_RETRIES"
    )
    resource_wait_timeout_seconds: str | None = Field(
        default=None, validation_alias="REDTEAM_RESOURCE_WAIT_TIMEOUT_SECONDS"
    )
    decision_normal_limit: str | None = Field(
        default=None, validation_alias="REDTEAM_DECISION_NORMAL_LIMIT"
    )
    decision_max_attempts: str | None = Field(
        default=None, validation_alias="REDTEAM_DECISION_MAX_ATTEMPTS"
    )
    decision_transport_max_attempts: str | None = Field(
        default=None, validation_alias="REDTEAM_DECISION_TRANSPORT_MAX_ATTEMPTS"
    )
    planner_replan_max_passes: str | None = Field(
        default=None, validation_alias="REDTEAM_PLANNER_REPLAN_MAX_PASSES"
    )
    decision_timeout_seconds: str | None = Field(
        default=None, validation_alias="REDTEAM_DECISION_TIMEOUT_SECONDS"
    )
    llm_attempt_timeout_seconds: str | None = Field(
        default=None, validation_alias="REDTEAM_LLM_ATTEMPT_TIMEOUT_SECONDS"
    )
    llm_candidate_generation: str | None = Field(
        default=None, validation_alias="REDTEAM_ORCHESTRATION_LLM_CANDIDATE_GENERATION"
    )
    llm_request_policy: str | None = Field(
        default=None, validation_alias="REDTEAM_LLM_REQUEST_POLICY_JSON"
    )
    specialist_profile_path: str | None = Field(
        default=None, validation_alias="REDTEAM_SPECIALIST_PROFILE"
    )
    runtime_backend: str | None = Field(default=None, validation_alias="REDTEAM_RUNTIME_BACKEND")
    central_state_path: str | None = Field(default=None, validation_alias="REDTEAM_STATE_DB")
    max_workers: str | None = Field(default=None, validation_alias="REDTEAM_MAX_WORKERS")
    global_concurrency: str | None = Field(
        default=None, validation_alias="REDTEAM_GLOBAL_CONCURRENCY"
    )
    per_target_concurrency: str | None = Field(
        default=None, validation_alias="REDTEAM_PER_TARGET_CONCURRENCY"
    )
    code_fixture_policy_path: str | None = Field(
        default=None, validation_alias="REDTEAM_CODE_FIXTURE_POLICY_PATH"
    )


class MethodologyEnvInput(_SnapshotSettings):
    """Raw operator-reference input. Single JSON blob as text."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    methodology_config_json: str | None = Field(
        default=None, validation_alias="REDTEAM_METHODOLOGY_CONFIG_JSON"
    )


class AppSelectionEnvInput(_SnapshotSettings):
    """Raw assembly-choice input. Explicit entrypoint names plus
    the optional inference-role provider name. No registry lookups.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    extensions: str | None = Field(default=None, validation_alias="REDTEAM_EXTENSIONS")
    planning_provider: str | None = Field(
        default=None, validation_alias="REDTEAM_PLANNING_PROVIDER"
    )


class MCPListEnvInput(_SnapshotSettings):
    """Raw server-list input. Comma-separated ids as one string."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    servers: str | None = Field(default=None, validation_alias="REDTEAM_MCP_SERVERS")


def env_snapshot(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """One input snapshot. Explicit mapping wins; otherwise the live process env."""
    if source is not None:
        return dict(source)
    selected = _SESSION_ENV.get()
    if selected is not None:
        return dict(selected)
    return dict(os.environ)


@contextmanager
def environment_context(source: Mapping[str, str]):
    """One session's validated assembly inputs, without changing process/.env.

    Tasks inherit the snapshot. Explicit reader arguments still take precedence;
    leaving this context restores the caller's original snapshot.
    """
    token = _SESSION_ENV.set(dict(source))
    try:
        yield
    finally:
        _SESSION_ENV.reset(token)


_SettingsT = TypeVar("_SettingsT", bound=BaseSettings)


def _env_name_for_field(settings_cls: type[BaseSettings], field_name: str) -> str:
    alias = settings_cls.model_fields[field_name].validation_alias
    if isinstance(alias, str):
        return alias
    prefix = str(settings_cls.model_config.get("env_prefix", ""))
    return f"{prefix}{field_name.upper()}"


def env_names_for(settings_cls: type[BaseSettings]) -> tuple[str, ...]:
    """Derive watched env names from aliases, no hardcoded list."""
    return tuple(_env_name_for_field(settings_cls, name) for name in settings_cls.model_fields)


def _collect_raw(
    settings_cls: type[_SettingsT], source: Mapping[str, str] | None
) -> dict[str, Any]:
    snapshot = env_snapshot(source)

    class SnapshotEnvSource(EnvSettingsSource):
        def _load_env_vars(self) -> Mapping[str, str | None]:
            return snapshot

    collector = SnapshotEnvSource(settings_cls, case_sensitive=True)
    return settings_cls(**collector()).model_dump()


def fill_missing_from_dotenv(
    env_path: str | Path = DEFAULT_ENV_PATH,
    *,
    target: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Fill only missing keys from a dotenv file. Live env always wins.

    SDK parser with literal values (no interpolation). Malformed lines
    are skipped by the parser. Missing or unreadable file is a silent
    no-op. Bare export prefix is stripped by the SDK, unlike the old
    hand parser which kept it as part of the key; harm is tiny and noted.
    """
    tgt: MutableMapping[str, str] = os.environ if target is None else target
    path = Path(env_path)
    try:
        if not path.exists():
            return {}
        parsed = dotenv_values(path, interpolate=False)
    except OSError:
        return {}
    if not parsed:
        return {}
    filled: dict[str, str] = {}
    for key, value in parsed.items():
        if not key or value is None:
            continue
        if key in tgt:
            continue
        tgt[key] = value
        filled[key] = value
    return filled


def load_dotenv_if_present(env_path: str | Path = DEFAULT_ENV_PATH) -> None:
    """Compat entrypoint. Same missing-only fill into the live process env."""
    fill_missing_from_dotenv(env_path, target=os.environ)


def llm_env_values(source: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Thin raw values for the model-connection shape. Same input, same result."""
    raw = _collect_raw(LLMEnvInput, source)
    return {
        "provider": raw["provider"],
        "model": raw["model"],
        "api_key_env_var": raw["api_key_env_var"],
        "reasoning_effort": raw["reasoning_effort"],
    }


def orchestration_env_values(source: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Thin raw values for the run-limit shape. Only set keys are returned."""
    raw = _collect_raw(OrchestrationEnvInput, source)
    if raw.get("max_retries") is not None:
        # Removed setting: reject new input explicitly instead of silently
        # ignoring it. Original saved Runs still decode this key passively.
        raise ValueError(
            "REDTEAM_ORCHESTRATION_MAX_RETRIES was removed: central execution "
            "never auto-retries a tool call, so this value selects nothing. A "
            "retryable failure is returned to the model instead."
        )
    return {key: value for key, value in raw.items() if value is not None}


def methodology_env_raw(source: Mapping[str, str] | None = None) -> str | None:
    """Raw operator-reference blob. None means absent, same as before."""
    return _collect_raw(MethodologyEnvInput, source)["methodology_config_json"]


def extensions_env_raw(source: Mapping[str, str] | None = None) -> str | None:
    """Raw assembly-choice blob. JSON list text; None means no explicit choice."""
    return _collect_raw(AppSelectionEnvInput, source)["extensions"]


def planning_provider_env_raw(source: Mapping[str, str] | None = None) -> str | None:
    """Raw inference-role provider name. None means no explicit choice."""
    return _collect_raw(AppSelectionEnvInput, source)["planning_provider"]


def app_selection_env_values(
    source: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Both assembly-choice raws together for callers that need one snapshot."""
    raw = _collect_raw(AppSelectionEnvInput, source)
    return {"extensions": raw["extensions"], "planning_provider": raw["planning_provider"]}


def mcp_server_list(
    source: Mapping[str, str] | None = None,
    env_var: str = "REDTEAM_MCP_SERVERS",
) -> list[str]:
    """Comma-separated server ids. Unset or blank means no server."""
    if env_var == "REDTEAM_MCP_SERVERS":
        raw = _collect_raw(MCPListEnvInput, source)["servers"]
    elif source is None:
        raw = os.environ.get(env_var, "")
    else:
        raw = source.get(env_var)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def read_prefixed_values(prefix: str, *, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Dynamic prefix read off the same snapshot source.

    Full KEY -> value for keys starting with PREFIX + "_". Used for
    per-server connection details and profile checks so dynamic reads
    see exactly what the static collectors saw.
    """
    snap = env_snapshot(source)
    needle = f"{prefix}_"
    return {key: value for key, value in snap.items() if key.startswith(needle)}
