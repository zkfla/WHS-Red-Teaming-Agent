"""Operator audit against the real pinned VDB backend, small synthetic DB only."""

import json
import os
import subprocess

from tests.integration.test_vdb_readonly_mcp import seeded as seeded  # noqa: F401
from tests.integration.test_vdb_readonly_mcp import snapshot


def test_real_original_backend_offline_staging_audit(seeded, tmp_path):
    spec_path = tmp_path / "audit.json"
    before = snapshot(seeded)
    spec_path.write_text(
        json.dumps(
            {
                "policy": {
                    "root": str(seeded),
                    "asset_ref": "vdb-test",
                    "engagement_id": "eng-test",
                    "run_id": "run-test",
                    "synthetic": True,
                },
                "allow_synthetic": True,
                "expected_sha256": before,
                "checks": [
                    {
                        "kind": "cve",
                        "query": "CVE-2026-900001",
                        "expected_advisory_ids": ["CVE-2026-900001"],
                    },
                    {
                        "kind": "ghsa",
                        "query": "GHSA-2345-6789-cfgh",
                        "expected_advisory_ids": ["CVE-2026-900001"],
                    },
                    {
                        "kind": "purl",
                        "query": "pkg:npm/redteam-synthetic-demo@2.0.0",
                        "expected_advisory_ids": [],
                    },
                ],
            }
        )
    )
    result = subprocess.run(
        [
            os.environ["REDTEAM_VDB_TEST_PYTHON"],
            "-m",
            "redteam.capability.vdb_snapshot_audit",
            "--spec",
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["local_validation_passed"] and len(report["checks"]) == 3
    assert report["database"]["data_rows"] == 3
    assert not report["operating_snapshot_candidate"] and not report["activated"]
    assert snapshot(seeded) == before
    assert str(seeded) not in result.stdout

    # A mismatched expected artifact must fail before database queries.
    data = json.loads(spec_path.read_text())
    data["expected_sha256"]["data.vdb7"] = "0" * 64
    spec_path.write_text(json.dumps(data))
    failed = subprocess.run(
        [
            os.environ["REDTEAM_VDB_TEST_PYTHON"],
            "-m",
            "redteam.capability.vdb_snapshot_audit",
            "--spec",
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert failed.returncode == 1
    assert "vdb_snapshot_audit_failed" in failed.stdout
    assert snapshot(seeded) == before
