"""Local, bounded knowledge lookup using AppThreat VDB's pinned search library.

This is our read-only bridge, NOT the upstream MCP server entry point. No
refresh, shard fetch, SQL, filesystem path, SBOM path or exploit execution tool.
Operator-owned immutable DB snapshots must be separate from writable workspaces.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator

UPSTREAM_COMMIT = "2dc040a44986da0c4eac1366ba77633f09e593b6"
BACKEND_VERSION = "7.0.1"
CVE_PATTERN = r"^CVE-[0-9]{4}-[0-9]{4,10}$"
_LOCK = threading.Lock()  # upstream connection globals belong to one process


class VDBError(ValueError):
    """Fixed codes only; never return host paths or raw backend exception text."""


class VDBPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_ref: str
    engagement_id: str
    run_id: str
    root: str
    synthetic: bool = False
    max_age_days: int = Field(default=7, ge=1, le=365)
    max_results: int = Field(default=20, ge=1, le=50)
    query_timeout_seconds: int = Field(default=5, ge=1, le=15)

    @field_validator("asset_ref", "engagement_id", "run_id")
    @classmethod
    def identity(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("vdb identities must be bounded opaque identifiers")
        return value

    @classmethod
    def load(cls, path: str) -> VDBPolicy:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


DataState = Literal[
    "ready",
    "missing",
    "empty",
    "invalid",
    "coverage_gap",
    "unknown_metadata",
    "backend_unavailable",
    "backend_version_mismatch",
]


class VDBStatus(BaseModel):
    asset_ref: str
    state: DataState
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    completeness: Literal["full", "partial", "unknown"] = "unknown"
    created_utc: str | None = None
    sources: list[str] = Field(default_factory=list)
    data_rows: int | None = None
    index_rows: int | None = None
    metadata_rows: int | None = None
    synthetic: bool
    required_backend_version: str = BACKEND_VERSION
    required_upstream_commit: str = UPSTREAM_COMMIT
    observed_backend_version: str | None = None
    source_revision_verified: Literal[False] = False
    # Pin is a deployment requirement, not a cryptographic provenance claim.
    assessment: Literal["knowledge_only"] = "knowledge_only"


class VDBMatch(BaseModel):
    advisory_id: str
    purl_prefix: str | None = None
    affected_versions: str | None = None
    source: str | None = None
    severity: str | None = None
    score: float | None = None
    published: str | None = None
    updated: str | None = None
    aliases: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class VDBLookup(BaseModel):
    database: VDBStatus
    query: str
    matches: list[VDBMatch]
    truncated: bool
    interpretation: Literal["knowledge_matches", "no_known_match_in_loaded_snapshot"]
    target_vulnerability_confirmed: Literal[False] = False


class VDBReader:
    def __init__(self, policy: VDBPolicy):
        self.policy = policy
        self.root = Path(policy.root)
        # No unresolved, relative or symlinked operator DB root.
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise VDBError("vdb_invalid_root")

    def bind(self, asset_ref: str, engagement_id: str, run_id: str) -> None:
        if (asset_ref, engagement_id, run_id) != (
            self.policy.asset_ref,
            self.policy.engagement_id,
            self.policy.run_id,
        ):
            raise VDBError("vdb_context_mismatch")

    def _regular(self, name: str) -> Path:
        path = self.root / name
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise VDBError("vdb_invalid_snapshot")
        return path

    def _status(self, data, index) -> VDBStatus:
        result = VDBStatus(
            asset_ref=self.policy.asset_ref, state="ready", synthetic=self.policy.synthetic
        )
        result.observed_backend_version = version("appthreat-vulnerability-db")
        result.data_rows = data.execute("SELECT count(*) FROM cve_data").fetchone()[0]
        result.index_rows = index.execute("SELECT count(*) FROM cve_index").fetchone()[0]
        result.metadata_rows = index.execute("SELECT count(*) FROM cve_metadata").fetchone()[0]
        # Check schema needed by original lookup without mutating/migrating it.
        index.execute("SELECT aliases, reference_text FROM cve_metadata_text LIMIT 0")
        if not result.data_rows or not result.index_rows:
            result.state = "empty"
            return result
        try:
            meta_path = self._regular("vdb.meta")
            descriptor = os.open(meta_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise VDBError("vdb_invalid_snapshot")
                raw = stream.read(65_537)
            if len(raw) > 65_536:
                raise VDBError("vdb_invalid_snapshot")
            metadata = json.loads(raw)
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
        except FileNotFoundError:
            result.state = "unknown_metadata"
            return result
        for name, observed in (
            ("cve_data_count", result.data_rows),
            ("cve_index_count", result.index_rows),
        ):
            if name in metadata and (type(metadata[name]) is not int or metadata[name] != observed):
                raise VDBError("vdb_invalid_snapshot")
        completeness = metadata.get("completeness")
        if completeness not in {"full", "partial"}:
            result.state = "unknown_metadata"
            return result
        result.completeness = completeness
        if completeness == "partial":
            result.state = "coverage_gap"  # no shard fan-out in this first bridge
        created = metadata.get("created_utc")
        if isinstance(created, str) and len(created) <= 64:
            try:
                timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if timestamp.tzinfo is not None:
                    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
                    if age >= -300:
                        result.created_utc = created
                        result.freshness = (
                            "stale" if age > self.policy.max_age_days * 86400 else "fresh"
                        )
            except ValueError:
                pass
        sources = metadata.get("sources", [])
        if isinstance(sources, list):
            result.sources = [s for s in sources[:50] if isinstance(s, str) and len(s) <= 128]
        return result

    @contextmanager
    def _connections(self):
        # Importing upstream config can create directories. Only import search
        # after preflight, using an existing root and private temporary cache.
        if self.root.resolve() != self.root:
            raise VDBError("vdb_invalid_root")
        paths = [self._regular(name) for name in ("data.vdb7", "data.index.vdb7")]
        # An immutable snapshot must not be an unfinished WAL/journal build.
        # Immutable SQLite avoids even creation of WAL shared-memory sidecars.
        for path in paths:
            for suffix in ("-wal", "-shm", "-journal"):
                if (self.root / (path.name + suffix)).exists():
                    raise VDBError("vdb_invalid_snapshot")
        if version("appthreat-vulnerability-db") != BACKEND_VERSION:
            raise VDBError("vdb_backend_version_mismatch")
        apsw = importlib.import_module("apsw")
        connections = []
        try:
            deadline = time.monotonic() + self.policy.query_timeout_seconds
            for path in paths:
                conn = apsw.Connection(
                    path.as_uri() + "?mode=ro&immutable=1",
                    flags=(
                        apsw.SQLITE_OPEN_URI | apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_NOFOLLOW
                    ),
                )  # Never CREATE; upstream db.get(read_only=True) has a create fallback.
                connections.append(conn)
                conn.pragma("query_only", True)
                conn.pragma("temp_store", "MEMORY")
                conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            yield connections
        finally:
            for conn in connections:
                conn.close()

    def status(self) -> VDBStatus:
        with _LOCK:
            try:
                with self._connections() as (data, index):
                    return self._status(data, index)
            except FileNotFoundError:
                state = "missing"
            except PackageNotFoundError:
                state = "backend_unavailable"
            except ImportError:
                state = "backend_unavailable"
            except VDBError as exc:
                state = (
                    "backend_version_mismatch"
                    if str(exc) == "vdb_backend_version_mismatch"
                    else "invalid"
                )
            except Exception:
                state = "invalid"
            return VDBStatus(
                asset_ref=self.policy.asset_ref, state=state, synthetic=self.policy.synthetic
            )

    def lookup(self, kind: Literal["cve", "purl"], query: str, limit: int) -> VDBLookup:
        if not 1 <= limit <= self.policy.max_results or isinstance(limit, bool):
            raise VDBError("vdb_invalid_limit")
        if not isinstance(query, str) or len(query) > 512 or any(ord(c) < 32 for c in query):
            raise VDBError("vdb_invalid_query")
        if kind == "cve" and not re.fullmatch(CVE_PATTERN, query):
            raise VDBError("vdb_invalid_cve")
        if kind not in {"cve", "purl"}:
            raise VDBError("vdb_invalid_query")
        with _LOCK:
            try:
                with self._connections() as (data, index):
                    status = self._status(data, index)
                    if status.state != "ready":
                        raise VDBError(f"vdb_{status.state}")
                    with tempfile.TemporaryDirectory(prefix="redteam-vdb-cache-") as cache:
                        os.environ.update(
                            VDB_HOME=str(self.root),
                            VDB_CACHE=cache,
                            VDB_AUTO_FETCH="false",
                            VDB_INCLUDE_METADATA="false",
                        )
                        config = importlib.import_module("vdb.lib.config")
                        if Path(config.DATA_DIR) != self.root:
                            raise VDBError("vdb_backend_binding_mismatch")
                        config.VDB_AUTO_FETCH = False
                        config.VDB_INCLUDE_METADATA = False
                        db = importlib.import_module("vdb.lib.db")
                        search = importlib.import_module("vdb.lib.search")
                        if db.db_conn is not None or db.index_conn is not None:
                            raise VDBError("vdb_backend_binding_mismatch")
                        # Bind original search to OUR strictly read-only handles.
                        db.db_conn, db.index_conn = data, index
                        db.bound_paths = (
                            str(self.root / "data.vdb7"),
                            str(self.root / "data.index.vdb7"),
                        )
                        db.tables_created = True
                        try:
                            index.createscalarfunction(
                                "vers_compare", db.vers_compare, deterministic=True
                            )
                            if kind == "purl":
                                from packageurl import PackageURL

                                purl = PackageURL.from_string(query)
                                if (
                                    not purl.version
                                    or any(c in purl.version for c in "*<>=|")
                                    or purl.subpath
                                ):
                                    raise VDBError("vdb_versioned_purl_required")
                                raw = search.search_by_purl_like(
                                    query,
                                    with_data=False,
                                    max_results=limit + 1,
                                )
                            else:
                                raw = search.search_by_cve(
                                    query,
                                    with_data=False,
                                    max_results=limit + 1,
                                )
                        finally:
                            db.db_conn = db.index_conn = db.bound_paths = None
                            db.tables_created = False
                    matches = [self._match(hit) for hit in raw[:limit]]
                    output = VDBLookup(
                        database=status,
                        query=query,
                        matches=matches,
                        truncated=len(raw) > limit,
                        interpretation=(
                            "knowledge_matches" if matches else "no_known_match_in_loaded_snapshot"
                        ),
                    )
                    # Bound encoded UTF-8 too, not only character counts/row count.
                    # Never return truncated JSON or silently discard a row.
                    while len(output.model_dump_json().encode("utf-8")) > 200_000:
                        output.matches.pop()
                        output.truncated = True
                    return output
            except VDBError:
                raise
            except Exception:
                raise VDBError("vdb_lookup_unavailable") from None

    @staticmethod
    def _match(hit: dict) -> VDBMatch:
        def text(name):
            value = hit.get(name)
            return value[:1024] if isinstance(value, str) else None

        def texts(name):
            value = hit.get(name, [])
            return (
                [v[:1024] for v in value[:20] if isinstance(v, str)]
                if isinstance(value, list)
                else []
            )

        return VDBMatch(
            advisory_id=text("cve_id") or "",
            purl_prefix=text("purl_prefix"),
            affected_versions=text("vers"),
            source=text("source"),
            severity=text("severity"),
            score=hit.get("score"),
            published=text("published"),
            updated=text("updated"),
            aliases=texts("aliases"),
            references=texts("references"),
        )


def create_server(policy: VDBPolicy) -> MCPServer:
    reader = VDBReader(policy)
    server = MCPServer("vdb-readonly", log_level="WARNING")

    def vdb_status(asset_ref: str, engagement_id: str = "", run_id: str = "") -> VDBStatus:
        """Read local DB readiness/age. Missing or empty DB is NOT a clean assessment."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.status()

    def search_by_cve(
        asset_ref: str, cve_id: str, limit: int = 10, engagement_id: str = "", run_id: str = ""
    ) -> VDBLookup:
        """Look up one exact CVE in the local snapshot; never confirms a target finding."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.lookup("cve", cve_id, limit)

    def search_by_purl_like(
        asset_ref: str, purl: str, limit: int = 10, engagement_id: str = "", run_id: str = ""
    ) -> VDBLookup:
        """Look up one observed versioned PURL; no inferred version, fetching or SBOM paths."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.lookup("purl", purl, limit)

    for function in (vdb_status, search_by_cve, search_by_purl_like):
        function.__annotations__["asset_ref"] = Literal[policy.asset_ref]
        if "limit" in function.__annotations__:
            function.__defaults__ = (min(10, policy.max_results), "", "")
            function.__annotations__["limit"] = Annotated[int, Field(ge=1, le=policy.max_results)]
        if "cve_id" in function.__annotations__:
            function.__annotations__["cve_id"] = Annotated[str, Field(pattern=CVE_PATTERN)]
        if "purl" in function.__annotations__:
            function.__annotations__["purl"] = Annotated[str, Field(min_length=1, max_length=512)]
        server.tool()(function)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    args = parser.parse_args()
    create_server(VDBPolicy.load(args.policy)).run(transport="stdio")


if __name__ == "__main__":
    main()
