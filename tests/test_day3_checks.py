import httpx

from whs_redteam.models import ObservationVerdict, Severity
from whs_redteam.tests_catalog.checks import (
    cookie_attributes,
    cors_configuration,
    information_exposure,
    security_headers,
    transport_security,
)
from whs_redteam.tools.http_client import SafeHttpResponse


def response(
    *,
    url: str = "https://example.test/",
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"ok",
) -> SafeHttpResponse:
    return SafeHttpResponse(
        url=url,
        status_code=200,
        headers=httpx.Headers(headers or []),
        body=body,
        elapsed_ms=1.0,
    )


def test_missing_html_security_headers_fail() -> None:
    result = security_headers(response(headers=[("content-type", "text/html")]))
    assert result.verdict is ObservationVerdict.FAIL
    assert "content-security-policy" in result.evidence["missing_headers"]


def test_insecure_cookie_is_reported_without_value() -> None:
    result = cookie_attributes(
        response(headers=[("set-cookie", "session=secret-value; Path=/")])
    )
    assert result.verdict is ObservationVerdict.FAIL
    assert result.severity is Severity.MEDIUM
    assert result.evidence["cookies"][0]["cookie_name"] == "session"
    assert "secret-value" not in str(result.evidence)


def test_arbitrary_credentialed_cors_reflection_is_high() -> None:
    origin = "https://cors-probe.invalid"
    result = cors_configuration(
        response(
            headers=[
                ("access-control-allow-origin", origin),
                ("access-control-allow-credentials", "true"),
            ]
        ),
        origin,
    )
    assert result.verdict is ObservationVerdict.FAIL
    assert result.severity is Severity.HIGH


def test_wildcard_credentials_is_informational_not_exploitable() -> None:
    result = cors_configuration(
        response(
            headers=[
                ("access-control-allow-origin", "*"),
                ("access-control-allow-credentials", "true"),
            ]
        ),
        "https://cors-probe.invalid",
    )
    assert result.verdict is ObservationVerdict.INFO
    assert result.severity is Severity.INFORMATIONAL


def test_preflight_origin_reflection_is_detected() -> None:
    origin = "https://cors-probe.invalid"
    result = cors_configuration(
        response(),
        origin,
        response(headers=[("access-control-allow-origin", origin)]),
    )
    assert result.verdict is ObservationVerdict.FAIL


def test_stack_trace_signature_is_reported() -> None:
    result = information_exposure(
        response(body=b"Traceback (most recent call last):\n  File app.py")
    )
    assert result.verdict is ObservationVerdict.FAIL
    assert "python_traceback" in result.evidence["matched_signatures"]


def test_plaintext_nonlocal_transport_fails_but_localhost_passes() -> None:
    assert transport_security("http://example.test").verdict is ObservationVerdict.FAIL
    assert transport_security("http://127.0.0.1:8000").verdict is ObservationVerdict.PASS
