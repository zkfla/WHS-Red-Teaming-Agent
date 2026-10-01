"""Real stdio/manifest tests; optional pinned AppThreat backend on a synthetic snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from copy import deepcopy

import pytest

from redteam.capability.bootstrap import open_mcp_tool_gateway
from redteam.capability.vdb_manifest import VDB_EXTENDED_TOOLS, validate_vdb_descriptor
from redteam.core.ids import ToolId
from tests.integration.test_vdb_readonly_mcp import (
    call as basic_call,
)
from tests.integration.test_vdb_readonly_mcp import (
    configure,
    snapshot,
)
from tests.integration.test_vdb_readonly_mcp import (
    seeded as seeded,
)
from tests.integration.test_vdb_readonly_mcp import (
    test_candidate_control_and_actual_vdb_lookup as candidate_control_check,
)

PURL = "pkg:npm/redteam-synthetic-demo@1.5.0"
CPE = "cpe:2.3:a:npm:redteam-synthetic-demo:1.5.0:*:*:*:*:*:*:*"
GHSA = "GHSA-2345-6789-cfgh"


def call(name, **args):
    return basic_call("vdb_status" if name == "vdb_status" else "search_by_cve", **args).model_copy(
        update={"tool_id": ToolId(f"analysis.{name}")}
    )


def extended(monkeypatch, tmp_path, *, initialized=False):
    policy_path = tmp_path / "policy.json"
    if not initialized:
        configure(monkeypatch, tmp_path)
    policy = json.loads(policy_path.read_text())
    sbom_root = tmp_path / "sboms"
    sbom_root.mkdir(mode=0o700)
    sbom = sbom_root / "approved.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [
                    {"bom-ref": "demo", "purl": PURL},
                    {"bom-ref": "patched", "purl": "pkg:npm/redteam-synthetic-demo@2.0.0"},
                    {"bom-ref": "unknown", "name": "version-not-observed"},
                ],
            }
        )
    )
    policy.update(
        sbom_root=str(sbom_root),
        sboms=[
            {
                "ref": "approved-sbom",
                "path": "approved.json",
                "sha256": hashlib.sha256(sbom.read_bytes()).hexdigest(),
            }
        ],
    )
    policy_path.write_text(json.dumps(policy))
    args = json.loads(os.environ["REDTEAM_MCP_VDB_TEST_ARGS_JSON"])
    args.extend(["--profile", "vdb_readonly_extended"])
    monkeypatch.setenv("REDTEAM_MCP_VDB_TEST_ARGS_JSON", json.dumps(args))
    monkeypatch.setenv("REDTEAM_MCP_VDB_TEST_PROFILE", "vdb_readonly_extended")
    monkeypatch.setenv(
        "REDTEAM_MCP_VDB_TEST_ALLOWED_TOOLS_JSON", json.dumps(list(VDB_EXTENDED_TOOLS))
    )
    monkeypatch.setenv(
        "REDTEAM_MCP_VDB_TEST_TOOL_OPERATIONS_JSON",
        json.dumps({name: [spec.operation] for name, spec in VDB_EXTENDED_TOOLS.items()}),
    )
    return sbom


async def test_extended_real_stdio_discovery_and_nested_schema_drift(monkeypatch, tmp_path):
    extended(monkeypatch, tmp_path)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        tools = gateway.list_tools_for_action("analysis", "static_analysis")
        assert {str(tool.tool_id) for tool in tools} == {
            f"analysis.{name}" for name in VDB_EXTENDED_TOOLS if name != "vdb_status"
        }
        for name in VDB_EXTENDED_TOOLS:
            descriptor = gateway.get(ToolId(f"analysis.{name}"))
            validate_vdb_descriptor(descriptor, "vdb-test", "vdb_readonly_extended")
        for name, field, key, value in (
            ("search_packages", "purls", "maxItems", 10000),
            ("search_by_ghsa", "ghsa_id", "pattern", ".*"),
            ("search_by_cpe_like", "sources", "maxItems", 100),
            ("search_sbom", "offset", "minimum", -1),
            ("search_sbom", "max_items", "maximum", 100),
            ("search_sbom", "sbom_ref", "pattern", ".*"),
        ):
            descriptor = deepcopy(gateway.get(ToolId(f"analysis.{name}")))
            descriptor.input_schema["properties"][field][key] = value
            with pytest.raises(ValueError):
                validate_vdb_descriptor(descriptor, "vdb-test", "vdb_readonly_extended")
        descriptor = gateway.get(ToolId("analysis.search_sbom"))
        assert descriptor.input_schema["properties"]["sbom_ref"]["enum"] == ["approved-sbom"]
        bad = await gateway.invoke(call("search_packages", purls=[]))
        assert not bad.success and bad.error.code == "vdb_invalid_arguments"
        for args in (
            {"purls": [PURL], "run_id": "other"},
            {"purls": [PURL], "asset_ref": "other"},
            {"purls": [PURL], "sources": ["nvd%"]},
        ):
            assert not (await gateway.invoke(call("search_packages", **args))).success
        no_db = await gateway.invoke(call("search_packages", purls=[PURL]))
        assert no_db.success and no_db.output["not_checked_items"] == 1
        assert no_db.output["database"]["state"] == "missing"
        assert not no_db.output["all_items_checked"]


@pytest.fixture
def extended_seeded(seeded, monkeypatch):
    extended(monkeypatch, seeded.parent, initialized=True)
    return seeded


async def test_ghsa_cpe_sources_and_batch_using_original_backend(extended_seeded):
    before = snapshot(extended_seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        ghsa = await gateway.invoke(call("search_by_ghsa", ghsa_id=GHSA))
        assert ghsa.success and len(ghsa.output["matches"]) == 1, ghsa
        match = ghsa.output["matches"][0]
        assert match["advisory_id"] == "CVE-2026-900001"
        assert match["match_basis"] == "advisory_alias" and match["description"] == "synthetic"
        assert ghsa.output["database"]["metadata_coverage"] == "full"
        assert not ghsa.output["target_vulnerability_confirmed"]
        cpe = await gateway.invoke(call("search_by_cpe_like", cpe=CPE))
        assert cpe.success and len(cpe.output["matches"]) == 3, cpe
        assert all(m["match_basis"] == "concrete_cpe" for m in cpe.output["matches"])
        patched = await gateway.invoke(
            call("search_by_cpe_like", cpe=CPE.replace(":1.5.0:", ":2.0.0:"))
        )
        assert patched.success and not patched.output["matches"], patched
        for invalid in (
            CPE.replace(":1.5.0:", ":*:"),
            CPE.replace(":1.5.0:", ":-:"),
            "npm:redteam-synthetic-demo:1.5.0",
            "cpe:2.3:a:npm:demo:1",
        ):
            assert not (await gateway.invoke(call("search_by_cpe_like", cpe=invalid))).success
        selected = await gateway.invoke(call("search_by_ghsa", ghsa_id=GHSA, sources=["synthetic"]))
        assert selected.success and selected.output["matches"]
        filtered = await gateway.invoke(call("search_by_ghsa", ghsa_id=GHSA, sources=["nvd"]))
        assert filtered.success and not filtered.output["matches"]
        assert filtered.output["requested_sources"] == ["nvd"]
        bulk = await gateway.invoke(
            call(
                "search_packages",
                purls=[
                    PURL,
                    "pkg:npm/redteam-synthetic-demo@2.0.0",
                    "pkg:npm/redteam-synthetic-demo",
                ],
                limit=2,
            )
        )
        assert bulk.success and bulk.output["checked_items"] == 2, bulk
        assert bulk.output["error_items"] == 1 and not bulk.output["all_items_checked"]
        assert bulk.output["items"][0]["lookup"]["truncated"]
        assert bulk.output["items"][2]["error_code"] == "vdb_versioned_purl_required"
        assert str(extended_seeded) not in json.dumps(bulk.output)
    assert snapshot(extended_seeded) == before


async def test_approved_sbom_actual_stdio_pages_and_digest_binding(extended_seeded):
    before = snapshot(extended_seeded)
    sbom = extended_seeded.parent / "sboms" / "approved.json"
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        first = await gateway.invoke(call("search_sbom", sbom_ref="approved-sbom", max_items=1))
        assert first.success and first.output["total_items"] == 3, first
        assert first.output["next_offset"] == 1 and first.output["not_checked_items"] == 2
        last = await gateway.invoke(call("search_sbom", sbom_ref="approved-sbom", offset=1))
        assert last.success and last.output["next_offset"] is None, last
        assert last.output["items"][0]["lookup"]["matches"] == []
        assert last.output["items"][1]["error_code"] == "vdb_component_purl_missing"
        assert not last.output["all_items_checked"]
        assert str(sbom) not in json.dumps(last.output)
        assert not (await gateway.invoke(call("search_sbom", sbom_ref="../other.json"))).success
        sbom.write_text("changed after approval")
        assert not (await gateway.invoke(call("search_sbom", sbom_ref="approved-sbom"))).success
    assert snapshot(extended_seeded) == before


@pytest.mark.parametrize("case", ["missing", "partial", "missing_text"])
async def test_metadata_gap_is_explicit_not_a_clean_empty_answer(extended_seeded, case):
    with sqlite3.connect(extended_seeded / "data.index.vdb7") as conn:
        if case == "missing":
            conn.execute("DELETE FROM cve_metadata")
        elif case == "partial":
            conn.execute("DELETE FROM cve_metadata WHERE cve_id='CVE-2026-900002'")
        else:
            conn.execute("DELETE FROM cve_metadata_text")
    before = snapshot(extended_seeded)
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        status = await gateway.invoke(call("vdb_status"))
        assert status.success and status.output["metadata_coverage"] != "full"
        assert not (await gateway.invoke(call("search_by_ghsa", ghsa_id=GHSA))).success
        bulk = await gateway.invoke(call("search_packages", purls=[PURL], sources=["synthetic"]))
        assert bulk.success and bulk.output["error_items"] == 1
        assert bulk.output["items"][0]["error_code"] == "vdb_metadata_coverage_gap"
        # Basic index-only package lookup remains available without metadata.
        basic = await gateway.invoke(call("search_by_purl_like", purl=PURL))
        assert basic.success and basic.output["matches"]
    assert snapshot(extended_seeded) == before


async def test_near_alias_is_rejected_not_false_positive(extended_seeded):
    with sqlite3.connect(extended_seeded / "data.index.vdb7") as conn:
        conn.execute("UPDATE cve_metadata_text SET aliases=?", (GHSA + "-not-exact",))
    async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False) as gateway:
        assert not (await gateway.invoke(call("search_by_ghsa", ghsa_id=GHSA))).success


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("search_by_ghsa", {"ghsa_id": GHSA}),
        ("search_by_cpe_like", {"cpe": CPE}),
        ("search_packages", {"purls": [PURL]}),
        ("search_sbom", {"sbom_ref": "approved-sbom"}),
    ],
)
async def test_extended_profile_preserves_candidate_control_boundary(
    extended_seeded,
    tool_name,
    arguments,
):
    await candidate_control_check(extended_seeded, tool_name=tool_name, arguments=arguments)


async def test_operator_profile_mismatch_fails_discovery(monkeypatch, tmp_path):
    extended(monkeypatch, tmp_path)
    args = json.loads(os.environ["REDTEAM_MCP_VDB_TEST_ARGS_JSON"])
    monkeypatch.setenv("REDTEAM_MCP_VDB_TEST_ARGS_JSON", json.dumps(args[:-2]))
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy.pop("sboms")
    policy.pop("sbom_root")
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(Exception) as caught:
        async with open_mcp_tool_gateway(["vdb-test"], health_refresh_enabled=False):
            pytest.fail("basic server must not satisfy the extended allowlist")

    def leaves(error):
        children = getattr(error, "exceptions", None)
        return [leaf for child in children for leaf in leaves(child)] if children else [error]

    assert any("allowlist does not match" in str(error) for error in leaves(caught.value))
