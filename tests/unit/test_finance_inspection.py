"""Response observations preserve scope and omit credential-bearing values."""

import json

import pytest

from redteam.capability.servers.config import TargetProfile
from redteam.capability.servers.inspection import html_observations, security_observations
from redteam.capability.servers.network import CapturedResponse, ToolInputError


def test_cookie_flags_do_not_merge_expires_or_expose_values() -> None:
    result = security_observations(CapturedResponse(
        200,
        {"cache-control": "private, no-cache", "content-security-policy-report-only": "secret"},
        b"private account body",
        (
            "session=private-token; Expires=Wed, 21 Oct 2030 07:28:00 GMT; "
            "Secure; HttpOnly; SameSite=Lax",
            "preference=private-value; SameSite=None",
            "broken cookie",
        ),
    ))
    assert result["set_cookie_header_count"] == 3
    assert result["cookies"][0]["secure"] and result["cookies"][0]["http_only"]
    assert result["cookies"][0]["persistent"]
    assert result["cookies"][1]["same_site"] == "none"
    assert not result["cookies"][1]["secure"]
    assert result["cookies"][2] == {"parse_status": "unknown"}
    assert not result["cache_control_no_store"]  # no-cache is not no-store
    assert not result["headers_present"]["content-security-policy"]
    assert result["headers_present"]["content-security-policy-report-only"]
    assert "private-token" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_cookie_observations_are_bounded_and_explicitly_truncated() -> None:
    result = security_observations(CapturedResponse(200, {}, b"", ("s=secret; Secure",) * 60))
    assert len(result["cookies"]) == 50
    assert result["cookies_truncated"] and result["set_cookie_header_count"] == 60


def profile() -> TargetProfile:
    return TargetProfile(origin="https://bank.test", allowed_paths=["/", "/account"])


def test_html_does_not_expose_query_form_values_or_external_urls() -> None:
    body = b'''<a href="/account?token=secret#private">private text</a>
        <form action="/unapproved" method="post"><input value="account-secret"></form>
        <script src="/assets/sdk.js?v=secret"></script>
        <a href="https://elsewhere.test/secret">external</a>
        <a href="javascript:alert('secret')">script</a>
        <a href="https://user:secret@bank.test/">credential</a>
        <a href="http://[invalid">broken</a>'''
    result = html_observations(body, profile(), "/")
    assert [item["path"] for item in result["references"]] == [
        "/account", "/unapproved", "/assets/sdk.js",
    ]
    assert result["references"][0]["path_allowlisted"]
    assert not result["references"][1]["path_allowlisted"]
    assert result["references"][1]["method"] == "POST"
    assert result["omitted_references"] == 4
    assert not result["references_followed"]
    assert "secret" not in json.dumps(result) and "elsewhere" not in json.dumps(result)
    assert profile().allowed_paths == ["/", "/account"]


def test_html_external_base_is_not_misreported_as_local() -> None:
    result = html_observations(
        b'<base href="https://elsewhere.test/"><script src="sdk.js"></script><form></form>',
        profile(), "/account",
    )
    assert result["base_element_present"] and result["omitted_references"] == 1
    assert [item["path"] for item in result["references"]] == ["/account"]


def test_html_limits_and_korean_charset() -> None:
    result = html_observations(b'<a href="/account">x</a>' * 201, profile(), "/")
    assert len(result["references"]) == 200 and result["references_truncated"]
    korean = html_observations(
        '<a href="/계좌">계좌</a>'.encode("cp949"), profile(), "/", "text/html; charset=cp949",
    )
    assert korean["references"][0]["path"] == "/계좌"
    assert not korean["decoding_replacements"]
    with pytest.raises(ToolInputError, match="charset"):
        html_observations(b"html", profile(), "/", "text/html; charset=not-a-codec")


def test_invalid_base_has_fixed_error_message() -> None:
    with pytest.raises(ToolInputError, match="^invalid HTML base URL$"):
        html_observations(b'<base href="http://[secret">', profile(), "/")
