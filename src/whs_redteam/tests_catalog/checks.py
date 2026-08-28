"""Pure analysis functions for Day 3 security observations."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from whs_redteam.models import Observation, ObservationVerdict, Severity
from whs_redteam.sanitizer import sanitize_url
from whs_redteam.tools.http_client import SafeHttpResponse
from whs_redteam.tools.response_diff import compare_responses

HTML_SECURITY_HEADERS = {
    "content-security-policy": "Define a restrictive Content-Security-Policy.",
    "x-content-type-options": "Set X-Content-Type-Options to nosniff.",
    "referrer-policy": "Set an explicit Referrer-Policy.",
}
INFORMATION_PATTERNS = {
    "python_traceback": re.compile(r"Traceback \(most recent call last\)", re.I),
    "java_stack_trace": re.compile(r"(?:java\.|javax\.)[\w.$]+Exception", re.I),
    "node_stack_trace": re.compile(r"\bat\s+[\w.$<>]+\s*\([^\n]+:\d+:\d+\)"),
    "sql_error": re.compile(
        r"(?:SQL syntax.*MySQL|PostgreSQL.*ERROR|SQLite(?:3)?::|ORA-\d{4,5})",
        re.I,
    ),
}


def security_headers(response: SafeHttpResponse) -> Observation:
    content_type = response.headers.get("content-type", "").lower()
    required = dict(HTML_SECURITY_HEADERS) if "text/html" in content_type else {
        "x-content-type-options": HTML_SECURITY_HEADERS["x-content-type-options"]
    }
    missing = [name for name in required if name not in response.headers]
    if "text/html" in content_type:
        csp = response.headers.get("content-security-policy", "").lower()
        if "x-frame-options" not in response.headers and "frame-ancestors" not in csp:
            missing.append("clickjacking-protection")
            required["clickjacking-protection"] = (
                "Set X-Frame-Options or a CSP frame-ancestors directive."
            )
    parsed = urlsplit(response.url)
    if (
        parsed.scheme == "https"
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and "strict-transport-security" not in response.headers
    ):
        missing.append("strict-transport-security")
        required["strict-transport-security"] = (
            "Set an appropriate Strict-Transport-Security policy."
        )
    if missing:
        remediation = " ".join(required[name] for name in missing)
        return Observation(
            test_id="WEB-HEADERS-001",
            category="security_headers",
            target_url=sanitize_url(response.url),
            verdict=ObservationVerdict.FAIL,
            severity=Severity.LOW,
            title="Recommended security headers are missing",
            summary=f"Missing headers: {', '.join(missing)}",
            evidence={"status_code": response.status_code, "missing_headers": missing},
            remediation=remediation,
        )
    return Observation(
        test_id="WEB-HEADERS-001",
        category="security_headers",
        target_url=sanitize_url(response.url),
        verdict=ObservationVerdict.PASS,
        severity=Severity.INFORMATIONAL,
        title="Required security headers are present",
        summary="The deterministic header policy passed for this content type.",
        evidence={"status_code": response.status_code},
    )


def cookie_attributes(response: SafeHttpResponse) -> Observation:
    cookies = response.headers.get_list("set-cookie")
    insecure: list[dict[str, str | list[str]]] = []
    for raw_cookie in cookies:
        parts = [part.strip() for part in raw_cookie.split(";")]
        name = parts[0].split("=", 1)[0]
        attributes = {part.split("=", 1)[0].lower() for part in parts[1:]}
        missing = [
            attribute
            for attribute in ("httponly", "samesite")
            if attribute not in attributes
        ]
        if urlsplit(response.url).scheme == "https" and "secure" not in attributes:
            missing.append("secure")
        if missing:
            insecure.append({"cookie_name": name, "missing_attributes": missing})
    if insecure:
        return Observation(
            test_id="WEB-COOKIE-001",
            category="cookie_attributes",
            target_url=sanitize_url(response.url),
            verdict=ObservationVerdict.FAIL,
            severity=Severity.MEDIUM,
            title="Cookie security attributes are incomplete",
            summary=f"{len(insecure)} cookie(s) are missing recommended attributes.",
            evidence={"cookies": insecure},
            remediation="Apply HttpOnly and SameSite to session cookies, and Secure over HTTPS.",
        )
    return Observation(
        test_id="WEB-COOKIE-001",
        category="cookie_attributes",
        target_url=sanitize_url(response.url),
        verdict=ObservationVerdict.PASS,
        severity=Severity.INFORMATIONAL,
        title="No insecure response cookies were observed",
        summary="No Set-Cookie header was present or all observed cookies passed the policy.",
        evidence={"cookie_count": len(cookies)},
    )


def cors_configuration(
    response: SafeHttpResponse,
    probe_origin: str,
    preflight: SafeHttpResponse | None = None,
) -> Observation:
    allow_origin = response.headers.get("access-control-allow-origin")
    credentials = response.headers.get("access-control-allow-credentials", "").lower() == "true"
    preflight_origin = (
        preflight.headers.get("access-control-allow-origin") if preflight is not None else None
    )
    reflected = allow_origin == probe_origin or preflight_origin == probe_origin
    vary_origin = "origin" in {
        item.strip().lower() for item in response.headers.get("vary", "").split(",")
    }
    if reflected:
        severity = Severity.HIGH if credentials else Severity.MEDIUM
        return Observation(
            test_id="WEB-CORS-001",
            category="cors_configuration",
            target_url=sanitize_url(response.url),
            verdict=ObservationVerdict.FAIL,
            severity=severity,
            title="Arbitrary Origin appears to be trusted",
            summary="The response allowed the untrusted probe Origin.",
            evidence={
                "allow_origin": allow_origin,
                "allow_credentials": credentials,
                "probe_origin": probe_origin,
                "preflight_allow_origin": preflight_origin,
                "vary_origin": vary_origin,
            },
            remediation=(
                "Use an explicit Origin allowlist, avoid reflecting untrusted origins, "
                "and emit Vary: Origin for dynamic policies."
            ),
        )
    if allow_origin == "*" and credentials:
        return Observation(
            test_id="WEB-CORS-001",
            category="cors_configuration",
            target_url=sanitize_url(response.url),
            verdict=ObservationVerdict.INFO,
            severity=Severity.INFORMATIONAL,
            title="Invalid CORS wildcard and credentials combination",
            summary=(
                "The response advertised wildcard Origin access and credentials; browsers "
                "normally reject credentialed access for this combination."
            ),
            evidence={"allow_origin": "*", "allow_credentials": True},
            remediation=(
                "Remove credentials or replace the wildcard with a strict Origin allowlist."
            ),
        )
    return Observation(
        test_id="WEB-CORS-001",
        category="cors_configuration",
        target_url=sanitize_url(response.url),
        verdict=ObservationVerdict.PASS,
        severity=Severity.INFORMATIONAL,
        title="Untrusted Origin was not reflected",
        summary="The safe CORS probe did not observe arbitrary Origin reflection.",
        evidence={
            "allow_origin": allow_origin,
            "allow_credentials": credentials,
            "preflight_allow_origin": preflight_origin,
            "vary_origin": vary_origin,
        },
    )


def information_exposure(response: SafeHttpResponse) -> Observation:
    matches = [
        name
        for name, pattern in INFORMATION_PATTERNS.items()
        if pattern.search(response.text)
    ]
    disclosed_headers = {
        name: response.headers[name]
        for name in ("server", "x-powered-by")
        if name in response.headers
    }
    if matches:
        return Observation(
            test_id="WEB-INFO-001",
            category="information_exposure",
            target_url=sanitize_url(response.url),
            verdict=ObservationVerdict.FAIL,
            severity=Severity.MEDIUM,
            title="Detailed error information was exposed",
            summary=f"Matched error signatures: {', '.join(matches)}",
            evidence={"matched_signatures": matches, "disclosed_headers": disclosed_headers},
            remediation="Return generic client errors and retain detailed diagnostics server-side.",
        )
    verdict = ObservationVerdict.INFO if disclosed_headers else ObservationVerdict.PASS
    return Observation(
        test_id="WEB-INFO-001",
        category="information_exposure",
        target_url=sanitize_url(response.url),
        verdict=verdict,
        severity=Severity.INFORMATIONAL,
        title="Technology disclosure review",
        summary=(
            "Technology-identifying response headers were observed."
            if disclosed_headers
            else "No configured error signatures or technology headers were observed."
        ),
        evidence={"disclosed_headers": disclosed_headers},
        remediation=(
            "Minimize unnecessary Server and X-Powered-By details."
            if disclosed_headers
            else None
        ),
    )


def transport_security(url: str) -> Observation:
    parsed = urlsplit(url)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    failed = parsed.scheme != "https" and not local
    return Observation(
        test_id="WEB-TRANSPORT-001",
        category="transport_security",
        target_url=sanitize_url(url),
        verdict=ObservationVerdict.FAIL if failed else ObservationVerdict.PASS,
        severity=Severity.MEDIUM if failed else Severity.INFORMATIONAL,
        title="HTTPS transport policy",
        summary=(
            "A non-local target is using plaintext HTTP."
            if failed
            else "The target uses HTTPS or is an explicitly local test target."
        ),
        evidence={"scheme": parsed.scheme, "local_target": local},
        remediation="Serve non-local applications exclusively over HTTPS." if failed else None,
    )


def actor_response_comparison(
    baseline: SafeHttpResponse,
    actor_response: SafeHttpResponse,
    actor_id: str,
) -> Observation:
    comparison = compare_responses(baseline, actor_response)
    changed = not (
        comparison["status_equal"]
        and comparison["content_type_equal"]
        and comparison["body_equal"]
    )
    return Observation(
        test_id=f"WEB-ACTOR-DIFF-001:{actor_id}",
        category="actor_response_comparison",
        target_url=sanitize_url(actor_response.url),
        verdict=ObservationVerdict.INFO,
        severity=Severity.INFORMATIONAL,
        title=f"Response comparison for actor {actor_id}",
        summary=(
            "The authenticated actor response differs from the anonymous baseline."
            if changed
            else "The authenticated actor response matches the anonymous baseline."
        ),
        evidence={"actor_id": actor_id, "response_changed": changed, **comparison},
    )
