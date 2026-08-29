from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


class ModelProvider(StrEnum):
    AUTO = "auto"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    LITELLM = "litellm"
    CODEX_CHATGPT = "codex-chatgpt"


PROVIDER_KEYS: dict[ModelProvider, str] = {
    ModelProvider.OPENAI: "OPENAI_API_KEY",
    ModelProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
    ModelProvider.OPENROUTER: "OPENROUTER_API_KEY",
    ModelProvider.GEMINI: "GEMINI_API_KEY",
    ModelProvider.LITELLM: "LITELLM_API_KEY",
}

MODEL_PREFIXES: dict[str, ModelProvider] = {
    "openai": ModelProvider.OPENAI,
    "anthropic": ModelProvider.ANTHROPIC,
    "openrouter": ModelProvider.OPENROUTER,
    "gemini": ModelProvider.GEMINI,
    "google": ModelProvider.GEMINI,
}


@dataclass(frozen=True)
class ProviderCredential:
    provider: ModelProvider
    api_key: str
    env_file: Path | None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_project_environment(
    env_file: Path | None, *, untrusted_repository_root: Path
) -> Path | None:
    """Load trusted operator configuration without reading the analyzed repository."""
    untrusted_root = untrusted_repository_root.resolve()
    if env_file is not None:
        candidate = env_file.expanduser().resolve()
        if _is_within(candidate, untrusted_root):
            raise ValueError("--env-file must be outside the analyzed repository")
        if not candidate.is_file():
            raise ValueError(f"Environment file does not exist: {candidate}")
        load_dotenv(candidate, override=False)
        return candidate

    application_root = Path(__file__).resolve().parents[3]
    candidates = [application_root / ".env"]
    discovered = find_dotenv(usecwd=True)
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and not _is_within(resolved, untrusted_root):
            load_dotenv(resolved, override=False)
            return resolved
    return None


def _model_provider(model: str) -> ModelProvider | None:
    prefix, separator, _ = model.partition("/")
    return MODEL_PREFIXES.get(prefix.lower()) if separator else None


def resolve_github_observer_token(
    env_file: Path | None,
    *,
    untrusted_repository_root: Path,
) -> str | None:
    """Load an operator-controlled GitHub token without trusting repository-local config."""

    load_project_environment(
        env_file,
        untrusted_repository_root=untrusted_repository_root,
    )
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(variable, "").strip()
        if token:
            return token
    return None


def resolve_litellm_credential(
    provider: ModelProvider,
    model: str,
    *,
    env_file: Path | None,
    untrusted_repository_root: Path,
) -> ProviderCredential:
    loaded_file = load_project_environment(
        env_file, untrusted_repository_root=untrusted_repository_root
    )
    available = [item for item, name in PROVIDER_KEYS.items() if os.environ.get(name)]
    if provider is ModelProvider.AUTO:
        if len(available) > 1:
            names = ", ".join(item.value for item in available)
            raise ValueError(
                "Multiple provider credentials are configured "
                f"({names}); choose one with --provider"
            )
        if not available:
            raise ValueError(
                "No provider credential is configured; set one supported key or choose "
                "--provider codex-chatgpt"
            )
        provider = available[0]
    if provider is ModelProvider.CODEX_CHATGPT:
        raise ValueError("codex-chatgpt does not use a LiteLLM API key")

    expected = _model_provider(model)
    if expected is not None and expected is not provider and provider is not ModelProvider.LITELLM:
        raise ValueError(
            f"Model {model!r} identifies provider {expected.value!r}, but --provider "
            f"selected {provider.value!r}"
        )
    variable = PROVIDER_KEYS[provider]
    api_key = os.environ.get(variable, "")
    if not api_key:
        raise ValueError(f"Selected provider {provider.value!r} requires {variable}")
    return ProviderCredential(provider=provider, api_key=api_key, env_file=loaded_file)
