"""Operator-only, offline, read-only VDB staging checks; never an Agent tool.

Expected digests and known positive/negative queries come from an independently
reviewed operator manifest. This module neither fetches nor activates a DB.
Run in the existing isolated AppThreat runtime, not the main Agent environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.capability.servers.vdb_readonly import VDBPolicy, VDBReader

SNAPSHOT_FILES = ("data.vdb7", "data.index.vdb7", "vdb.meta")


class SnapshotCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["cve", "purl", "ghsa", "cpe"]
    query: str = Field(min_length=1, max_length=512)
    expected_advisory_ids: tuple[str, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def bounded_public_identifiers(self) -> SnapshotCheck:
        if (
            any(ord(c) < 32 for c in self.query)
            or any(
                not re.fullmatch(r"(?:CVE-[0-9]{4}-[0-9]{4,10}|GHSA-[a-z0-9-]{14})", item)
                for item in self.expected_advisory_ids
            )
            or len(set(self.expected_advisory_ids)) != len(self.expected_advisory_ids)
        ):
            raise ValueError("invalid_snapshot_check")
        return self


class SnapshotAuditSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: VDBPolicy
    expected_sha256: dict[str, str]
    checks: tuple[SnapshotCheck, ...] = Field(min_length=2, max_length=20)
    max_snapshot_bytes: int = Field(default=2 * 1024**3, ge=1, le=64 * 1024**3, strict=True)
    max_audit_seconds: int = Field(default=300, ge=1, le=3600, strict=True)
    max_lookup_seconds: float = Field(default=5.0, gt=0, le=15, allow_inf_nan=False)
    allow_synthetic: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def explicit_manifest_and_controls(self) -> SnapshotAuditSpec:
        if set(self.expected_sha256) != set(SNAPSHOT_FILES) or any(
            not re.fullmatch(r"[a-f0-9]{64}", value) for value in self.expected_sha256.values()
        ):
            raise ValueError("three_expected_snapshot_digests_required")
        if self.policy.synthetic and not self.allow_synthetic:
            raise ValueError("synthetic_requires_explicit_test_opt_in")
        if not any(c.expected_advisory_ids for c in self.checks) or not any(
            not c.expected_advisory_ids for c in self.checks
        ):
            raise ValueError("positive_and_negative_controls_required")
        return self


def _read_regular(path: Path, limit: int, deadline: float) -> tuple[str, int]:
    """Bounded streaming digest, rejecting links/special files and observed drift."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("snapshot_file_or_size_rejected")
        digest, count = hashlib.sha256(), 0
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("snapshot_audit_timeout")
            chunk = stream.read(min(1024**2, limit - count + 1))
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise ValueError("snapshot_size_limit_exceeded")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        current = path.lstat()

        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("snapshot_changed_during_read")
        return digest.hexdigest(), count


def _snapshot(spec: SnapshotAuditSpec, deadline: float) -> tuple[dict[str, str], int]:
    root = Path(spec.policy.root)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError("snapshot_root_rejected")
    for name in SNAPSHOT_FILES[:2]:
        for suffix in ("-wal", "-shm", "-journal"):
            # lexists detects dangling sidecar links too; never treat these as clean.
            if os.path.lexists(root / (name + suffix)):
                raise ValueError("snapshot_sqlite_sidecar_present")
    hashes, total = {}, 0
    for name in SNAPSHOT_FILES:
        file_limit = (
            min(spec.max_snapshot_bytes - total, 65536)
            if name == "vdb.meta"
            else (spec.max_snapshot_bytes - total)
        )
        hashes[name], size = _read_regular(root / name, file_limit, deadline)
        total += size
    if hashes != spec.expected_sha256:
        raise ValueError("snapshot_expected_digest_mismatch")
    return hashes, total


def audit_snapshot(spec: SnapshotAuditSpec) -> dict:
    """No raw query/body/path/error echo, downloads, DB updates or activation."""
    started = time.monotonic()
    deadline = started + spec.max_audit_seconds
    before, total = _snapshot(spec, deadline)
    reader = VDBReader(spec.policy)
    status = reader.status(deadline=deadline)
    # Never turn an observed synthetic corpus into an operating candidate merely
    # because its operator policy was accidentally marked synthetic=false.
    synthetic = spec.policy.synthetic or "synthetic" in status.sources
    ready = (
        status.state == "ready"
        and status.freshness == "fresh"
        and status.completeness == "full"
        and status.metadata_coverage == "full"
        and (not synthetic or spec.allow_synthetic)
    )
    checks = []
    if ready:
        for index, check in enumerate(spec.checks):
            start = time.monotonic()
            passed = False
            try:
                result = reader.lookup(
                    check.kind, check.query, spec.policy.max_results, deadline=deadline
                )
                actual = {item.advisory_id for item in result.matches}
                passed = not result.truncated and actual == set(check.expected_advisory_ids)
            except Exception:
                # Backend messages may contain filesystem paths or input; do not echo them.
                pass
            elapsed = time.monotonic() - start
            checks.append(
                {
                    "index": index,
                    "kind": check.kind,
                    "matched_expected": passed,
                    "within_latency_limit": elapsed <= spec.max_lookup_seconds,
                    "elapsed_seconds": round(elapsed, 6),
                }
            )
    after, _ = _snapshot(spec, deadline)
    passed = (
        ready
        and len(checks) == len(spec.checks)
        and all(c["matched_expected"] and c["within_latency_limit"] for c in checks)
    )
    return {
        "local_validation_passed": passed,
        "synthetic": synthetic,
        "operating_snapshot_candidate": passed and not synthetic,
        "expected_manifest_digests_verified": True,
        "snapshot_unchanged": before == after,
        "snapshot_bytes": total,
        "database": status.model_dump(mode="json"),
        "checks": checks,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "upstream_source_revision_verified": False,
        "live_llm_tested": False,
        "target_vulnerability_confirmed": False,
        "activated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    try:
        # Trusted operator input only; cap bytes and refuse symlinks/FIFO.
        expected, _ = _read_regular(args.spec, 65536, time.monotonic() + 5)
        descriptor = os.open(args.spec, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("audit_spec_not_regular")
            raw = stream.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("audit_spec_too_large")
        report = audit_snapshot(SnapshotAuditSpec.model_validate_json(raw))
    except Exception:
        print(
            json.dumps(
                {
                    "local_validation_passed": False,
                    "error": "vdb_snapshot_audit_failed",
                    "activated": False,
                }
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["local_validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
