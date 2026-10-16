"""OpenCode Go provider adapter.

Operator-selectable access to the OpenCode Go gateway over three explicit
SDK-qualified model forms -- openai/<id> (Chat Completions),
openai/responses/<id> (Responses) and anthropic/<id> (Messages) --
without a model-name catalog.

Routing grammar (single source of truth, longest prefix first): the
openai/responses/ marker selects Responses; openai/ and anthropic/ select
Chat and Messages. A matched longer prefix with no meaningful id raises
and never falls through to a shorter prefix.
Attribution: own User-Agent plus stable x-opencode-session per Run/owner;
outside a Run a stable client-owned identity. Messages adds the Go Bearer
header via extra_headers; LiteLLM also supplies its Anthropic x-api-key
header to the same endpoint. Structured responses convey the schema via
output_config because response_format is dropped on that path.

Wire translation stays inside LiteLLM: the adapter wraps
redteam.llm.litellm.LiteLLMClient and only adds endpoint pinning,
headers, and the secret-free connection fingerprint.
"""

from __future__ import annotations

import hashlib
import uuid
from importlib import metadata as _metadata
from typing import TYPE_CHECKING, Any

from redteam.core.ports.llm import (
    NativeCompletion,
    NativeToolSpec,
    StructuredCompletion,
)
from redteam.core.request_scope import current_request_scope
from redteam.llm.litellm import LiteLLMClient
from redteam.llm.opencode_go_config import (
    OPENCODE_GO_PROVIDER,
    USER_AGENT,
)
from redteam.llm.opencode_go_config import (
    llm_connection_fingerprint_for_config as llm_connection_fingerprint_for_config,
)
from redteam.llm.opencode_go_config import (
    parse_model as parse_model,
)
from redteam.llm.reasoning_effort import apply_messages_effort, litellm_default_kwargs

if TYPE_CHECKING:
    from redteam.core.config import LLMConfig

MIN_LITELLM_VERSION = (1, 104, 0)

_SESSION_HEADER = "x-opencode-session"
_USER_AGENT_HEADER = "User-Agent"
_AUTH_HEADER = "authorization"

# SDK-supported keys that would retarget the endpoint, credential, routing
# or transport. Vendor connection settings belong to this adapter.
_REJECTED_CALL_KEYS = frozenset(
    {
        "extra_headers",
        "headers",
        "api_base",
        "api_key",
        "custom_llm_provider",
        "client",
        "http_client",
        "base_url",
        "auth_token",
        "use_bearer_for_custom_base",
        "use_responses_api",
    }
)

def _litellm_version() -> str:
    return _metadata.version("litellm")


def _require_litellm() -> None:
    minimum = ".".join(str(part) for part in MIN_LITELLM_VERSION)
    install_hint = f'upgrade with: python -m pip install --upgrade "litellm>={minimum}"'
    try:
        installed = _litellm_version()
    except _metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"opencode-go requires litellm>={minimum}; {install_hint}",
        ) from exc
    # packaging is a LiteLLM dependency; compare prereleases correctly too.
    from packaging.version import InvalidVersion, Version

    try:
        supported = Version(installed) >= Version(minimum)
    except InvalidVersion:
        supported = False
    if not supported:
        raise RuntimeError(
            f"opencode-go requires litellm>={minimum}; found {installed}; {install_hint}",
        )


class OpenCodeGoClient:
    """LLM-port client pinned to the OpenCode Go gateway."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        protocol: str,
        endpoint: str,
        api_key: str,
        client: LiteLLMClient,
        fingerprint: str,
        user_agent: str = USER_AGENT,
        reasoning_effort: str | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._protocol = protocol
        self._endpoint = endpoint
        self._api_key = api_key
        self._client = client
        self._fingerprint = fingerprint
        self._user_agent = user_agent
        from redteam.core.contracts.llm_config import normalize_reasoning_effort

        self._reasoning_effort = normalize_reasoning_effort(reasoning_effort)
        self._client_identity = uuid.uuid4().hex

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def connection_fingerprint(self) -> str:
        return self._fingerprint

    def __repr__(self) -> str:
        return "OpenCodeGoClient(" + self._model + "/" + self._protocol + ")"

    def _session_id(self) -> str:
        scope = current_request_scope()
        if scope is None:
            return self._client_identity
        return hashlib.sha256(repr((scope.run_id, scope.owner)).encode()).hexdigest()

    def _guard_kwargs(self, kwargs: dict[str, Any]) -> None:
        for key in _REJECTED_CALL_KEYS:
            if key in kwargs:
                raise ValueError("per-call override rejected: " + key)
        if kwargs.get("num_retries", 0) != 0:
            raise ValueError(
                "opencode-go disables hidden SDK retries; per-call "
                "num_retries must be 0",
            )

    def _call_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        self._guard_kwargs(kwargs)
        headers = {}
        headers[_USER_AGENT_HEADER] = self._user_agent
        headers[_SESSION_HEADER] = self._session_id()
        if self._protocol == "messages":
            headers[_AUTH_HEADER] = "Bearer " + self._api_key
        merged = dict(kwargs)
        merged["extra_headers"] = headers
        if self._protocol == "messages" and self._reasoning_effort is not None:
            merged["output_config"] = apply_messages_effort(
                merged.get("output_config"), self._reasoning_effort
            )
        return merged

    def _structured_kwargs(
        self, kwargs: dict[str, Any], response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        call_kwargs = self._call_kwargs(kwargs)
        if self._protocol == "messages":
            merged = apply_messages_effort(
                call_kwargs.get("output_config"), self._reasoning_effort
            )
            fmt = merged.get("format")
            fmt_dict = dict(fmt) if isinstance(fmt, dict) else {}
            fmt_dict["type"] = "json_schema"
            fmt_dict["schema"] = response_schema
            merged["format"] = fmt_dict
            call_kwargs["output_config"] = merged
        return call_kwargs

    async def complete(self, *, messages, system=None, **kwargs) -> str:
        return await self._client.complete(
            messages=messages, system=system, **self._call_kwargs(kwargs)
        )

    async def complete_with_usage(self, *, messages, system=None, **kwargs) -> StructuredCompletion:
        return await self._client.complete_with_usage(
            messages=messages, system=system, **self._call_kwargs(kwargs)
        )

    async def complete_structured_with_usage(
        self, *, messages, response_schema, system=None, **kwargs
    ) -> StructuredCompletion:
        return await self._client.complete_structured_with_usage(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **self._structured_kwargs(kwargs, response_schema),
        )

    async def complete_structured(
        self, *, messages, response_schema, system=None, **kwargs
    ) -> dict[str, Any]:
        return await self._client.complete_structured(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **self._structured_kwargs(kwargs, response_schema),
        )

    async def complete_native(
        self, messages: list[dict[str, Any]], tools: list[NativeToolSpec],
        system: str | None = None, **kwargs: Any
    ) -> NativeCompletion:
        return await self._client.complete_native(
            messages, tools, system=system, **self._call_kwargs(kwargs)
        )
def build(config: LLMConfig) -> OpenCodeGoClient:
    """Build the Go client from the existing LLMConfig."""
    provider = getattr(config, "provider", None)
    if provider != OPENCODE_GO_PROVIDER:
        raise ValueError(
            "opencode-go adapter requires provider opencode-go; received " + repr(provider),
        )
    model = config.default_model.strip()
    parsed = parse_model(model)
    resolve = getattr(config, "resolve_api_key", None)
    api_key = resolve() if callable(resolve) else None
    if not api_key:
        env_var = getattr(config, "api_key_env_var", "LLM_API_KEY")
        raise ValueError(
            "opencode-go requires an API key: set " + str(env_var),
        )
    _require_litellm()
    effort_kwargs = litellm_default_kwargs(getattr(config, "reasoning_effort", None))
    inner_kwargs: dict[str, Any] = {"api_base": parsed.endpoint}
    inner_kwargs.update(effort_kwargs)
    if parsed.protocol == "responses":
        inner = LiteLLMClient(
            parsed.sdk_model, api_key, num_retries=0, use_responses_api=True, **inner_kwargs
        )
    else:
        inner = LiteLLMClient(parsed.sdk_model, api_key, num_retries=0, **inner_kwargs)
    fingerprint = llm_connection_fingerprint_for_config(config)
    assert fingerprint is not None
    return OpenCodeGoClient(
        provider=provider, model=model, protocol=parsed.protocol,
        endpoint=parsed.endpoint, api_key=api_key, client=inner, fingerprint=fingerprint,
        reasoning_effort=getattr(config, "reasoning_effort", None),
    )
