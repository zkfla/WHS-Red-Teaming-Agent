"""Offline audit boundaries; no DB fetch, provider credentials or upstream install."""

from __future__ import annotations

import hashlib
import os
import time

import pytest
from pydantic import ValidationError

from redteam.capability import vdb_snapshot_audit as audit
from redteam.capability.servers.vdb_readonly import VDBStatus


def spec_for(root, **changes):
    for name in audit.SNAPSHOT_FILES:
        (root / name).write_bytes(name.encode())
    values = {
        "policy": {
            "root": str(root),
            "asset_ref": "vdb-test",
            "engagement_id": "eng-test",
            "run_id": "run-test",
            "synthetic": True,
        },
        "allow_synthetic": True,
        "expected_sha256": {
            n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in audit.SNAPSHOT_FILES
        },
        "checks": [
            {
                "kind": "cve",
                "query": "CVE-2026-900001",
                "expected_advisory_ids": ["CVE-2026-900001"],
            },
            {"kind": "cve", "query": "CVE-2026-900099", "expected_advisory_ids": []},
        ],
    }
    values.update(changes)
    return audit.SnapshotAuditSpec.model_validate(values)


class FakeReader:
    changes: dict[str, object] = {}
    calls = 0

    def __init__(self, policy):
        self.policy = policy

    def status(self, **kwargs):
        return VDBStatus(
            asset_ref=self.policy.asset_ref,
            state="ready",
            freshness="fresh",
            completeness="full",
            metadata_coverage="full",
            synthetic=self.policy.synthetic,
        ).model_copy(update=self.changes)

    def lookup(self, kind, query, limit, **kwargs):
        from redteam.capability.servers.vdb_readonly import VDBLookup, VDBMatch

        type(self).calls += 1
        return VDBLookup(
            database=self.status(),
            query=query,
            matches=[VDBMatch(advisory_id=query)] if query.endswith("900001") else [],
            truncated=False,
            interpretation="knowledge_matches",
        )


@pytest.fixture
def fake(monkeypatch):
    FakeReader.calls, FakeReader.changes = 0, {}
    monkeypatch.setattr(audit, "VDBReader", FakeReader)
    return FakeReader


def test_synthetic_pass_cannot_claim_operational_activation(tmp_path, fake):
    spec = spec_for(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    report = audit.audit_snapshot(spec)
    assert report["local_validation_passed"] and report["snapshot_unchanged"]
    assert not report["operating_snapshot_candidate"]
    assert not report["activated"] and not report["live_llm_tested"]
    assert not report["upstream_source_revision_verified"]
    assert fake.calls == 2
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert str(tmp_path) not in str(report)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "missing"},
        {"freshness": "stale"},
        {"freshness": "unknown"},
        {"completeness": "partial"},
        {"metadata_coverage": "partial"},
    ],
)
def test_unready_snapshot_does_not_query_or_pass(tmp_path, fake, changes):
    spec = spec_for(tmp_path)
    fake.changes = changes
    report = audit.audit_snapshot(spec)
    assert not report["local_validation_passed"] and report["checks"] == []
    assert fake.calls == 0


@pytest.mark.parametrize("allow", [False, True])
def test_observed_synthetic_source_cannot_be_relabelled_operating(tmp_path, fake, allow):
    spec = spec_for(tmp_path)
    spec = spec.model_copy(
        update={
            "policy": spec.policy.model_copy(update={"synthetic": False}),
            "allow_synthetic": allow,
        }
    )
    fake.changes = {"sources": ["synthetic"]}
    report = audit.audit_snapshot(spec)
    assert report["synthetic"] and not report["operating_snapshot_candidate"]
    assert report["local_validation_passed"] is allow
    assert fake.calls == (2 if allow else 0)


@pytest.mark.parametrize("mutation", ["digest", "size", "symlink", "hardlink", "fifo", "wal"])
def test_rejects_artifact_boundary_before_backend(tmp_path, fake, mutation):
    spec = spec_for(tmp_path)
    path = tmp_path / "data.vdb7"
    if mutation == "digest":
        path.write_bytes(b"tampered")
    elif mutation == "size":
        spec = spec.model_copy(update={"max_snapshot_bytes": 1})
    elif mutation == "wal":
        # A dangling link must not hide an unfinished SQLite sidecar.
        (tmp_path / "data.vdb7-wal").symlink_to(tmp_path / "absent")
    else:
        path.rename(tmp_path / "original")
        if mutation == "symlink":
            path.symlink_to(tmp_path / "original")
        elif mutation == "hardlink":
            os.link(tmp_path / "original", path)
        else:
            os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        audit.audit_snapshot(spec)
    assert fake.calls == 0


def test_digest_rechecked_after_query(tmp_path, monkeypatch, fake):
    spec = spec_for(tmp_path)
    lookup = fake.lookup

    def modify(self, *args, **kwargs):
        (tmp_path / "vdb.meta").write_bytes(b"changed")
        return lookup(self, *args, **kwargs)

    monkeypatch.setattr(fake, "lookup", modify)
    with pytest.raises(ValueError, match="expected_digest_mismatch"):
        audit.audit_snapshot(spec)


def test_actual_empty_answer_is_not_a_pass_for_positive_control(tmp_path, monkeypatch, fake):
    spec = spec_for(tmp_path)
    lookup = fake.lookup

    def no_matches(self, *args, **kwargs):
        return lookup(self, *args, **kwargs).model_copy(update={"matches": []})

    monkeypatch.setattr(fake, "lookup", no_matches)
    assert not audit.audit_snapshot(spec)["local_validation_passed"]


def test_latency_budget_and_truncation_are_failure(tmp_path, monkeypatch, fake):
    spec = spec_for(tmp_path, max_lookup_seconds=1e-12)
    assert not audit.audit_snapshot(spec)["local_validation_passed"]
    lookup = fake.lookup

    def truncated(self, *args, **kwargs):
        return lookup(self, *args, **kwargs).model_copy(update={"truncated": True})

    monkeypatch.setattr(fake, "lookup", truncated)
    assert not audit.audit_snapshot(spec.model_copy(update={"max_lookup_seconds": 5.0}))[
        "local_validation_passed"
    ]


def test_global_hash_deadline_is_bounded(tmp_path):
    spec = spec_for(tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        audit._snapshot(spec, time.monotonic() - 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_synthetic": False},
        {"expected_sha256": {}},
        {"max_snapshot_bytes": True},
        {"max_audit_seconds": 0},
        {"max_lookup_seconds": float("nan")},
        {"checks": [{"kind": "cve", "query": "CVE-2026-900001", "expected_advisory_ids": []}] * 2},
    ],
)
def test_requires_reviewed_manifest_explicit_resources_and_both_controls(tmp_path, changes):
    with pytest.raises(ValidationError):
        spec_for(tmp_path, **changes)


def test_cli_error_does_not_echo_spec_contents(tmp_path, monkeypatch, capsys):
    path = tmp_path / "invalid.json"
    path.write_text('{"secret":"not-for-logs"}')
    monkeypatch.setattr("sys.argv", ["audit", "--spec", str(path)])
    assert audit.main() == 1
    output = capsys.readouterr().out
    assert "not-for-logs" not in output and str(path) not in output
    assert "vdb_snapshot_audit_failed" in output
