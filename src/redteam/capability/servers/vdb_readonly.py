"""Local, bounded knowledge lookup using AppThreat VDB's pinned search library.

This is our read-only bridge, NOT the upstream MCP server entry point. No
refresh, shard fetch, SQL, filesystem path, SBOM path or exploit execution tool.
Operator-owned immutable DB snapshots must be separate from writable workspaces.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Thread
from typing import IO, Annotated, Literal, Protocol, cast

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

from redteam.core.contracts.tool import PROVIDER_METADATA_RESOURCE_EFFECTS

UPSTREAM_COMMIT = "2dc040a44986da0c4eac1366ba77633f09e593b6"
BACKEND_VERSION = "7.0.1"
CVE_PATTERN = r"^CVE-[0-9]{4}-[0-9]{4,10}$"
GHSA_PATTERN = r"^GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}$"
_UPSTREAM_LOCK = threading.Lock()  # child-local guard around AppThreat's module globals
_MAX_CONCURRENT_LOOKUP_WORKERS = 8
_LOOKUP_WORKER_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_LOOKUP_WORKERS)
_LOOKUP_WORKER_REQUEST_BYTES = 65_536
_LOOKUP_WORKER_OUTPUT_BYTES = 200_000
_LOOKUP_WORKER_CLEANUP_SECONDS = 0.5


class VDBError(ValueError):
    """Fixed codes only; never return host paths or raw backend exception text."""


class _LookupWorkerCleanupUncertain(VDBError):
    """The child or one of its pipe threads outlived bounded cleanup."""

    def __init__(self, stopped: Callable[[], bool]) -> None:
        super().__init__("vdb_lookup_cleanup_uncertain")
        self.stopped = stopped


class _RetainedLookupSlots:
    """Own the exact acquired slots until the original worker has stopped."""

    def __init__(
        self, stopped: Callable[[], bool], policy_slots: threading.BoundedSemaphore,
        worker_slots: threading.BoundedSemaphore,
    ) -> None:
        self.stopped = stopped
        self.policy_slots = policy_slots
        self.worker_slots = worker_slots
        self.released = False
        self.lock = threading.Lock()

    def reap(self) -> bool:
        with self.lock:
            if self.released:
                return True
            try:
                if not self.stopped():
                    return False
            except (OSError, RuntimeError, ValueError):
                return False
            self.released = True
            self.policy_slots.release()
            self.worker_slots.release()
            return True


_REAPER_CONDITION = threading.Condition()
_RETAINED_LOOKUPS: list[_RetainedLookupSlots] = []
_REAPER_THREAD: threading.Thread | None = None


def _reap_lookup_slots() -> None:
    while True:
        with _REAPER_CONDITION:
            while not _RETAINED_LOOKUPS:
                _REAPER_CONDITION.wait()
            pending = tuple(_RETAINED_LOOKUPS)
        for record in pending:
            if record.reap():
                with _REAPER_CONDITION:
                    if record in _RETAINED_LOOKUPS:
                        _RETAINED_LOOKUPS.remove(record)
        with _REAPER_CONDITION:
            if _RETAINED_LOOKUPS:
                _REAPER_CONDITION.wait(timeout=0.1)


def _ensure_lookup_reaper() -> None:
    global _REAPER_THREAD
    with _REAPER_CONDITION:
        if _RETAINED_LOOKUPS and (_REAPER_THREAD is None or not _REAPER_THREAD.is_alive()):
            thread = threading.Thread(target=_reap_lookup_slots, daemon=True)
            try:
                thread.start()
            except RuntimeError:
                # Keep ownership in the registry; a later call retries startup.
                return
            _REAPER_THREAD = thread


def _retain_lookup_slots(
    stopped: Callable[[], bool], policy_slots: threading.BoundedSemaphore,
    worker_slots: threading.BoundedSemaphore,
) -> None:
    # Each record holds a global slot, so this registry is bounded by that cap.
    with _REAPER_CONDITION:
        _RETAINED_LOOKUPS.append(_RetainedLookupSlots(stopped, policy_slots, worker_slots))
        _REAPER_CONDITION.notify_all()
    _ensure_lookup_reaper()


class _BackendConfig(Protocol):
    """Global configuration exposed by the pinned upstream version."""

    DATA_DIR: str
    VDB_AUTO_FETCH: bool
    VDB_INCLUDE_METADATA: bool


class _BackendDatabase(Protocol):
    """Upstream connection globals bound only inside one lookup worker."""

    db_conn: object | None
    index_conn: object | None
    bound_paths: tuple[str, str] | None
    tables_created: bool
    vers_compare: Callable[..., int]


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
    max_concurrent_lookups: int = Field(default=1, ge=1, le=_MAX_CONCURRENT_LOOKUP_WORKERS)

    @model_serializer(mode="wrap")
    def compatible_payload(self, handler):
        payload = handler(self)
        if self.max_concurrent_lookups == 1:
            payload.pop("max_concurrent_lookups", None)
        return payload

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
    metadata_coverage: Literal["full", "partial", "missing", "unknown"] = "unknown"
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
    description: str | None = None
    text_truncated: bool = False
    match_basis: (
        Literal["advisory_id", "advisory_alias", "versioned_purl", "concrete_cpe"] | None
    ) = None


class VDBLookup(BaseModel):
    database: VDBStatus
    query: str
    query_kind: Literal["cve", "purl", "ghsa", "cpe"] | None = None
    requested_sources: list[str] = Field(default_factory=list)
    matches: list[VDBMatch]
    truncated: bool
    interpretation: Literal["knowledge_matches", "no_known_match_in_loaded_snapshot"]
    target_vulnerability_confirmed: Literal[False] = False


class _LookupWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: VDBPolicy
    kind: Literal["cve", "purl", "ghsa", "cpe"]
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(strict=True, ge=1, le=50)
    sources: list[str] | None = None
    timeout_seconds: float = Field(gt=0, le=15)
    timeout_error_code: Literal["vdb_lookup_unavailable", "vdb_batch_timeout"]


class VDBReader:
    def __init__(self, policy: VDBPolicy):
        self.policy = policy
        self.root = Path(policy.root)
        self._lookup_slots = threading.BoundedSemaphore(policy.max_concurrent_lookups)
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
        missing = index.execute(
            "SELECT EXISTS(SELECT 1 FROM cve_index i "
            "LEFT JOIN cve_metadata m ON m.cve_id=i.cve_id "
            "AND m.vers=i.vers AND m.purl_prefix=i.purl_prefix "
            "LEFT JOIN cve_metadata_text t ON t.cve_id=i.cve_id "
            "WHERE m.cve_id IS NULL OR t.cve_id IS NULL "
            "OR m.source IS NULL OR m.source='' LIMIT 1)"
        ).fetchone()[0]
        result.metadata_coverage = (
            "missing" if not result.metadata_rows else "partial" if missing else "full"
        )
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
    def _connections(self, deadline: float | None = None):
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
            query_deadline = time.monotonic() + self.policy.query_timeout_seconds
            deadline = min(deadline, query_deadline) if deadline is not None else query_deadline
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

    def status(self, *, deadline: float | None = None) -> VDBStatus:
        state: DataState
        try:
            # Status owns fresh immutable APSW handles and does not touch the
            # AppThreat module globals used only by search lookup.
            with self._connections(deadline) as (data, index):
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

    def lookup(
        self,
        kind: Literal["cve", "purl", "ghsa", "cpe"],
        query: str,
        limit: int,
        *,
        sources: list[str] | None = None,
        deadline: float | None = None,
    ) -> VDBLookup:
        _ensure_lookup_reaper()
        self._validate_lookup(kind, query, limit, sources, deadline)
        policy_deadline = time.monotonic() + self.policy.query_timeout_seconds
        batch_deadline = deadline
        lookup_deadline = (
            min(policy_deadline, batch_deadline)
            if batch_deadline is not None
            else policy_deadline
        )
        timeout_error_code: Literal["vdb_lookup_unavailable", "vdb_batch_timeout"] = (
            "vdb_batch_timeout"
            if batch_deadline is not None and batch_deadline <= policy_deadline
            else "vdb_lookup_unavailable"
        )

        remaining = lookup_deadline - time.monotonic()
        if remaining <= 0:
            raise VDBError(timeout_error_code)
        if not self._lookup_slots.acquire(timeout=remaining):
            raise VDBError(timeout_error_code)
        retain_policy_slot = False
        worker_slots = _LOOKUP_WORKER_SLOTS
        try:
            remaining = lookup_deadline - time.monotonic()
            if remaining <= 0 or not worker_slots.acquire(timeout=max(0.0, remaining)):
                raise VDBError(timeout_error_code)
            retain_worker_slot = False
            try:
                request = _LookupWorkerRequest(
                    policy=VDBPolicy.model_validate(
                        self.policy.model_dump(include=set(VDBPolicy.model_fields.keys()))
                    ),
                    kind=kind,
                    query=query,
                    limit=limit,
                    sources=sources,
                    timeout_seconds=max(0.001, lookup_deadline - time.monotonic()),
                    timeout_error_code=timeout_error_code,
                )
                try:
                    return _invoke_lookup_worker(request, lookup_deadline, timeout_error_code)
                except _LookupWorkerCleanupUncertain as exc:
                    retain_worker_slot = True
                    retain_policy_slot = True
                    _retain_lookup_slots(exc.stopped, self._lookup_slots, worker_slots)
                    raise
            finally:
                if not retain_worker_slot:
                    worker_slots.release()
        finally:
            if not retain_policy_slot:
                self._lookup_slots.release()

    def _validate_lookup(
        self,
        kind: str,
        query: str,
        limit: int,
        sources: list[str] | None,
        deadline: float | None,
    ) -> None:
        if not 1 <= limit <= self.policy.max_results or isinstance(limit, bool):
            raise VDBError("vdb_invalid_limit")
        if not isinstance(query, str) or len(query) > 512 or any(ord(c) < 32 for c in query):
            raise VDBError("vdb_invalid_query")
        if kind == "cve" and not re.fullmatch(CVE_PATTERN, query):
            raise VDBError("vdb_invalid_cve")
        if kind == "ghsa" and not re.fullmatch(GHSA_PATTERN, query):
            raise VDBError("vdb_invalid_ghsa")
        if kind not in {"cve", "purl", "ghsa", "cpe"}:
            raise VDBError("vdb_invalid_query")
        if sources is not None and (
            len(sources) > 5
            or len(set(sources)) != len(sources)
            or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", source) for source in sources)
        ):
            raise VDBError("vdb_invalid_sources")
        if deadline is not None and time.monotonic() >= deadline:
            raise VDBError("vdb_batch_timeout")

    def _lookup_in_worker(
        self,
        kind: Literal["cve", "purl", "ghsa", "cpe"],
        query: str,
        limit: int,
        sources: list[str] | None,
        timeout_seconds: float,
        timeout_error_code: Literal["vdb_lookup_unavailable", "vdb_batch_timeout"],
    ) -> VDBLookup:
        deadline = time.monotonic() + timeout_seconds
        with _UPSTREAM_LOCK:
            try:
                with self._connections(deadline) as (data, index):
                    status = self._status(data, index)
                    if status.state != "ready":
                        raise VDBError(f"vdb_{status.state}")
                    if kind == "ghsa" or sources:
                        # Original alias/filter APIs can silently answer [] on
                        # non-extended artifacts. Require complete query metadata.
                        if status.metadata_coverage != "full":
                            raise VDBError("vdb_metadata_coverage_gap")
                    with tempfile.TemporaryDirectory(prefix="redteam-vdb-cache-") as cache:
                        os.environ.update(
                            VDB_HOME=str(self.root),
                            VDB_CACHE=cache,
                            VDB_AUTO_FETCH="false",
                            VDB_INCLUDE_METADATA="false",
                        )
                        config = cast(_BackendConfig, importlib.import_module("vdb.lib.config"))
                        if Path(config.DATA_DIR) != self.root:
                            raise VDBError("vdb_backend_binding_mismatch")
                        config.VDB_AUTO_FETCH = False
                        config.VDB_INCLUDE_METADATA = False
                        db = cast(_BackendDatabase, importlib.import_module("vdb.lib.db"))
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
                            filters = {"sources": sources} if sources else None
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
                                    filters=filters,
                                    max_results=limit + 1,
                                )
                            elif kind == "cpe":
                                # Do not broaden an unknown/wildcard version into
                                # all versions. Strict concrete CPE 2.3 subset.
                                parts = query.split(":")
                                if (
                                    len(parts) != 13
                                    or parts[:2] != ["cpe", "2.3"]
                                    or parts[2] not in {"a", "o", "h"}
                                    or any(
                                        not re.fullmatch(r"[A-Za-z0-9_.+-]+", p) for p in parts[3:6]
                                    )
                                    or parts[5] == "-"
                                    or any(p != "*" for p in parts[6:])
                                ):
                                    raise VDBError("vdb_concrete_cpe_required")
                                raw = search.search_by_cpe_like(
                                    query, with_data=False, filters=filters, max_results=limit + 1
                                )
                            else:
                                raw = search.search_by_cve(
                                    query,
                                    with_data=False,
                                    filters=filters,
                                    max_results=limit + 1,
                                )
                                if kind == "ghsa" and any(
                                    hit.get("cve_id") != query
                                    and query not in hit.get("aliases", [])
                                    for hit in raw
                                ):
                                    # Upstream alias lookup uses substring LIKE.
                                    # Never silently accept a near-ID or call it clean.
                                    raise VDBError("vdb_alias_match_inexact")
                        finally:
                            db.db_conn = db.index_conn = None
                            db.bound_paths = None
                            db.tables_created = False
                    matches = [self._match(hit) for hit in raw[:limit]]
                    for match in matches:
                        match.match_basis = {
                            "cve": "advisory_id",
                            "purl": "versioned_purl",
                            "cpe": "concrete_cpe",
                            "ghsa": (
                                "advisory_id" if match.advisory_id == query else "advisory_alias"
                            ),
                        }[kind]
                    output = VDBLookup(
                        database=status,
                        query=query,
                        query_kind=kind,
                        requested_sources=sources or [],
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
                if time.monotonic() >= deadline:
                    raise VDBError(timeout_error_code) from None
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
            description=text("description"),
            text_truncated=any(
                (isinstance(value, str) and len(value) > 1024)
                or (
                    isinstance(value, list)
                    and (
                        len(value) > 20 or any(isinstance(v, str) and len(v) > 1024 for v in value)
                    )
                )
                for value in hit.values()
            ),
        )


def _lookup_worker_environment(module_root: str) -> dict[str, str]:
    # Do not pass credentials or the parent's VDB settings into the worker.
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": module_root,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _join_worker_threads_until(
    reader: Thread,
    reader_started: bool,
    writer: Thread,
    writer_started: bool,
    deadline: float,
) -> bool:
    for worker_thread, started in ((writer, writer_started), (reader, reader_started)):
        if started:
            try:
                worker_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            except RuntimeError:
                pass
    return (not writer_started or not writer.is_alive()) and (
        not reader_started or not reader.is_alive()
    )


def _close_stopped_worker_pipes(
    worker_stdin: IO[bytes],
    worker_stdout: IO[bytes],
    reader: Thread,
    reader_started: bool,
    writer: Thread,
    writer_started: bool,
) -> None:
    # Closing a pipe while its reader/writer thread is blocked can itself hang.
    if not writer_started or not writer.is_alive():
        try:
            worker_stdin.close()
        except (OSError, ValueError):
            pass
    if not reader_started or not reader.is_alive():
        try:
            worker_stdout.close()
        except (OSError, ValueError):
            pass


def _stop_lookup_worker_bounded(
    process: subprocess.Popen[bytes],
    worker_stdin: IO[bytes],
    worker_stdout: IO[bytes],
    reader: Thread,
    reader_started: bool,
    writer: Thread,
    writer_started: bool,
) -> bool:
    cleanup_deadline = time.monotonic() + _LOOKUP_WORKER_CLEANUP_SECONDS
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass

    exited = False
    try:
        process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
        exited = True
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        try:
            exited = process.poll() is not None
        except OSError:
            pass

    _join_worker_threads_until(
        reader, reader_started, writer, writer_started, cleanup_deadline
    )
    reader_stopped = not reader_started or not reader.is_alive()
    writer_stopped = not writer_started or not writer.is_alive()
    _close_stopped_worker_pipes(
        worker_stdin, worker_stdout, reader, reader_started, writer, writer_started
    )
    return exited and reader_stopped and writer_stopped


def _lookup_worker_stopped(
    process: subprocess.Popen[bytes], reader: Thread, reader_started: bool,
    writer: Thread, writer_started: bool,
) -> bool:
    """Nonblocking termination proof; poll also reaps an exited OS child."""
    if process.poll() is None:
        return False
    if (reader_started and reader.is_alive()) or (writer_started and writer.is_alive()):
        return False
    for pipe in (process.stdin, process.stdout):
        if pipe is not None:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    return True


def _invoke_lookup_worker(
    request: _LookupWorkerRequest,
    deadline: float,
    timeout_error_code: Literal["vdb_lookup_unavailable", "vdb_batch_timeout"],
) -> VDBLookup:
    payload = request.model_dump_json().encode("utf-8")
    if len(payload) > _LOOKUP_WORKER_REQUEST_BYTES:
        raise VDBError("vdb_lookup_unavailable")

    module_root = str(Path(__file__).resolve().parents[3])
    argv = [sys.executable, "-m", "redteam.capability.servers.vdb_readonly", "--lookup-worker"]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=module_root,
            env=_lookup_worker_environment(module_root),
            bufsize=0,
        )
    except OSError:
        raise VDBError("vdb_lookup_unavailable") from None
    if process.stdin is None or process.stdout is None:
        cleanup_deadline = time.monotonic() + _LOOKUP_WORKER_CLEANUP_SECONDS
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        exited = False
        try:
            process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            exited = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            try:
                exited = process.poll() is not None
            except OSError:
                pass
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        if not exited:
            raise _LookupWorkerCleanupUncertain(lambda: process.poll() is not None) from None
        raise VDBError("vdb_lookup_unavailable")

    worker_stdin = process.stdin
    worker_stdout = process.stdout
    output = bytearray()
    output_too_large = threading.Event()

    def read_output() -> None:
        while True:
            room = _LOOKUP_WORKER_OUTPUT_BYTES + 1 - len(output)
            if room <= 0:
                output_too_large.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            chunk = worker_stdout.read(min(8192, room))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > _LOOKUP_WORKER_OUTPUT_BYTES:
                output_too_large.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return

    def write_input() -> None:
        try:
            pending = memoryview(payload)
            while pending:
                written = worker_stdin.write(pending)
                if not written:
                    break
                pending = pending[written:]
        except OSError:
            pass
        finally:
            try:
                worker_stdin.close()
            except OSError:
                pass

    reader = Thread(target=read_output, daemon=True)
    writer = Thread(target=write_input, daemon=True)
    reader_started = False
    writer_started = False

    def stopped() -> bool:
        return _lookup_worker_stopped(process, reader, reader_started, writer, writer_started)

    try:
        reader.start()
        reader_started = True
        writer.start()
        writer_started = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, 0)
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        if not _stop_lookup_worker_bounded(
            process, worker_stdin, worker_stdout, reader, reader_started, writer, writer_started
        ):
            raise _LookupWorkerCleanupUncertain(stopped) from None
        raise VDBError(timeout_error_code) from None
    except (OSError, RuntimeError):
        if not _stop_lookup_worker_bounded(
            process, worker_stdin, worker_stdout, reader, reader_started, writer, writer_started
        ):
            raise _LookupWorkerCleanupUncertain(stopped) from None
        raise VDBError("vdb_lookup_unavailable") from None

    if not _join_worker_threads_until(reader, reader_started, writer, writer_started, deadline):
        if not _stop_lookup_worker_bounded(
            process, worker_stdin, worker_stdout, reader, reader_started, writer, writer_started
        ):
            raise _LookupWorkerCleanupUncertain(stopped) from None
        raise VDBError(timeout_error_code) from None
    _close_stopped_worker_pipes(
        worker_stdin, worker_stdout, reader, reader_started, writer, writer_started
    )

    if return_code != 0 or output_too_large.is_set():
        raise VDBError("vdb_lookup_unavailable")
    try:
        response: object = json.loads(output)
        if not isinstance(response, dict) or set(response) not in ({"lookup"}, {"error_code"}):
            raise ValueError("invalid lookup worker response")
        if "error_code" in response:
            code = response["error_code"]
            if not isinstance(code, str) or not re.fullmatch(r"vdb_[a-z0-9_]+", code):
                raise ValueError("invalid lookup worker error")
            raise VDBError(code)
        return VDBLookup.model_validate(response["lookup"])
    except VDBError:
        raise
    except Exception:
        raise VDBError("vdb_lookup_unavailable") from None


def _lookup_worker_main() -> int:
    raw = sys.stdin.buffer.read(_LOOKUP_WORKER_REQUEST_BYTES + 1)
    response: dict[str, object]
    if len(raw) > _LOOKUP_WORKER_REQUEST_BYTES:
        response = {"error_code": "vdb_lookup_unavailable"}
    else:
        try:
            request = _LookupWorkerRequest.model_validate_json(raw)
            reader = VDBReader(request.policy)
            with contextlib.redirect_stdout(sys.stderr):
                result = reader._lookup_in_worker(
                    request.kind,
                    request.query,
                    request.limit,
                    request.sources,
                    request.timeout_seconds,
                    request.timeout_error_code,
                )
            response = {"lookup": result.model_dump(mode="json")}
        except VDBError as exc:
            code = str(exc)
            response = {
                "error_code": code if re.fullmatch(r"vdb_[a-z0-9_]+", code)
                else "vdb_lookup_unavailable"
            }
        except Exception:
            response = {"error_code": "vdb_lookup_unavailable"}

    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _LOOKUP_WORKER_OUTPUT_BYTES:
        encoded = b'{"error_code":"vdb_lookup_unavailable"}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0

def create_server(policy: VDBPolicy, *, extended: bool = False) -> MCPServer:
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
        server.tool(meta={PROVIDER_METADATA_RESOURCE_EFFECTS: []})(function)
    if extended:
        from redteam.capability.servers.vdb_extended import register_extended_tools

        register_extended_tools(server, policy)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy")
    parser.add_argument("--lookup-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--profile", choices=("vdb_readonly", "vdb_readonly_extended"), default="vdb_readonly"
    )
    args = parser.parse_args()
    if args.lookup_worker:
        raise SystemExit(_lookup_worker_main())
    if args.policy is None:
        parser.error("--policy is required")
    if args.profile == "vdb_readonly_extended":
        from redteam.capability.servers.vdb_extended import VDBExtendedPolicy

        policy = VDBExtendedPolicy.load(args.policy)
    else:
        policy = VDBPolicy.load(args.policy)
    create_server(policy, extended=args.profile == "vdb_readonly_extended").run(transport="stdio")


if __name__ == "__main__":
    main()
