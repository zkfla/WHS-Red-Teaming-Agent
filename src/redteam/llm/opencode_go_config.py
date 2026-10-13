"""SDK-free selection vocabulary for the OpenCode Go adapter.

No third-party imports: safe for app bootstrap, CLI validation, and tests
without the optional `litellm` dependency installed.

This module owns the Go model-shape grammar. The T1 adapter module
(`redteam.llm.opencode_go`), bootstrap, CLI validate, resume preflights, and
binding code import and reuse the grammar, endpoints, User-Agent, and
fingerprint below -- no independent re-implementation. Nothing here is
configured per call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from redteam import __version__ as _package_version

__all__ = [
    "OPENCODE_GO_PROVIDER",
    "OPENCODE_GO_API_KEY_ENV_VAR",
    "is_go_provider",
    "validate_model",
    "GoModel",
    "parse_model",
    "llm_connection_fingerprint_for_config",
]

#: Provider name selecting the OpenCode Go adapter.
OPENCODE_GO_PROVIDER = "opencode-go"

#: Reference environment-variable name for the Go adapter API key, written by
#: onboarding when the operator has not already configured a key variable.
OPENCODE_GO_API_KEY_ENV_VAR = "OPENCODE_API_KEY"

#: Pinned Go bases (LiteLLM appends the path suffix; never configured).
OPENCODE_GO_CHAT_ENDPOINT = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_RESPONSES_ENDPOINT = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_MESSAGES_ENDPOINT_BASE = "https://opencode.ai/zen/go"

#: Own User-Agent sent on every Go request (also fingerprinted).
USER_AGENT = f"whs-red-teaming-agent/{_package_version} (opencode-go)"


def is_go_provider(provider: str | None) -> bool:
    """Return True only for the exact `opencode-go` provider name."""
    return (provider or "").strip() == OPENCODE_GO_PROVIDER


_MODEL_PREFIXES = (
    ("openai/responses/", "responses", OPENCODE_GO_RESPONSES_ENDPOINT, True),
    ("openai/", "chat", OPENCODE_GO_CHAT_ENDPOINT, False),
    ("anthropic/", "messages", OPENCODE_GO_MESSAGES_ENDPOINT_BASE, False),
)


@dataclass(frozen=True)
class GoModel:
    """Parsed Go model: wire protocol, endpoint, SDK model, matched prefix."""

    protocol: str
    endpoint: str
    sdk_model: str
    prefix: str


def parse_model(model: str) -> GoModel:
    """Parse and validate a user-facing Go model (single source of truth).

    The longest matching prefix wins and is never re-examined: once e.g.
    ``openai/responses/`` matches, an empty remainder is an error -- the
    shorter ``openai/`` branch must not claim the remainder ``responses/``.
    A matched id must be slash-free and whitespace-free.
    """
    if isinstance(model, str):
        model = model.strip()
        for prefix, protocol, endpoint, keep_provider in _MODEL_PREFIXES:
            if model.startswith(prefix):
                rest = model[len(prefix):]
                bad = (not rest.strip()) or ("/" in rest)
                if not bad:
                    for ch in rest:
                        if ch.isspace():
                            bad = True
                if bad:
                    raise ValueError(
                        "invalid REDTEAM_LLM_MODEL=" + repr(model) + " for "
                        "provider 'opencode-go' -- the model id after "
                        + repr(prefix) + " must not be empty and must not "
                        "contain '/' or whitespace",
                    )
                sdk = ("openai/" + rest) if keep_provider else model
                return GoModel(protocol, endpoint, sdk, prefix)
    raise ValueError(
        "invalid REDTEAM_LLM_MODEL=" + repr(model) + " for provider "
        "'opencode-go' -- use openai/<id>, openai/responses/<id>, or "
        "anthropic/<id>"
    )


def llm_connection_fingerprint_for_config(llm_config: Any) -> str | None:
    """Secret-free Go connection fingerprint for binding and resume.

    Canonical form is a sorted, compact JSON object so the value is stable
    and independent of a string-join convention. Only the credential
    REFERENCE (the env var name) is included -- never the key value, and
    never the session id.
    """
    provider = getattr(llm_config, "provider", None) or ""
    if (not isinstance(provider, str)) or (provider.strip() != OPENCODE_GO_PROVIDER):
        return None
    model = getattr(llm_config, "default_model", "") or ""
    parsed = parse_model(model)
    credential = getattr(llm_config, "api_key_env_var", "") or ""
    canonical = json.dumps(
        {
            "v": "v1",
            "endpoint": parsed.endpoint,
            "protocol": parsed.prefix,
            "model": model.strip(),
            "credential": credential,
            "ua": USER_AGENT,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_model(model: str) -> str:
    """Check an ``opencode-go`` model string without any provider call.

    Returns the stripped model when it has SDK-qualified shape; raises
    ``ValueError`` with an actionable message otherwise. Delegates to
    ``parse_model`` so there is exactly one routing grammar. Pure, offline,
    and shared by bootstrap selection and ``redteam validate``.
    """
    cleaned = (model or "").strip()
    if not cleaned:
        raise ValueError(
            "REDTEAM_LLM_MODEL is not set for provider 'opencode-go' -- set it to "
            "openai/<id>, openai/responses/<id>, or anthropic/<id>."
        )
    parse_model(cleaned)
    return cleaned
