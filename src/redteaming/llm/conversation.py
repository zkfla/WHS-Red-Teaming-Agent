from __future__ import annotations

from dataclasses import dataclass, field
import os
import re

from .client import ChatClient, LLMConfig, LLMError, build_client
from .profiles import oauth_config
from ..models import Scope


SYSTEM_PROMPT = """You are the conversational controller for an authorized red-team agent.
Never invent scope, credentials, evidence, findings, or tool results. Ask for an explicit
authorized scope before planning scans. Explain proposed actions, risk, and expected evidence.
The controller may suggest reconnaissance and verification work, but execution is handled by
the policy-gated tool layer. Keep answers concise and operational.
"""


@dataclass
class Conversation:
    client: ChatClient | None = None
    scope: Scope | None = None
    messages: list[dict[str, str]] = field(default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}])

    def set_model(self, model: str) -> str:
        model = model.strip()
        if not model:
            return "사용법: /model <provider/model>"
        if self.client is None:
            return "LLM provider가 연결되지 않았습니다."
        setter = getattr(self.client, "set_model", None)
        if setter is None:
            return "현재 LLM client는 런타임 모델 변경을 지원하지 않습니다."
        setter(model)
        return f"LLM 모델을 변경했습니다: {model}"

    def configure(self, config: LLMConfig) -> str:
        self.client = build_client(config)
        if self.client is None:
            return "LLM provider 설정이 비어 있습니다."
        return f"LLM 연결을 설정했습니다: {config.model}"

    def _register_scope_from_text(self, text: str) -> str | None:
        """Handle natural-language scope registration before any LLM call."""
        korean_scope_intent = ("허용", "승인", "등록", "대상", "정찰", "스캔", "분석", "점검")
        scope_words = ("허용", "승인", "대상", "타깃", "scope", "스코프")
        action_words = ("정찰", "스캔", "포트", "서비스", "취약점", "확인", "분석", "analyze", "scan", "recon")
        if (not any(word in text.lower() for word in scope_words + action_words)
                and not any(word in text for word in korean_scope_intent)):
            return None
        extraction_text = re.sub(r"\[[^\]]+\]\((https?://[^)]+)\)", r"\1", text)
        url_pattern = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
        host_pattern = r"(?:\[[0-9A-Fa-f:.]+\]|\d{1,3}(?:\.\d{1,3}){3}|localhost|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?::\d{1,5})?"
        pattern = rf"(?:{url_pattern}|{host_pattern})"
        hosts = tuple(dict.fromkeys(
            self._clean_scope_token(item) for item in re.findall(pattern, extraction_text)
            if self._clean_scope_token(item)
        ))
        if not hosts:
            return None
        normalized = text.lower().replace(" ", "")
        add_mode = any(token in normalized for token in ("추가", "also", "another", "add", "더"))
        add_mode = add_mode or any(token in normalized for token in ("추가", "더해", "더해줘"))
        if add_mode and self.scope is not None:
            hosts = tuple(dict.fromkeys(self.scope.allowed_hosts + hosts))
            self.scope = Scope(allowed_hosts=hosts)
            return f"허용 Scope에 대상을 추가했습니다: {', '.join(hosts)}"
        self.scope = Scope(allowed_hosts=hosts)
        return f"허용 Scope를 등록했습니다: {', '.join(hosts)}"

    def use_existing_oauth(self) -> str:
        """Use a keyless provider profile configured in this local environment."""
        self.client = build_client(oauth_config())
        return "로컬 OAuth/키 없는 설정을 사용합니다. API key를 입력하지 않아도 됩니다."

    def use_litellm_from_environment(self) -> str:
        """Connect LiteLLM from the current process environment, without prompting for a key."""
        os.environ["REDTEAM_LLM_BACKEND"] = "litellm"
        config = LLMConfig.from_env()
        if config is None:
            config = oauth_config()
        self.client = build_client(config)
        return f"LiteLLM 연결을 사용합니다: {config.model} (인증정보는 현재 세션 설정만 사용)"

    def connection_status(self) -> str:
        """Explain the local LLM setup without sending a model request."""
        if self.client is None:
            return "LLM이 아직 연결되지 않았습니다. ‘LiteLLM으로 연결해줘’라고 입력하세요."
        summary = getattr(self.client, "connection_summary", None)
        if callable(summary):
            return str(summary())
        config = getattr(self.client, "config", None)
        return f"LLM 설정됨: model={getattr(config, 'model', 'unknown')}"

    def reply(self, text: str) -> str:
        natural_scope = self._register_scope_from_text(text)
        if natural_scope:
            return natural_scope
        normalized = text.lower().replace(" ", "")
        if any(phrase in normalized for phrase in ("연결상태", "llm상태", "litellm상태", "인증상태")):
            return self.connection_status()
        if "litellm" in normalized or ("llm" in normalized and any(token in normalized for token in ("연결", "사용", "설정"))):
            return self.use_litellm_from_environment()
        if any(
            phrase in normalized
            for phrase in ("oauth", "오쓰", "오어쓰", "키없이", "키등록없이", "기존설정", "기존llm")
        ):
            return self.use_existing_oauth()
        if text.startswith("/scope "):
            hosts = tuple(self._clean_scope_token(item) for item in text.removeprefix("/scope ").split() if item)
            hosts = tuple(item for item in hosts if item)
            if not hosts:
                return "사용법: /scope host.example 또는 /scope host1 host2"
            self.scope = Scope(allowed_hosts=hosts)
            return f"허용 Scope를 등록했습니다: {', '.join(hosts)}"
        if text.startswith("/scope-add "):
            hosts = tuple(self._clean_scope_token(item) for item in text.removeprefix("/scope-add ").split() if item)
            hosts = tuple(item for item in hosts if item)
            if not hosts:
                return "사용법: /scope-add host.example 또는 /scope-add host1 host2"
            current = self.scope.allowed_hosts if self.scope is not None else ()
            merged = tuple(dict.fromkeys(current + hosts))
            self.scope = Scope(allowed_hosts=merged)
            return f"허용 Scope에 대상을 추가했습니다: {', '.join(merged)}"
        if text == "/status":
            if self.scope is None:
                return "Scope가 아직 등록되지 않았습니다. /scope <허용 호스트>를 입력하세요."
            return f"scope={', '.join(self.scope.allowed_hosts)}"
        if text == "/model":
            if self.client is None:
                return "LLM provider가 연결되지 않았습니다."
            config = getattr(self.client, "config", None)
            model = getattr(config, "model", "unknown") or "(현재 설정에서 자동 로드)"
            return f"model={model}\n사용법: /model <provider/model>"
        if text.startswith("/model "):
            model = text.removeprefix("/model ").strip()
            if model.startswith("openai/") and self.client is not None:
                config = getattr(self.client, "config", None)
                if not getattr(config, "api_key", ""):
                    return "openai 모델은 OpenAI API key가 필요합니다. OAuth를 쓰려면 'OAuth로 연결해줘'라고 입력하세요."
            return self.set_model(model)
        if text == "/clear":
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            return "대화 컨텍스트를 초기화했습니다."
        if self.scope is None:
            return "먼저 명시적인 허용 대상을 등록하세요. 예: /scope <host>"
        if self.client is None:
            return "LLM이 연결되지 않았습니다. 환경변수 REDTEAM_LLM_API_KEY를 설정하면 대화형 계획을 사용할 수 있습니다."

        self.messages.append({"role": "user", "content": text})
        try:
            answer = self.client.chat(self.messages)
        except LLMError as exc:
            return f"LLM 호출 실패: {exc}"
        self.messages.append({"role": "assistant", "content": answer})
        return answer

    @staticmethod
    def _clean_scope_token(token: str) -> str:
        markdown = re.fullmatch(r"\[([^]]+)\]\(([^)]+)\)", token)
        if markdown:
            return markdown.group(2)
        return token.strip("`<>,.!?;:()")
