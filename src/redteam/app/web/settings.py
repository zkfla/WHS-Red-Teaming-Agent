"""Web-editable runtime settings (plan3 W10).

Reuses the existing environment readers (``LLMConfig.from_environment``,
``OrchestrationConfig.from_environment``) as the single validation path. The
API key is write-only: it lives in the process environment, is never returned,
logged or saved to the web settings file, and reaches ``.env`` only when the
operator explicitly asks for it. Non-secret choices are kept in a separate
state file and are read back only for keys the environment did not set.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import yaml

from redteam.app.cli._shared import DEFAULT_ENV_PATH, env_file_is_gitignored, write_env_var
from redteam.core.config import (
    CYBER_ACCESS_PROGRAMS,
    DEFAULT_SPECIALIST_PROFILE_PATH,
    REASONING_EFFORTS,
    LLMConfig,
    OrchestrationConfig,
    normalize_cyber_access_program,
    normalize_reasoning_effort,
)
from redteam.core.environment import OrchestrationEnvInput
from redteam.llm.opencode_go_config import (
    OPENCODE_GO_API_KEY_ENV_VAR,
    is_go_provider,
)
from redteam.llm.opencode_go_config import (
    validate_model as _validate_go_model,
)

PROVIDER = "litellm"
SUPPORTED_PROVIDERS = frozenset({"litellm", "opencode-go"})
PROFILE_DIR = Path(DEFAULT_SPECIALIST_PROFILE_PATH).parent
STATE_PATH = Path("data/web/settings.json")

ENV_PROVIDER = "REDTEAM_LLM_PROVIDER"
ENV_MODEL = "REDTEAM_LLM_MODEL"
ENV_EFFORT = "REDTEAM_LLM_REASONING_EFFORT"
ENV_CYBER = "REDTEAM_LLM_CYBER_ACCESS_PROGRAM"
ENV_KEY_VAR = "REDTEAM_LLM_API_KEY_ENV_VAR"
ENV_PROFILE = "REDTEAM_SPECIALIST_PROFILE"
ENV_POLICY = "REDTEAM_LLM_REQUEST_POLICY_JSON"
DEFAULT_KEY_VAR = "LLM_API_KEY"
DEFAULT_PROFILE_ID = Path(DEFAULT_SPECIALIST_PROFILE_PATH).stem
REMOVED_MAX_RETRIES_ENV = str(
    OrchestrationEnvInput.model_fields["max_retries"].validation_alias
)

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_KEY = re.compile(r"^[^\s\x00-\x1f\x7f]{1,4096}$")
_KEY_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ALLOWED = frozenset(
    {
        "model",
        "api_key",
        "persist_key",
        "profile_id",
        "max_requests",
        "max_concurrent_requests",
        "provider",
        "reasoning_effort",
        "cyber_access_program",
    }
)
_REQUEST_LIMIT = (1, 1_000_000)
_CONCURRENT_LIMIT = (1, 64)


class SettingsError(ValueError):
    """Stable ``field``/``code`` pair; never carries the offending value."""

    def __init__(self, field: str, code: str) -> None:
        super().__init__(f"{field}:{code}")
        self.field = field
        self.code = code


def _integer(value: Any, field: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(field, "invalid_type")
    if not bounds[0] <= value <= bounds[1]:
        raise SettingsError(field, "out_of_range")
    return value


def _key_var_for(env: Any) -> str:
    if not hasattr(env, "get"):
        return DEFAULT_KEY_VAR
    name = env.get(ENV_KEY_VAR)
    if isinstance(name, str) and _KEY_VAR.match(name):
        return name
    provider = env.get(ENV_PROVIDER)
    if is_go_provider(provider if isinstance(provider, str) else None):
        return OPENCODE_GO_API_KEY_ENV_VAR
    return DEFAULT_KEY_VAR


class SettingsStore:
    def __init__(
        self,
        *,
        env: MutableMapping[str, str] | None = None,
        state_path: Path = STATE_PATH,
        profile_dir: Path = PROFILE_DIR,
        env_path: Path = DEFAULT_ENV_PATH,
    ) -> None:
        self.env: MutableMapping[str, str] = os.environ if env is None else env
        self.state_path = state_path
        self.profile_dir = profile_dir
        self.env_path = env_path
        self._key_var_explicit = ENV_KEY_VAR in self.env
        if is_go_provider(self.env.get(ENV_PROVIDER)) and not self._key_var_explicit:
            # Publish the same derived reference to the runtime consumer, even
            # on first startup before any web settings file has been written.
            self.env[ENV_KEY_VAR] = OPENCODE_GO_API_KEY_ENV_VAR

    # -- profiles -------------------------------------------------------

    def profiles(self) -> list[dict[str, str]]:
        """Selectable profile files. Only ids leave this module, never paths."""
        listing: list[dict[str, str]] = []
        try:
            files = sorted(self.profile_dir.glob("*.yaml"))
        except OSError:
            return listing
        for path in files:
            label: str = path.stem
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                profile_id = loaded.get("profile_id") if isinstance(loaded, dict) else None
                if isinstance(profile_id, str) and profile_id != path.stem:
                    label = profile_id[:80] + " (" + path.stem + ")"
                elif isinstance(profile_id, str):
                    label = profile_id[:80]
            except (OSError, yaml.YAMLError):
                continue
            listing.append({"id": path.stem, "label": label})
        return listing

    def _profile_path(self, profile_id: str) -> str | None:
        if profile_id not in {item["id"] for item in self.profiles()}:
            return None
        return str(self.profile_dir / f"{profile_id}.yaml")

    def _selected_profile_id(self) -> str | None:
        raw = self.env.get(ENV_PROFILE)
        if not raw:
            return (
                DEFAULT_PROFILE_ID
                if self._profile_path(DEFAULT_PROFILE_ID) is not None
                else None
            )
        for item in self.profiles():
            if self._profile_path(item["id"]) == raw:
                return item["id"]
        return "custom"

    # -- reading --------------------------------------------------------

    def _policy(self) -> dict[str, Any] | None:
        raw = self.env.get(ENV_POLICY)
        if not raw:
            return None
        try:
            loaded = json.loads(raw)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None

    def _key_var(self) -> str:
        return _key_var_for(self.env)

    def _effort(self) -> str | None:
        raw = self.env.get(ENV_EFFORT)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return normalize_reasoning_effort(raw)
        except ValueError:
            return None

    def _effort_valid(self) -> bool:
        raw = self.env.get(ENV_EFFORT)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return True
        try:
            normalize_reasoning_effort(raw)
        except ValueError:
            return False
        return True

    def _cyber_program(self) -> str | None:
        raw = self.env.get(ENV_CYBER)
        if raw is None:
            return None
        try:
            return normalize_cyber_access_program(raw)
        except ValueError:
            return None

    def _cyber_program_valid(self) -> bool:
        raw = self.env.get(ENV_CYBER)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return True
        try:
            normalize_cyber_access_program(raw)
        except ValueError:
            return False
        return True

    def problems(self) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        provider = self.env.get(ENV_PROVIDER)
        if not provider:
            found.append({"field": "provider", "code": "missing"})
        elif provider not in SUPPORTED_PROVIDERS:
            found.append({"field": "provider", "code": "unsupported"})
        model = self.env.get(ENV_MODEL)
        if not model:
            found.append({"field": "model", "code": "missing"})
        elif is_go_provider(provider) and isinstance(model, str):
            try:
                _validate_go_model(model)
            except ValueError:
                found.append({"field": "model", "code": "invalid"})
        if not self._effort_valid():
            found.append({"field": "reasoning_effort", "code": "invalid"})
        if not self._cyber_program_valid():
            found.append({"field": "cyber_access_program", "code": "invalid"})
        elif self._cyber_program() is not None and provider != "litellm":
            found.append({"field": "cyber_access_program", "code": "unsupported"})
        if not (model or "").startswith("chatgpt/") and not self.env.get(self._key_var()):
            found.append({"field": "api_key", "code": "missing"})
        if self._selected_profile_id() is None:
            found.append({"field": "profile_id", "code": "missing"})
        if not self.env.get(ENV_POLICY):
            found.append({"field": "llm_request_policy", "code": "missing"})
        if self.env.get(REMOVED_MAX_RETRIES_ENV):
            found.append({"field": "max_retries", "code": "removed"})
        if not found:
            try:
                LLMConfig.from_environment(self.env)
                OrchestrationConfig.from_environment(self.env)
            except Exception:  # noqa: BLE001 - stable code only, never the raw message
                found.append({"field": "settings", "code": "invalid"})
        return found

    def summary(self) -> dict[str, Any]:
        policy = self._policy() or {}
        problems = self.problems()
        return {
            "model": self.env.get(ENV_MODEL) or None,
            "provider": self.env.get(ENV_PROVIDER) or None,
            "reasoning_effort": self._effort(),
            "reasoning_effort_options": sorted(REASONING_EFFORTS),
            "cyber_access_program": self._cyber_program(),
            "cyber_access_program_options": sorted(CYBER_ACCESS_PROGRAMS),
            "api_key_set": bool(self.env.get(self._key_var())),
            "api_key_env_var": self._key_var(),
            "profile_id": self._selected_profile_id(),
            "max_requests": policy.get("max_requests"),
            "max_concurrent_requests": policy.get("max_concurrent_requests"),
            "problems": problems,
            "ready": not problems,
        }

    # -- saved non-secret choices ---------------------------------------

    def load_saved(self) -> None:
        """Fill only keys the environment left unset from the web state file."""
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        key_var = saved.get("api_key_env_var")
        if isinstance(key_var, str) and _KEY_VAR.match(key_var):
            if ENV_KEY_VAR not in self.env:
                self.env[ENV_KEY_VAR] = key_var
                self._key_var_explicit = True
        provider = saved.get("provider")
        if isinstance(provider, str) and provider in SUPPORTED_PROVIDERS:
            self.env.setdefault(ENV_PROVIDER, provider)
        model = saved.get("model")
        if isinstance(model, str) and _MODEL.match(model):
            self.env.setdefault(ENV_MODEL, model)
            if ENV_PROVIDER not in self.env:
                self.env.setdefault(ENV_PROVIDER, PROVIDER)
        current_provider = self.env.get(ENV_PROVIDER)
        if ENV_KEY_VAR not in self.env and is_go_provider(
            current_provider if isinstance(current_provider, str) else None
        ):
            self.env.setdefault(ENV_KEY_VAR, OPENCODE_GO_API_KEY_ENV_VAR)
        effort = saved.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip():
            try:
                normalized = normalize_reasoning_effort(effort)
            except ValueError:
                normalized = None
            if normalized is not None:
                self.env.setdefault(ENV_EFFORT, normalized)
        cyber_program = saved.get("cyber_access_program")
        if isinstance(cyber_program, str) and cyber_program.strip():
            try:
                normalized_cyber = normalize_cyber_access_program(cyber_program)
            except ValueError:
                normalized_cyber = None
            if normalized_cyber is not None:
                self.env.setdefault(ENV_CYBER, normalized_cyber)
        profile = saved.get("profile_id")
        if isinstance(profile, str):
            path = self._profile_path(profile)
            if path is not None:
                self.env.setdefault(ENV_PROFILE, path)
        requests, concurrent = saved.get("max_requests"), saved.get("max_concurrent_requests")
        if (
            isinstance(requests, int) and not isinstance(requests, bool)
            and isinstance(concurrent, int) and not isinstance(concurrent, bool)
            and _REQUEST_LIMIT[0] <= requests <= _REQUEST_LIMIT[1]
            and _CONCURRENT_LIMIT[0] <= concurrent <= _CONCURRENT_LIMIT[1]
        ):
            self.env.setdefault(ENV_POLICY, self._policy_json(requests, concurrent))

    def _save(self, values: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2)
            os.chmod(name, 0o600)
            os.replace(name, self.state_path)
        except BaseException:
            _discard(name)
            raise

    @staticmethod
    def _policy_json(max_requests: int, max_concurrent: int) -> str:
        return json.dumps(
            {
                "mode": "bounded",
                "max_requests": max_requests,
                "max_concurrent_requests": max_concurrent,
            },
            separators=(",", ":"),
        )

    # -- writing --------------------------------------------------------

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate on a candidate environment, then commit. Returns flags only."""
        if not isinstance(payload, dict):
            raise SettingsError("body", "invalid_type")
        for name in payload:
            if name not in _ALLOWED:
                raise SettingsError("body", "unknown_field")
        updates: dict[str, str] = {}
        saved: dict[str, Any] = {}
        removals: list[str] = []
        saved_removals: list[str] = []
        current = self._policy() or {}

        if "provider" in payload:
            provider = payload["provider"]
            if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
                raise SettingsError("provider", "unsupported")
            updates[ENV_PROVIDER] = provider
            saved["provider"] = provider
        if "reasoning_effort" in payload:
            raw_effort = payload["reasoning_effort"]
            if raw_effort is None or (isinstance(raw_effort, str) and not raw_effort.strip()):
                removals.append(ENV_EFFORT)
                saved_removals.append("reasoning_effort")
            elif isinstance(raw_effort, str):
                try:
                    normalized = normalize_reasoning_effort(raw_effort)
                except ValueError:
                    raise SettingsError("reasoning_effort", "invalid") from None
                if normalized is None:
                    removals.append(ENV_EFFORT)
                    saved_removals.append("reasoning_effort")
                else:
                    updates[ENV_EFFORT] = normalized
                    saved["reasoning_effort"] = normalized
            else:
                raise SettingsError("reasoning_effort", "invalid")
        if "cyber_access_program" in payload:
            raw_cyber = payload["cyber_access_program"]
            if raw_cyber is None or (isinstance(raw_cyber, str) and not raw_cyber.strip()):
                removals.append(ENV_CYBER)
                saved_removals.append("cyber_access_program")
            elif isinstance(raw_cyber, str):
                try:
                    normalized_cyber = normalize_cyber_access_program(raw_cyber)
                except ValueError:
                    raise SettingsError("cyber_access_program", "invalid") from None
                if normalized_cyber is None:
                    removals.append(ENV_CYBER)
                    saved_removals.append("cyber_access_program")
                else:
                    updates[ENV_CYBER] = normalized_cyber
                    saved["cyber_access_program"] = normalized_cyber
            else:
                raise SettingsError("cyber_access_program", "invalid")
        if "model" in payload:
            model = payload["model"]
            if not isinstance(model, str) or not _MODEL.match(model):
                raise SettingsError("model", "invalid")
            updates[ENV_MODEL] = model
            saved["model"] = model
            if "provider" not in payload and not self.env.get(ENV_PROVIDER):
                updates[ENV_PROVIDER] = PROVIDER
                saved["provider"] = PROVIDER
        candidate_provider_name = updates.get(ENV_PROVIDER, self.env.get(ENV_PROVIDER))
        old_ref = self.env.get(ENV_KEY_VAR)
        prior_ref: str | None = self._read_saved().get("api_key_env_var")
        if not (isinstance(prior_ref, str) and _KEY_VAR.match(prior_ref)):
            prior_ref = None
        if (
            isinstance(old_ref, str)
            and _KEY_VAR.match(old_ref)
            and (self._key_var_explicit or old_ref == prior_ref)
        ):
            explicit_ref: str | None = old_ref
        else:
            explicit_ref = prior_ref
        if explicit_ref is not None:
            if old_ref != explicit_ref:
                updates[ENV_KEY_VAR] = explicit_ref
            saved["api_key_env_var"] = explicit_ref
        else:
            new_derived = (
                OPENCODE_GO_API_KEY_ENV_VAR
                if is_go_provider(
                    candidate_provider_name
                    if isinstance(candidate_provider_name, str)
                    else None
                )
                else DEFAULT_KEY_VAR
            )
            updates[ENV_KEY_VAR] = new_derived
            saved_removals.append("api_key_env_var")
        if "profile_id" in payload:
            profile = payload["profile_id"]
            path = self._profile_path(profile) if isinstance(profile, str) else None
            if path is None:
                raise SettingsError("profile_id", "unknown_profile")
            updates[ENV_PROFILE] = path
            saved["profile_id"] = profile
        requests = (
            _integer(payload["max_requests"], "max_requests", _REQUEST_LIMIT)
            if "max_requests" in payload else current.get("max_requests")
        )
        concurrent = (
            _integer(
                payload["max_concurrent_requests"], "max_concurrent_requests", _CONCURRENT_LIMIT
            )
            if "max_concurrent_requests" in payload else current.get("max_concurrent_requests")
        )
        if "max_requests" in payload or "max_concurrent_requests" in payload:
            if requests is None:
                raise SettingsError("max_requests", "missing")
            if concurrent is None:
                raise SettingsError("max_concurrent_requests", "missing")
            updates[ENV_POLICY] = self._policy_json(requests, concurrent)
            saved["max_requests"], saved["max_concurrent_requests"] = requests, concurrent

        key = payload.get("api_key")
        if "api_key" in payload and (not isinstance(key, str) or not _KEY.match(key)):
            raise SettingsError("api_key", "invalid")
        persist = payload.get("persist_key", False)
        if not isinstance(persist, bool):
            raise SettingsError("persist_key", "invalid_type")
        if persist and key is None:
            raise SettingsError("persist_key", "requires_api_key")

        candidate = dict(self.env)
        candidate.update(updates)
        for name in removals:
            candidate.pop(name, None)
        candidate_provider = candidate.get(ENV_PROVIDER)
        candidate_model = candidate.get(ENV_MODEL)
        candidate_is_go = is_go_provider(
            candidate_provider if isinstance(candidate_provider, str) else None
        )
        if candidate.get(ENV_CYBER) and candidate_provider != "litellm":
            raise SettingsError("cyber_access_program", "unsupported")
        if candidate_is_go and ("model" in payload or "provider" in payload):
            if not isinstance(candidate_model, str) or not candidate_model:
                raise SettingsError("model", "missing")
            try:
                _validate_go_model(candidate_model)
            except ValueError:
                raise SettingsError("model", "invalid") from None
        candidate_key_var = _key_var_for(candidate)
        if key is not None:
            candidate[candidate_key_var] = key
        if candidate.get(REMOVED_MAX_RETRIES_ENV):
            raise SettingsError("max_retries", "removed")
        if (
            updates.get(ENV_POLICY)
            or updates.get(ENV_PROFILE)
            or updates.get(ENV_MODEL)
            or updates.get(ENV_PROVIDER)
            or updates.get(ENV_EFFORT)
            or updates.get(ENV_CYBER)
            or removals
        ):
            try:
                if candidate.get(ENV_PROVIDER):
                    LLMConfig.from_environment(candidate)
                OrchestrationConfig.from_environment(candidate)
            except Exception:  # noqa: BLE001 - stable code only
                raise SettingsError("settings", "invalid") from None

        overridden = sorted(
            name for name in updates if self.env.get(name) not in (None, updates[name])
        ) + sorted(name for name in removals if self.env.get(name) is not None)
        # Persist before committing so a storage failure leaves the process unchanged.
        try:
            if saved or saved_removals:
                merged = {**self._read_saved(), **saved}
                for key_name in saved_removals:
                    merged.pop(key_name, None)
                self._save(merged)
            if persist and key is not None:
                write_env_var(self.env_path, candidate_key_var, key)
        except OSError:
            raise SettingsError("storage", "write_failed") from None
        for name, value in updates.items():
            self.env[name] = value
        for name in removals:
            self.env.pop(name, None)
        if key is not None:
            self.env[candidate_key_var] = key
        self._key_var_explicit = explicit_ref is not None
        return {
            "key_persisted": bool(persist and key is not None),
            "env_gitignored": (
                env_file_is_gitignored(self.env_path) if persist and key is not None else None
            ),
            "overrode_environment": overridden,
        }

    def _read_saved(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


def _discard(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass
