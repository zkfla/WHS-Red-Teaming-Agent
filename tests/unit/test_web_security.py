"""Loopback-only access rules: bind address, peer, Host, Origin and write proof."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from redteam.app.web.security import (
    CLIENT_HEADER,
    GuardMiddleware,
    GuardPolicy,
    normalize_origin,
    validate_bind_host,
)

PORT = 8787
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://127.0.0.1:{PORT}"
WRITE_HEADERS = {
    "Host": HOST,
    "Origin": ORIGIN,
    "Content-Type": "application/json",
    CLIENT_HEADER: "web",
}


def make_client(**policy_overrides) -> TestClient:
    async def ok(_):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/x", ok, methods=["GET", "POST", "PUT"])])
    policy = GuardPolicy.for_loopback(PORT)
    policy = GuardPolicy(
        policy.allowed_hosts,
        policy.allowed_origins,
        extra_peers=frozenset({"testclient"}),
        **policy_overrides,
    )
    app.add_middleware(GuardMiddleware, policy=policy)
    return TestClient(app, base_url=f"http://{HOST}")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_addresses_are_accepted(host):
    assert validate_bind_host(host) in {"127.0.0.1", "::1"}


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.0.10", "example.com", "", "10.0.0.1"])
def test_non_loopback_and_wildcard_bind_addresses_are_refused(host):
    with pytest.raises(ValueError):
        validate_bind_host(host)


@pytest.mark.parametrize("origin", ["*", "http://*.example", "ftp://x", "http://x/path",
                                    "http://u:p@x", "http://x?q=1", "x"])
def test_origin_must_be_exact(origin):
    with pytest.raises(ValueError):
        normalize_origin(origin)


def test_origin_is_normalized_without_trailing_slash():
    assert normalize_origin("HTTP://Localhost:8080/") == "http://localhost:8080"


def test_get_from_allowed_host_succeeds_and_sets_hardening_headers():
    response = make_client().get("/x", headers={"Host": HOST})
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_unknown_host_is_refused_against_dns_rebinding():
    response = make_client().get("/x", headers={"Host": "evil.example:8787"})
    assert response.status_code == 403
    assert response.json() == {"error": "host_not_allowed"}


def test_unknown_peer_is_refused():
    async def ok(_):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/x", ok)])
    policy = GuardPolicy.for_loopback(PORT)
    app.add_middleware(GuardMiddleware, policy=policy)
    # Without the test peer allowance the synthetic client is not loopback.
    response = TestClient(app, base_url=f"http://{HOST}").get("/x")
    assert response.status_code == 403
    assert response.json() == {"error": "peer_not_allowed"}


def test_foreign_origin_is_refused_even_for_reads():
    response = make_client().get("/x", headers={"Host": HOST, "Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json() == {"error": "origin_not_allowed"}


def test_write_with_all_proofs_is_accepted():
    response = make_client().post("/x", content=b"{}", headers=WRITE_HEADERS)
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("drop", "code"),
    [("Origin", "origin_required"), (CLIENT_HEADER, "client_header_required"),
     ("Content-Type", "json_required")],
)
def test_write_missing_any_proof_is_refused(drop, code):
    headers = {k: v for k, v in WRITE_HEADERS.items() if k != drop}
    response = make_client().post("/x", content=b"{}", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": code}


def test_write_with_form_content_type_is_refused():
    headers = {**WRITE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    response = make_client().post("/x", content=b"a=b", headers=headers)
    assert response.json() == {"error": "json_required"}


def test_extra_origin_is_allowed_only_when_configured_exactly():
    policy = GuardPolicy.for_loopback(PORT, extra_origins=("http://localhost:8080",))
    assert "http://localhost:8080" in policy.allowed_origins
    assert "http://localhost:8081" not in policy.allowed_origins


def test_ipv4_mapped_loopback_peer_is_allowed_and_others_are_not():
    policy = GuardPolicy.for_loopback(PORT)
    assert policy.peer_allowed("127.0.0.1")
    assert policy.peer_allowed("::1")
    assert policy.peer_allowed("::ffff:127.0.0.1")
    assert not policy.peer_allowed("192.168.1.5")
    assert not policy.peer_allowed("not-an-ip")
    assert not policy.peer_allowed(None)
