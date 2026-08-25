"""컴파일된 정책 — Hard Constraint 를 코드가 물어볼 수 있는 형태로 만든다.

`compiler.py::compile_policy()` 만 이 객체를 만든다. `probing/session.py` 가
모든 실제 요청 직전에 `check_request()` 를 부른다 — 이게 최종 강제 지점이다
(§8). `agent/react.py` 는 그보다 먼저 `is_method_allowed`/`check_amount` 로
미리 걸러서 LLM 에게 왜 막혔는지 알려주지만, 그 사전 검사를 빠뜨리거나
우회해도 여기서 다시 막힌다 — 방어의 이중화다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from finredteam.config import AllowedHostError, assert_allowed_host
from finredteam.engagement.errors import PolicyViolation

_STATE_CHANGE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class CompiledPolicy:
    engagement_id: str

    allowed_hosts: frozenset[str]
    denied_hosts: frozenset[str]

    external_services_allowed: bool

    allow_state_change: bool
    allow_account_creation: bool
    allow_file_upload: bool
    allow_delete: bool
    allow_race_testing: bool
    allow_dos: bool
    allow_bruteforce: bool

    max_requests_per_second: float
    max_concurrency: int
    max_amount: Decimal

    allow_real_user_data: bool
    allow_raw_response_storage: bool

    cleanup_required: bool
    cleanup_resource_types: tuple[str, ...]

    stop_repeated_5xx: bool
    stop_repeated_5xx_threshold: int
    stop_request_budget_exceeded: bool

    guidance: tuple[str, ...]

    policy_hash: str

    def is_host_allowed(self, host: str | None) -> bool:
        if host is None:
            return False
        if host in self.denied_hosts:
            return False
        return host in self.allowed_hosts

    def is_method_allowed(self, method: str) -> bool:
        method = method.upper()
        if method == "DELETE" and not self.allow_delete:
            return False
        if method in _STATE_CHANGE_METHODS and not self.allow_state_change:
            return False
        return True

    def is_action_allowed(self, action_type: str) -> bool:
        mapping = {
            "account_creation": self.allow_account_creation,
            "file_upload": self.allow_file_upload,
            "race_testing": self.allow_race_testing,
            "dos": self.allow_dos,
            "bruteforce": self.allow_bruteforce,
            "external_service": self.external_services_allowed,
        }
        if action_type not in mapping:
            raise ValueError(f"알 수 없는 action_type: {action_type!r}")
        return mapping[action_type]

    def check_request(self, method: str, url: str) -> None:
        """실제로 나갈 요청 하나를 검사한다 — `probing/session.py::_request()`
        가 `self._client.request()` 호출 직전에 부른다. 여기서 막히면 아무
        바이트도 네트워크로 나가지 않는다."""
        host = urlparse(url).hostname
        try:
            assert_allowed_host(url, self.allowed_hosts)
        except AllowedHostError as exc:
            raise PolicyViolation("OUT_OF_SCOPE_HOST", str(exc)) from exc
        if host in self.denied_hosts:
            raise PolicyViolation("OUT_OF_SCOPE_HOST", f"host explicitly denied: {host!r}")
        if not self.is_method_allowed(method):
            rule = "STATE_CHANGE_FORBIDDEN" if method.upper() != "DELETE" else "METHOD_FORBIDDEN"
            raise PolicyViolation(rule, f"method not allowed by engagement policy: {method}")

    def check_amount(self, amount: Decimal) -> None:
        if abs(amount) > self.max_amount:
            raise PolicyViolation(
                "AMOUNT_EXCEEDS_CEILING",
                f"|{amount}| > effective max_amount {self.max_amount}",
            )
