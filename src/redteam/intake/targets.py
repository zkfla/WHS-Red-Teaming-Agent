"""Read explicit network addresses from operator input, without model inference."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from redteam.core.contracts.target import HostTarget, UrlTarget
from redteam.core.scope_rules import resolve_target_hostports

_ADDRESS = re.compile(
    r"https?://[\x21-\x7e]+"
    r"|(?<![A-Za-z0-9_@./-])"
    r"(?:localhost|\[[0-9a-fA-F:]+\]|(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]+)"
    r"(?::[0-9]+)?(?![A-Za-z0-9_@.:-])",
    re.IGNORECASE,
)


def explicit_target_origins(text: str, *, address_list: bool = False) -> dict[str, str]:
    """Return exact host/port scopes and their origins; never add subdomains or networks.

    Token recognition belongs to intake. Address/port meaning belongs to the
    shared scope contract. Paths remain in the original task description.
    """
    origins: dict[str, str] = {}
    candidates = (
        [item.strip() for item in text.split(",")]
        if address_list
        else [match.group() for match in _ADDRESS.finditer(text)]
    )
    for candidate in candidates:
        address = candidate.rstrip(".,;!?)>\"'")
        if not address or any(char.isspace() for char in address):
            if address_list:
                raise ValueError("대상 URL 또는 호스트 주소를 입력해 주세요.")
            continue
        is_url = "://" in address
        try:
            parsed = urlsplit(address if is_url else "//" + address)
        except ValueError:
            if address_list:
                raise ValueError("대상 주소 형식이 올바르지 않습니다.") from None
            continue
        if parsed.username or parsed.password:
            if address_list:
                raise ValueError("대상 주소에 로그인 정보를 포함할 수 없습니다.")
            continue
        if not is_url and (parsed.path or parsed.query or parsed.fragment):
            if address_list:
                raise ValueError("경로를 포함한 주소는 URL 형식으로 입력해 주세요.")
            continue
        target = UrlTarget(url=address) if is_url else HostTarget(host=address)
        identities, error = resolve_target_hostports(target)
        if error or not identities:
            if address_list:
                raise ValueError("대상 주소 또는 포트가 올바르지 않습니다.")
            continue
        hostname, port = identities[0]
        hostname = hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        host = f"{hostname}:{port}" if port is not None else hostname
        origins.setdefault(host, f"{parsed.scheme if is_url else 'http'}://{host}")
    return origins
