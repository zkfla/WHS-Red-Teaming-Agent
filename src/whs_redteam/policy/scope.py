"""Deterministic URL and method scope enforcement."""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass
from urllib.parse import unquote, urlsplit

from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import ScopeViolation
from whs_redteam.sanitizer import sanitize_url

BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.internal",
}


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    allowed: bool
    reason: str
    url: str
    method: str

    def to_dict(self) -> dict[str, bool | str]:
        result = asdict(self)
        result["url"] = sanitize_url(self.url)
        return result


@dataclass(frozen=True, slots=True)
class _TargetOrigin:
    scheme: str
    hostname: str
    port: int
    base_path: str


def _effective_port(scheme: str, port: int | None) -> int:
    return port if port is not None else (443 if scheme == "https" else 80)


def _normalize_path(path: str) -> str:
    decoded = unquote(path or "/")
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def _path_matches(path: str, prefix: str) -> bool:
    normalized_prefix = _normalize_path(prefix).rstrip("/") or "/"
    if normalized_prefix == "/":
        return True
    return path == normalized_prefix or path.startswith(normalized_prefix + "/")


class ScopeGuard:
    """Authorize network actions against an immutable engagement scope."""

    def __init__(
        self,
        config: EngagementConfig,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self.config = config
        self.audit_logger = audit_logger
        self._targets = tuple(self._parse_target(target) for target in config.targets)

    @staticmethod
    def _parse_target(target: str) -> _TargetOrigin:
        parsed = urlsplit(target)
        return _TargetOrigin(
            scheme=parsed.scheme.lower(),
            hostname=(parsed.hostname or "").lower().rstrip("."),
            port=_effective_port(parsed.scheme.lower(), parsed.port),
            base_path=_normalize_path(parsed.path),
        )

    def evaluate(self, url: str, method: str = "GET") -> ScopeDecision:
        method = method.upper()
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return self._decision(False, "URL must be absolute HTTP(S)", url, method)
            if parsed.username or parsed.password:
                return self._decision(False, "embedded credentials are forbidden", url, method)

            hostname = parsed.hostname.lower().rstrip(".")
            if hostname in BLOCKED_METADATA_HOSTS:
                return self._decision(False, "metadata endpoints are forbidden", url, method)
            try:
                ip = ipaddress.ip_address(hostname)
            except ValueError:
                ip = None
            if ip is not None and ip.is_unspecified:
                return self._decision(False, "unspecified IP addresses are forbidden", url, method)

            scheme = parsed.scheme.lower()
            port = _effective_port(scheme, parsed.port)
            path = _normalize_path(parsed.path)
            target = next(
                (
                    item
                    for item in self._targets
                    if item.scheme == scheme
                    and item.hostname == hostname
                    and item.port == port
                    and _path_matches(path, item.base_path)
                ),
                None,
            )
            if target is None:
                return self._decision(
                    False,
                    "target origin or base path is out of scope",
                    url,
                    method,
                )
            if method not in self.config.allowed_methods:
                return self._decision(False, f"HTTP method {method} is not allowed", url, method)
            if any(_path_matches(path, excluded) for excluded in self.config.excluded_paths):
                return self._decision(False, "path is explicitly excluded", url, method)
            return self._decision(True, "request is within the approved scope", url, method)
        except ValueError as exc:
            return self._decision(False, f"invalid URL: {exc}", url, method)

    def authorize(self, url: str, method: str = "GET") -> ScopeDecision:
        decision = self.evaluate(url, method)
        if not decision.allowed:
            raise ScopeViolation(decision.reason)
        return decision

    def authorize_redirect(self, redirect_url: str, method: str = "GET") -> ScopeDecision:
        """Require a fresh policy decision for a redirect destination."""

        return self.authorize(redirect_url, method)

    def _decision(self, allowed: bool, reason: str, url: str, method: str) -> ScopeDecision:
        decision = ScopeDecision(allowed=allowed, reason=reason, url=url, method=method)
        if self.audit_logger is not None:
            self.audit_logger.emit(
                "policy_decision", allowed=allowed, reason=reason, url=url, method=method
            )
        return decision
