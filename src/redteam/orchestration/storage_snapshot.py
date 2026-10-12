"""Offline SQLite inspection: copy WAL too, never open or set up an original.

The offline assertion is an operational precondition, not inferred from stable
hashes. Hashes detect concurrent changes during capture; original Run ownership
is checked separately by the migration entrypoint.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from redteam.orchestration.coordination import StateConflict


def digest(value: Any) -> str:
    def binary(item):
        if isinstance(item, bytes):
            return {"bytes_sha256": hashlib.sha256(item).hexdigest()}
        raise TypeError("unsupported source value")

    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=binary,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def source_manifest(databases: dict[str, Path], references: dict[str, Path]) -> dict:
    """Only paths and hashes, including the absence of a SQLite sidecar."""
    entries: dict[str, str | None] = {}
    for path in databases.values():
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            entries[str(candidate)] = (
                hashlib.sha256(candidate.read_bytes()).hexdigest() if candidate.exists() else None
            )
    for path in references.values():
        if not path.exists():
            raise StateConflict("migration_original_reference_missing")
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        entries[str(path)] = digest(
            [
                (
                    str(p.relative_to(path)) if path.is_dir() else "",
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                )
                for p in files
            ]
        )
    return {
        "databases": {name: str(path) for name, path in databases.items()},
        "references": {name: str(path) for name, path in references.items()},
        "files": entries,
    }


class OfflineSnapshot:
    """A disposable, query-only copy of every explicitly selected source."""

    def __init__(self, databases: dict[str, str | Path], references: dict[str, str | Path]):
        self.paths = {name: Path(path).resolve(strict=True) for name, path in databases.items()}
        self.references = {
            name: Path(path).resolve(strict=True) for name, path in references.items()
        }
        self.stack = ExitStack()
        self.connections: dict[str, sqlite3.Connection] = {}
        self.copies: dict[str, Path] = {}
        self.manifest: dict = {}

    def __enter__(self):
        try:
            self.manifest = source_manifest(self.paths, self.references)
            folder = Path(self.stack.enter_context(TemporaryDirectory(prefix="redteam-migrate-")))
            for index, (name, source) in enumerate(sorted(self.paths.items())):
                destination = folder / str(index) / source.name
                destination.parent.mkdir()
                for suffix in ("", "-wal", "-shm"):
                    original = Path(str(source) + suffix)
                    if original.exists():
                        shutil.copy2(original, Path(str(destination) + suffix))
                self.copies[name] = destination
            self.verify_originals()
            for name, path in self.copies.items():
                db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                self.stack.callback(db.close)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise StateConflict("migration_source_integrity_failed")
                self.connections[name] = db
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def verify_originals(self) -> None:
        if source_manifest(self.paths, self.references) != self.manifest:
            raise StateConflict("migration_source_changed")

    def tables(self, source: str) -> list[str]:
        db = self.connections.get(source)
        return (
            []
            if db is None
            else sorted(
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'",
                )
            )
        )

    def rows(self, source: str, table: str) -> list[dict]:
        if table not in self.tables(source):
            return []
        quoted = '"' + table.replace('"', '""') + '"'
        return [dict(row) for row in self.connections[source].execute("SELECT * FROM " + quoted)]

    def row_key(self, source: str, table: str, row: dict) -> str:
        quoted = '"' + table.replace('"', '""') + '"'
        columns = sorted(
            (
                item
                for item in self.connections[source].execute(
                    "PRAGMA table_info(" + quoted + ")",
                )
                if item[5]
            ),
            key=lambda item: item[5],
        )
        return json.dumps([row[item[1]] for item in columns]) if columns else digest(row)
