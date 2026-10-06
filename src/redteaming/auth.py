from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HttpIdentityProfile:
    """In-memory credentials supplied by an authorized assessment operator.

    Profiles are deliberately transport-neutral. The CLI/API may construct one
    from a short-lived token or cookie, but this object never persists secrets.
    """

    name: str
    headers: tuple[tuple[str, str], ...] = ()

    def as_headers(self) -> dict[str, str]:
        return {key: value for key, value in self.headers}


@dataclass(frozen=True)
class AuthorizationCheck:
    """Operator-declared object ownership check for safe GET replay."""

    object_key: str
    owner_identity: str
    comparison_identities: tuple[str, ...]
    label: str = "operator-declared object"
    denied_statuses: tuple[int, ...] = (401, 403)


ANONYMOUS = HttpIdentityProfile("anonymous")
