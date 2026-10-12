"""Bounded observations from one response; no secondary requests or vulnerability verdicts."""

from __future__ import annotations

from email.message import Message
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urljoin, urlsplit

from redteam.capability.servers.config import TargetProfile
from redteam.capability.servers.network import CapturedResponse, ToolInputError


def security_observations(response: CapturedResponse) -> dict[str, Any]:
    """Return fixed labels only, never cookie names/values or raw policy headers."""
    headers = response.headers
    cookies: list[dict[str, Any]] = []
    for raw in response.set_cookie_headers[:50]:
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            cookie.clear()
        if len(cookie) != 1:
            cookies.append({"parse_status": "unknown"})
            continue
        morsel = next(iter(cookie.values()))
        same_site = morsel["samesite"].lower()
        cookies.append({
            "parse_status": "parsed",
            "secure": bool(morsel["secure"]),
            "http_only": bool(morsel["httponly"]),
            "same_site": (
                same_site if same_site in {"strict", "lax", "none"}
                else "unspecified" if not same_site else "invalid"
            ),
            "domain_attribute_present": bool(morsel["domain"]),
            "persistent": bool(morsel["expires"] or morsel["max-age"]),
        })
    cache_directives = {
        item.strip().split("=", 1)[0].lower()
        for item in headers.get("cache-control", "").split(",")
    }
    return {
        "headers_present": {
            key: key in headers
            for key in (
                "strict-transport-security", "content-security-policy",
                "content-security-policy-report-only", "x-frame-options",
                "x-content-type-options", "referrer-policy", "permissions-policy",
            )
        },
        "cache_control_no_store": "no-store" in cache_directives,
        "content_type_nosniff": headers.get("x-content-type-options", "").lower() == "nosniff",
        "cookies": cookies,
        "set_cookie_header_count": len(response.set_cookie_headers),
        "cookies_truncated": len(response.set_cookie_headers) > 50,
        "raw_headers_omitted": True,
        "cookie_names_and_values_omitted": True,
        "verdict": "observation_only",
    }


class _PageReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str, str]] = []
        self.base: str | None = None
        self.truncated = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "base" and self.base is None and attributes.get("href") is not None:
            self.base = attributes["href"]
        key = {"a": "href", "form": "action", "script": "src", "link": "href"}.get(tag)
        if key is None or (key not in attributes and tag != "form"):
            return
        if len(self.references) >= 200:
            self.truncated = True
            return
        method = (attributes.get("method") or "GET").upper() if tag == "form" else "GET"
        self.references.append((tag, attributes.get(key) or "", method))


def html_observations(
    body: bytes, profile: TargetProfile, path: str, content_type: str = "text/html",
) -> dict[str, Any]:
    """Inventory static HTML references only. Never execute JS, submit forms, or fetch assets."""
    parser = _PageReferences()
    header = Message()
    header["content-type"] = content_type
    charset = header.get_content_charset() or "utf-8"
    if charset not in {"utf-8", "utf8", "euc-kr", "cp949", "ks_c_5601-1987"}:
        raise ToolInputError("HTML charset is not supported")
    decoded = body.decode(charset, errors="replace")
    parser.feed(decoded)
    parser.close()
    page_url = profile.origin + path
    try:
        base_url = urljoin(page_url, parser.base) if parser.base is not None else page_url
    except ValueError as exc:
        raise ToolInputError("invalid HTML base URL") from exc
    origin = urlsplit(profile.origin)

    def origin_key(url: Any) -> tuple[str, str | None, int]:
        return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)

    references = []
    omitted = 0
    for kind, raw, method in parser.references:
        try:
            # Missing/empty form actions resolve against the document, not <base>.
            url = urlsplit(page_url if kind == "form" and not raw else urljoin(base_url, raw))
            route = url.path or "/"
            if (
                url.scheme not in {"http", "https"}
                or origin_key(url) != origin_key(origin)
                or url.username is not None or url.password is not None
                or len(route) > 512
                or any(ord(c) < 32 or c in "%\\" for c in route)
                or "//" in route
            ):
                omitted += 1
                continue
        except ValueError:
            omitted += 1
            continue
        references.append({
            "kind": kind,
            "path": route,
            "method": method if method in {"GET", "POST"} else "UNKNOWN",
            "query_omitted": bool(url.query),
            "fragment_omitted": bool(url.fragment),
            "path_allowlisted": profile.allows_path(route),
        })
    return {
        "references": references,
        "reference_limit": 200,
        "references_truncated": parser.truncated,
        "omitted_references": omitted,
        "base_element_present": parser.base is not None,
        "decoding_replacements": "\ufffd" in decoded,
        "references_followed": False,
        "javascript_executed": False,
        "source_untrusted": True,
        "verdict": "observation_only",
    }
