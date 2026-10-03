"""Default tests need no VDB download/package, credentials or external server."""

from __future__ import annotations

from copy import deepcopy
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from redteam.capability.servers.vdb_readonly import VDBError, VDBPolicy, VDBReader
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings


def policy(tmp_path: Path, **changes) -> VDBPolicy:
    return VDBPolicy(
        root=str(tmp_path),
        asset_ref="vdb-test",
        engagement_id="eng-test",
        run_id="run-test",
        synthetic=True,
        **changes,
    )


def test_missing_status_does_not_materialize_db(tmp_path):
    reader = VDBReader(policy(tmp_path))
    assert reader.status().state == "missing"
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(VDBError, match="vdb_lookup_unavailable"):
        reader.lookup("cve", "CVE-2026-900001", 2)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("query", ["CVE-2026-%", "CVE-2026-1", "cve-2026-900001", "';DROP", "x\n"])
def test_cve_queries_fail_closed(tmp_path, query):
    with pytest.raises(VDBError, match="vdb_invalid"):
        VDBReader(policy(tmp_path)).lookup("cve", query, 1)


@pytest.mark.parametrize("limit", [0, -1, 51, True])
def test_limit_fail_closed(tmp_path, limit):
    with pytest.raises(VDBError, match="vdb_invalid_limit"):
        VDBReader(policy(tmp_path)).lookup("cve", "CVE-2026-900001", limit)


def test_root_symlink_blocked(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(VDBError, match="vdb_invalid_root"):
        VDBReader(policy(link))


def test_snapshot_links_blocked(tmp_path):
    target = tmp_path / "operator-db"
    target.write_bytes(b"x")
    (tmp_path / "data.vdb7").symlink_to(target)
    reader = VDBReader(policy(tmp_path))
    assert reader.status().state == "invalid"
    assert target.read_bytes() == b"x"


def test_context_and_bounded_source_text(tmp_path):
    reader = VDBReader(policy(tmp_path))
    reader.bind("vdb-test", "eng-test", "run-test")
    with pytest.raises(VDBError, match="vdb_context_mismatch"):
        reader.bind("vdb-test", "eng-other", "run-test")
    hit = {"cve_id": "CVE-2026-900001", "references": ["x" * 2000] * 50}
    original = deepcopy(hit)
    match = reader._match(hit)
    assert len(match.references) == 20
    assert len(match.references[0]) == 1024
    assert match.severity is None and match.source is None
    assert hit == original


def test_missing_backend_and_version_drift_are_explicit(tmp_path, monkeypatch):
    for name in ("data.vdb7", "data.index.vdb7"):
        (tmp_path / name).write_bytes(b"placeholder")
    reader = VDBReader(policy(tmp_path))

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("redteam.capability.servers.vdb_readonly.version", missing)
    assert reader.status().state == "backend_unavailable"
    monkeypatch.setattr("redteam.capability.servers.vdb_readonly.version", lambda name: "9.0.0")
    assert reader.status().state == "backend_version_mismatch"


def test_snapshot_hardlink_blocked(tmp_path):
    import os

    target = tmp_path / "operator-db"
    target.write_bytes(b"x")
    os.link(target, tmp_path / "data.vdb7")
    assert VDBReader(policy(tmp_path)).status().state == "invalid"


def configure(monkeypatch):
    values = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": "python",
        "PROFILE": "vdb_readonly",
        "VDB_ASSET_REF": "vdb-test",
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": '["vdb_status","search_by_cve","search_by_purl_like"]',
        "TOOL_OPERATIONS_JSON": '{"vdb_status":["response_analysis"],'
        '"search_by_cve":["static_analysis"],'
        '"search_by_purl_like":["static_analysis"]}',
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)


def test_profile_exact_allowlist_and_bindings(monkeypatch):
    configure(monkeypatch)
    settings = MCPServerSettings.from_environment("vdb-test")
    assert settings.vdb_asset_ref == "vdb-test"
    assert settings.workspace_asset_ref is None
    monkeypatch.setenv(
        "REDTEAM_MCP_VDB_TEST_ALLOWED_TOOLS_JSON",
        '["vdb_status","search_by_cve","search_by_purl_like","db_refresh"]',
    )
    with pytest.raises(MCPConfigurationError, match="exact allowlist"):
        MCPServerSettings.from_environment("vdb-test")


@pytest.mark.parametrize(
    "suffix,value", [("KIND", "web_api"), ("RISK_LEVEL", "low"), ("VDB_ASSET_REF", "/host/path")]
)
def test_profile_rejects_unsafe_configuration(monkeypatch, suffix, value):
    configure(monkeypatch)
    monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)
    with pytest.raises(MCPConfigurationError):
        MCPServerSettings.from_environment("vdb-test")
