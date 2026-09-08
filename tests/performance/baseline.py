"""Read-only historical/release source verification, independent of runtime code."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "baselines" / "generic_core_release.json"


def release_baseline() -> dict[str, object]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    if data["schema_version"] != 1:
        raise ValueError("unsupported release baseline schema")
    return data


def tree_hash(rows: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(rows):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def current_core_hash() -> str:
    core = ROOT / "src" / "redteam_core"
    return tree_hash([(p.relative_to(core).as_posix(), p.read_bytes()) for p in core.rglob("*.py")])


def historical_core_hash() -> str:
    """Git object reads only. Never checkout, fetch, mutate, or normalize bytes."""
    commit = str(release_baseline()["historical_commit"])
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("historical commit must be an exact hash")
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "-z", commit, "src/redteam_core"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    prefix = "src/redteam_core/"
    entries = []
    for row in listing.split(b"\0"):
        if not row:
            continue
        header, path_bytes = row.split(b"\t", 1)
        _mode, kind, object_id = header.split()
        path = path_bytes.decode("utf-8")
        if kind == b"blob" and path.startswith(prefix) and path.endswith(".py"):
            entries.append((path.removeprefix(prefix), object_id))
    if not entries:
        raise ValueError("historical tree has no core source")
    # Unlike archive/checkout, raw blob reads cannot apply core.autocrlf filters.
    blobs = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=ROOT,
        input=b"".join(oid + b"\n" for _, oid in entries),
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    stream = io.BytesIO(blobs)
    rows = []
    for name, expected_oid in entries:
        oid, kind, size = stream.readline().split()
        if oid != expected_oid or kind != b"blob":
            raise ValueError("unexpected historical object")
        content = stream.read(int(size))
        if len(content) != int(size) or stream.read(1) != b"\n":
            raise ValueError("truncated historical object")
        rows.append((name, content))
    return tree_hash(rows)
