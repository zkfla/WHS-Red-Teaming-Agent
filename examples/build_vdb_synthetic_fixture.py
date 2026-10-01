"""Operator-only SMALL synthetic VDB7 fixture, not a vulnerability feed/build tool.

Run in the isolated VDB environment. Refuses existing output roots; writes
three fake advisory records for wire/contract tests. No network access.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def build(root: Path) -> None:
    import apsw

    root.mkdir(mode=0o700)  # exclusive; never modify an operator's real snapshot
    data = apsw.Connection(str(root / "data.vdb7"))
    index = apsw.Connection(str(root / "data.index.vdb7"))
    try:
        data.execute(
            "CREATE TABLE cve_data(cve_id TEXT, type TEXT, namespace TEXT, name TEXT, "
            "override_data BLOB, source_data_hash TEXT, vers TEXT, purl_prefix TEXT)"
        )
        data.execute("CREATE TABLE cve_source_data(source_data_hash TEXT, source_data BLOB)")
        index.execute(
            "CREATE TABLE cve_index(cve_id TEXT, type TEXT, namespace TEXT, name TEXT, "
            "vers TEXT, purl_prefix TEXT, PRIMARY KEY(cve_id, vers, purl_prefix)) WITHOUT ROWID"
        )
        index.execute(
            "CREATE TABLE cve_metadata(cve_id TEXT, vers TEXT, purl_prefix TEXT, source TEXT, "
            "severity TEXT, score REAL, published TEXT, updated TEXT, is_malware INTEGER, "
            "PRIMARY KEY(cve_id, vers, purl_prefix)) WITHOUT ROWID"
        )
        index.execute(
            "CREATE TABLE cve_metadata_text(cve_id TEXT PRIMARY KEY, aliases TEXT, "
            "reference_text TEXT, description_text TEXT, affected_symbols TEXT) WITHOUT ROWID"
        )
        for number in range(3):
            cve = f"CVE-2026-{900001 + number}"
            vers = "vers:npm/>=1.0.0|<2.0.0"
            prefix = "pkg:npm/redteam-synthetic-demo"
            row = (cve, "npm", None, "redteam-synthetic-demo", vers, prefix)
            index.execute("INSERT INTO cve_index VALUES(?,?,?,?,?,?)", row)
            data.execute(
                "INSERT INTO cve_data VALUES(?,?,?,?,?,?,?,?)", (*row[:4], None, cve, vers, prefix)
            )
            data.execute(
                "INSERT INTO cve_source_data VALUES(?,?)",
                (cve, apsw.jsonb_encode({"synthetic": True})),
            )
            index.execute(
                "INSERT INTO cve_metadata VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    cve,
                    vers,
                    prefix,
                    "synthetic",
                    "HIGH",
                    7.5,
                    "2026-01-01T00:00:00Z",
                    "2026-01-02T00:00:00Z",
                    0,
                ),
            )
            index.execute(
                "INSERT INTO cve_metadata_text VALUES(?,?,?,?,?)",
                (
                    cve, "GHSA-2345-6789-cfgh" if number == 0 else "",
                    "https://example.invalid/synthetic-advisory", "synthetic", "",
                ),
            )
    finally:
        data.close()
        index.close()
    (root / "vdb.meta").write_text(
        json.dumps(
            {
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "completeness": "full",
                "sources": ["synthetic"],
                "cve_data_count": 3,
                "cve_index_count": 3,
                "synthetic": True,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    build(parser.parse_args().root)
