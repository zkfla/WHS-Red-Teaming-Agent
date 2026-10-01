"""Extension boundaries need no real DB, credentials, download or financial target."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from redteam.capability.servers.vdb_extended import (
    VDBExtendedPolicy,
    VDBExtendedReader,
)
from redteam.capability.servers.vdb_readonly import VDBError, VDBLookup, VDBMatch, VDBStatus
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings
from redteam.capability.vdb_manifest import VDB_EXTENDED_TOOLS


def setup(tmp_path, **changes):
    root = tmp_path / "sboms"
    root.mkdir(mode=0o700)
    path = root / "approved.json"
    content = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "components": [
                {"bom-ref": "demo", "purl": "pkg:npm/synthetic@1.0.0"},
                {"bom-ref": "unknown", "name": "not-observed"},
            ],
        }
    ).encode()
    path.write_bytes(content)
    policy = VDBExtendedPolicy(
        asset_ref="vdb-test",
        engagement_id="eng-test",
        run_id="run-test",
        root=str(tmp_path / "db"),
        synthetic=True,
        sbom_root=str(root),
        sboms=[
            {
                "ref": "approved",
                "path": "approved.json",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        **changes,
    )
    return VDBExtendedReader(policy), path


def ready(reader, monkeypatch):
    status = VDBStatus(asset_ref="vdb-test", state="ready", synthetic=True)
    monkeypatch.setattr(reader, "status", lambda **kwargs: status)

    def lookup(kind, query, limit, **kwargs):
        if not query.startswith("pkg:") or "@" not in query:
            raise VDBError("vdb_versioned_purl_required")
        return VDBLookup(
            database=status,
            query=query,
            matches=[],
            truncated=False,
            interpretation="no_known_match_in_loaded_snapshot",
        )

    monkeypatch.setattr(reader, "lookup", lookup)
    return status


def test_batch_partial_failures_and_duplicate_inputs_preserve_positions(tmp_path, monkeypatch):
    reader, _ = setup(tmp_path)
    ready(reader, monkeypatch)
    result = reader.batch(["pkg:npm/test@1.0", "invalid", "pkg:npm/test@1.0"], 1, [])
    assert [item.index for item in result.items] == [0, 1, 2]
    assert [item.state for item in result.items] == ["checked", "error", "checked"]
    assert result.checked_items == 2 and result.error_items == 1
    assert not result.all_items_checked and not result.target_vulnerability_confirmed


def test_missing_db_and_deadline_are_not_checked(tmp_path, monkeypatch):
    reader, _ = setup(tmp_path)
    result = reader.batch(["pkg:npm/test@1.0"], 1, [])
    assert result.database.state == "missing"
    assert result.not_checked_items == 1
    assert result.items[0].lookup is None
    ready(reader, monkeypatch)
    result = reader.batch(["pkg:npm/test@1.0"], 1, [], deadline=time.monotonic() - 1)
    assert result.items[0].error_code == "vdb_batch_timeout"
    assert not result.all_items_checked


@pytest.mark.parametrize("queries", [[], ["pkg:npm/test@1"] * 26])
def test_batch_size_bounds(tmp_path, queries):
    reader, _ = setup(tmp_path)
    with pytest.raises(VDBError, match="vdb_invalid_batch_size"):
        reader.batch(queries, 1, [])


@pytest.mark.parametrize("sources", [["synthetic"] * 2, ["nvd%"], ["nvd\n"], ["x"] * 6])
def test_invalid_source_filter_is_not_silently_ignored(tmp_path, sources):
    reader, _ = setup(tmp_path)
    with pytest.raises(VDBError, match="vdb_invalid_sources"):
        reader.batch(["pkg:npm/test@1"], 1, sources)


def test_sbom_paging_records_unobserved_and_unqueried_components(tmp_path, monkeypatch):
    reader, path = setup(tmp_path)
    ready(reader, monkeypatch)
    first = reader.sbom("approved", 0, 1, 1, [])
    assert first.total_items == 2 and first.checked_items == 1
    assert first.next_offset == 1 and first.not_checked_items == 1
    assert first.truncated and not first.all_items_checked
    assert first.sbom_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    second = reader.sbom("approved", 1, 1, 1, [])
    assert second.items[0].index == 1 and second.next_offset is None
    assert second.items[0].error_code == "vdb_component_purl_missing"
    assert second.not_checked_items == 2  # full document, not just this page
    assert str(path) not in second.model_dump_json()


@pytest.mark.parametrize("ref", ["other", "../approved.json", "/etc/passwd", "https://invalid"])
def test_sbom_only_operator_refs(tmp_path, ref):
    reader, _ = setup(tmp_path)
    with pytest.raises(VDBError, match="vdb_sbom_not_approved"):
        reader.sbom(ref, 0, 1, 1, [])


@pytest.mark.parametrize("case", ["digest", "symlink", "hardlink", "large"])
def test_sbom_changed_linked_and_oversize_files_blocked(tmp_path, case):
    import os

    reader, path = setup(tmp_path)
    if case == "digest":
        path.write_text("changed")
    elif case in {"symlink", "hardlink"}:
        other = tmp_path / "outside.json"
        other.write_bytes(path.read_bytes())
        path.unlink()
        if case == "symlink":
            path.symlink_to(other)
        else:
            os.link(other, path)
    else:
        path.write_bytes(b" " * 64_001)
    with pytest.raises(VDBError, match="vdb_sbom_(digest_mismatch|access_denied)"):
        reader.sbom("approved", 0, 1, 1, [])


@pytest.mark.parametrize("offset,size", [(-1, 1), (True, 1), (2, 1), (0, 0), (0, 26)])
def test_sbom_page_bounds(tmp_path, offset, size):
    reader, _ = setup(tmp_path)
    with pytest.raises(VDBError, match="vdb_invalid_sbom_page"):
        reader.sbom("approved", offset, size, 1, [])


def test_nested_components_and_metadata_component_are_counted(tmp_path, monkeypatch):
    reader, path = setup(tmp_path)
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {"component": {"bom-ref": "root", "purl": "pkg:npm/root@1"}},
        "components": [{"bom-ref": "parent", "components": [{"bom-ref": "child"}]}],
    }
    path.write_text(json.dumps(bom))
    original = reader.extended_policy.model_dump()
    original["sboms"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    reader = VDBExtendedReader(VDBExtendedPolicy.model_validate(original))
    ready(reader, monkeypatch)
    result = reader.sbom("approved", 0, 25, 1, [])
    assert result.total_items == 3
    assert [item.component_ref for item in result.items] == ["root", "parent", "child"]
    assert result.not_checked_items == 2 and not result.all_items_checked


@pytest.mark.parametrize("change", ["traversal", "duplicate", "shared_root", "no_root"])
def test_policy_approval_boundaries(tmp_path, change):
    reader, _ = setup(tmp_path)
    values = reader.extended_policy.model_dump()
    if change == "traversal":
        values["sboms"][0]["path"] = "../outside.json"
    elif change == "duplicate":
        values["sboms"] = values["sboms"] * 2
    elif change == "shared_root":
        values["sbom_root"] = values["root"]
    else:
        values["sbom_root"] = None
    with pytest.raises((ValidationError, ValueError)):
        VDBExtendedPolicy.model_validate(values)


def test_aggregate_output_keeps_item_status_and_signals_evidence_truncation(tmp_path, monkeypatch):
    reader, _ = setup(tmp_path)
    status = ready(reader, monkeypatch)

    def lookup(kind, query, limit, **kwargs):
        return VDBLookup(
            database=status,
            query=query,
            interpretation="knowledge_matches",
            truncated=False,
            matches=[
                VDBMatch(
                    advisory_id="CVE-2026-900001",
                    references=["x" * 1024] * 20,
                )
                for _ in range(20)
            ],
        )

    monkeypatch.setattr(reader, "lookup", lookup)
    result = reader.batch(["pkg:npm/test@1"] * 10, 20, [])
    assert len(result.model_dump_json().encode()) <= 200_000
    assert len(result.items) == 10 and result.checked_items == 10
    assert result.truncated and any(item.lookup.truncated for item in result.items)
    assert all(item.lookup.interpretation == "knowledge_matches" for item in result.items)


def test_extended_profile_requires_its_exact_bindings(monkeypatch):
    for suffix, value in {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": "python",
        "PROFILE": "vdb_readonly_extended",
        "VDB_ASSET_REF": "vdb-test",
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": json.dumps(list(VDB_EXTENDED_TOOLS)),
        "TOOL_OPERATIONS_JSON": json.dumps(
            {name: [spec.operation] for name, spec in VDB_EXTENDED_TOOLS.items()}
        ),
    }.items():
        monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)
    assert MCPServerSettings.from_environment("vdb-test").profile == "vdb_readonly_extended"
    monkeypatch.setenv("REDTEAM_MCP_VDB_TEST_ALLOWED_TOOLS_JSON", '["vdb_status"]')
    with pytest.raises(MCPConfigurationError, match="exact allowlist"):
        MCPServerSettings.from_environment("vdb-test")


def test_query_deadline_is_passed_to_readonly_connections(tmp_path, monkeypatch):
    reader, _ = setup(tmp_path)
    seen = []

    @contextmanager
    def connections(deadline):
        seen.append(deadline)
        raise VDBError("vdb_test_failure")
        yield  # pragma: no cover

    monkeypatch.setattr(reader, "_connections", connections)
    deadline = time.monotonic() + 5
    with pytest.raises(VDBError, match="vdb_test_failure"):
        reader.lookup("purl", "pkg:npm/test@1", 1, deadline=deadline)
    assert seen == [deadline]


def test_batch_stops_new_queries_after_first_consumes_deadline(tmp_path, monkeypatch):
    reader, _ = setup(tmp_path)
    status = ready(reader, monkeypatch)
    clock = [1.0]
    calls = []
    monkeypatch.setattr("redteam.capability.servers.vdb_extended.time.monotonic", lambda: clock[0])

    def lookup(kind, query, limit, **kwargs):
        calls.append(query)
        clock[0] = 10.0
        return VDBLookup(
            database=status,
            query=query,
            matches=[],
            truncated=False,
            interpretation="no_known_match_in_loaded_snapshot",
        )

    monkeypatch.setattr(reader, "lookup", lookup)
    result = reader.batch(["pkg:npm/a@1", "pkg:npm/b@1", "pkg:npm/c@1"], 1, [], deadline=5.0)
    assert len(calls) == 1 and result.checked_items == 1 and result.not_checked_items == 2
    assert [item.error_code for item in result.items[1:]] == ["vdb_batch_timeout"] * 2


@pytest.mark.parametrize("query", ["GHSA-%", "GHSA-2345-6789-cfgh-extra", "ghsa-2345-6789-cfgh"])
def test_ghsa_input_is_exact_not_wildcard_or_suffix(tmp_path, query):
    reader, _ = setup(tmp_path)
    with pytest.raises(VDBError, match="vdb_invalid_ghsa"):
        reader.lookup("ghsa", query, 1)


@pytest.mark.parametrize("case", ["format", "components", "component_limit", "version"])
def test_sbom_structure_and_count_fail_closed(tmp_path, case):
    reader, path = setup(tmp_path)
    bom = json.loads(path.read_text())
    if case == "format":
        bom["bomFormat"] = "SPDX"
    elif case == "components":
        bom["components"] = "not a list"
    elif case == "version":
        bom["specVersion"] = "unknown"
    else:
        bom["components"] *= 101
    path.write_text(json.dumps(bom))
    values = reader.extended_policy.model_dump()
    values["sboms"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    reader = VDBExtendedReader(VDBExtendedPolicy.model_validate(values))
    with pytest.raises(VDBError, match="vdb_(invalid_sbom|sbom_component_limit)"):
        reader.sbom("approved", 0, 1, 1, [])
