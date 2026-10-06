from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from urllib.request import Request, urlopen
from typing import Protocol


class LLMError(RuntimeError):
    pass


class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, str]]) -> str:
        ...


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        api_key = os.getenv("REDTEAM_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("REDTEAM_LLM_BASE_URL", "https://api.openai.com/v1")
        explicit_model = os.getenv("REDTEAM_LLM_MODEL") or os.getenv("LITELLM_MODEL")
        model = explicit_model or "gpt-4o-mini"
        backend = os.getenv("REDTEAM_LLM_BACKEND", "litellm").lower()
        if not api_key and not explicit_model and not os.getenv("REDTEAM_OAUTH_MODEL"):
            return None
        if not api_key and backend not in {"litellm", "direct-litellm", "openai-compatible", "auto"} and not explicit_model:
            return None
        return cls(base_url.rstrip("/"), api_key or "", model)


def _is_reasoning_model(model: str) -> bool:
    lowered = model.lower().split("/", 1)[-1]
    return lowered.startswith(("gpt-5", "o1", "o3", "o4"))


class OpenAICompatibleClient:
    """Small stdlib-only client for OpenAI-compatible chat-completions endpoints."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def set_model(self, model: str) -> None:
        self.config = LLMConfig(**{**self.config.__dict__, "model": model})

    def chat(self, messages: list[dict[str, str]]) -> str:
        payload_data: dict[str, object] = {"model": self.config.model, "messages": messages}
        if not _is_reasoning_model(self.config.model):
            payload_data["temperature"] = 0.2
        reasoning_effort = os.getenv("REDTEAM_LLM_REASONING_EFFORT")
        if reasoning_effort:
            payload_data["reasoning_effort"] = reasoning_effort
        payload = json.dumps(payload_data).encode()
        request = Request(
            f"{self.config.base_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - provider/network dependent
            raise LLMError(str(exc)) from exc
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response did not contain choices[0].message.content") from exc


class LiteLLMClient:
    """Adapter for the existing LiteLLM routing configuration.

    Import is lazy so the core and tests remain dependency-free when LiteLLM is not installed.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def set_model(self, model: str) -> None:
        self.config = LLMConfig(**{**self.config.__dict__, "model": model})

    def connection_summary(self) -> str:
        """Report local LiteLLM facts without making a billable model call."""
        try:
            import litellm  # noqa: F401
        except ImportError:
            return "LiteLLM 패키지가 설치되지 않았습니다. .\\scripts\\bootstrap.ps1을 실행하세요."
        if self.config.api_key:
            credential = "현재 CLI 세션의 API key"
        elif self.config.model.lower().startswith("chatgpt/"):
            credential = "provider OAuth/환경 설정(키 없는 모드)"
        elif os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY"):
            credential = "환경변수 기반 인증"
        else:
            credential = "확인되지 않음(첫 호출 시 provider가 인증 오류를 반환할 수 있음)"
        return f"LiteLLM 준비됨: model={self.config.model}, credential={credential}"

    def chat(self, messages: list[dict[str, str]]) -> str:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise LLMError("LiteLLM is not installed; install it or use an OpenAI-compatible backend") from exc
        # LiteLLM can otherwise fail before the provider sees the request when a
        # reasoning model rejects temperature. Unsupported provider parameters
        # are safe to drop because the controller does not depend on them.
        litellm.drop_params = True
        kwargs: dict[str, object] = {"model": self.config.model, "messages": messages}
        lowered = self.config.model.lower()
        if not lowered.startswith(("gpt-5", "o1", "o3", "o4")):
            kwargs["temperature"] = 0.2
        reasoning_effort = os.getenv("REDTEAM_LLM_REASONING_EFFORT")
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if self.config.api_key:
            kwargs["api_key"] = self.config.api_key
        if self.config.base_url and self.config.base_url != "https://api.openai.com/v1":
            kwargs["api_base"] = self.config.base_url
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="Pydantic serializer warnings:")
                # The ChatGPT OAuth provider currently emits a Responses SSE stream
                # even for non-streaming completion requests. LiteLLM's bridge can
                # then see output=[] and raise "Unknown items ...". Consume that
                # provider's stream explicitly and rebuild the assistant text.
                if lowered.startswith("chatgpt/"):
                    kwargs["stream"] = True
                    return self._collect_stream(litellm.completion(**kwargs))
                response = litellm.completion(**kwargs)
            return str(response.choices[0].message.content)
        except Exception as exc:  # pragma: no cover - provider/network dependent
            raise LLMError(str(exc)) from exc

    @staticmethod
    def _collect_stream(response: object) -> str:
        parts: list[str] = []
        for chunk in response:  # type: ignore[union-attr]
            try:
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    parts.append(str(content))
            except (AttributeError, IndexError, TypeError):
                if isinstance(chunk, dict):
                    choices = chunk.get("choices", [])
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta", {})
                        if isinstance(delta, dict) and delta.get("content"):
                            parts.append(str(delta["content"]))
        if not parts:
            raise LLMError(
                "ChatGPT OAuth 응답 스트림에서 텍스트를 받지 못했습니다. "
                "LiteLLM chatgpt provider의 응답 파서 문제일 수 있습니다."
            )
        return "".join(parts)


def build_client(config: LLMConfig | None) -> ChatClient | None:
    if config is None:
        return None
    backend = os.getenv("REDTEAM_LLM_BACKEND", "litellm").lower()
    if backend in {"litellm", "direct-litellm", "auto"}:
        return LiteLLMClient(config)
    return OpenAICompatibleClient(config)
