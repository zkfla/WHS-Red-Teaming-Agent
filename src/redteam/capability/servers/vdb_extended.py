"""Opt-in bounded VDB knowledge queries; no remote fetch or arbitrary SBOM paths."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.capability.servers.vdb_readonly import (
    GHSA_PATTERN,
    VDBError,
    VDBLookup,
    VDBMatch,
    VDBPolicy,
    VDBReader,
    VDBStatus,
)
from redteam.capability.servers.workspace import (
    WorkspaceError,
    WorkspaceIO,
    WorkspacePolicy,
    _parts,
)

MAX_BATCH_ITEMS = 50
MAX_OUTPUT_BYTES = 200_000
OpaqueRef = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
SourceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
BoundedQuery = Annotated[str, Field(min_length=1, max_length=512)]


class ApprovedSBOM(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: OpaqueRef
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        _parts(value)
        if not value.endswith(".json"):
            raise ValueError("only approved JSON SBOM files are supported")
        return value


class VDBExtendedPolicy(VDBPolicy):
    max_batch_items: int = Field(default=25, ge=1, le=MAX_BATCH_ITEMS)
    batch_timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_sbom_bytes: int = Field(default=64_000, ge=1, le=64_000)
    max_sbom_components: int = Field(default=200, ge=1, le=500)
    sbom_root: str | None = None
    sboms: tuple[ApprovedSBOM, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def approved_assets(self) -> VDBExtendedPolicy:
        if len({asset.ref for asset in self.sboms}) != len(self.sboms):
            raise ValueError("SBOM refs must be unique")
        if self.sboms and self.sbom_root is None:
            raise ValueError("SBOM refs require a separately provisioned root")
        if self.sbom_root is not None:
            root, db_root = Path(self.sbom_root), Path(self.root)
            if (
                not root.is_absolute()
                or root.resolve() != root
                or root == db_root
                or root in db_root.parents
                or db_root in root.parents
            ):
                raise ValueError("SBOM root must be absolute, unlinked and separate from DB")
        return self


class VDBBatchLookup(BaseModel):
    """Database context is shared once by the batch, not duplicated per item."""

    matches: list[VDBMatch]
    truncated: bool
    interpretation: Literal["knowledge_matches", "no_known_match_in_loaded_snapshot"]
    query_kind: Literal["purl"] = "purl"
    requested_sources: list[str] = Field(default_factory=list)
    target_vulnerability_confirmed: Literal[False] = False


class VDBBatchItem(BaseModel):
    index: int
    component_ref: str | None = None
    query: str | None = None
    state: Literal["checked", "error", "not_checked"]
    error_code: str | None = None
    lookup: VDBBatchLookup | None = None


class VDBBatch(BaseModel):
    database: VDBStatus
    items: list[VDBBatchItem]
    total_items: int
    checked_items: int
    error_items: int
    not_checked_items: int
    all_items_checked: bool
    truncated: bool = False
    assessment: Literal["knowledge_only"] = "knowledge_only"
    target_vulnerability_confirmed: Literal[False] = False
    sbom_ref: str | None = None
    sbom_sha256: str | None = None
    offset: int = 0
    next_offset: int | None = None


class VDBExtendedReader(VDBReader):
    def __init__(self, policy: VDBExtendedPolicy):
        super().__init__(policy)
        self.extended_policy = policy
        self.sbom_io = None
        if policy.sbom_root is not None:
            try:
                self.sbom_io = WorkspaceIO(
                    WorkspacePolicy(
                        asset_ref=policy.asset_ref,
                        engagement_id=policy.engagement_id,
                        run_id=policy.run_id,
                        root=policy.sbom_root,
                        max_file_bytes=policy.max_sbom_bytes,
                        max_return_chars=64_000,
                    )
                )
            except (WorkspaceError, OSError):
                raise VDBError("vdb_invalid_sbom_root") from None

    def batch(
        self,
        queries: list[str | None],
        limit: int,
        sources: list[str],
        *,
        component_refs: list[str | None] | None = None,
        offset: int = 0,
        total_items: int | None = None,
        deadline: float | None = None,
    ) -> VDBBatch:
        policy = self.extended_policy
        if not 1 <= len(queries) <= policy.max_batch_items:
            raise VDBError("vdb_invalid_batch_size")
        if isinstance(limit, bool) or not 1 <= limit <= policy.max_results:
            raise VDBError("vdb_invalid_limit")
        if (
            len(sources) > 5
            or len(set(sources)) != len(sources)
            or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", s) for s in sources)
        ):
            raise VDBError("vdb_invalid_sources")
        if component_refs is None:
            component_refs = [None] * len(queries)
        deadline = deadline or time.monotonic() + policy.batch_timeout_seconds
        # Status and all lookups share a cooperative wall-clock SQL budget.
        # Missing data is a per-item NOT CHECKED result, never a clean lookup.
        database = self.status(deadline=deadline)
        items: list[VDBBatchItem] = []
        for i, (query, ref) in enumerate(zip(queries, component_refs, strict=True)):
            item = VDBBatchItem(
                index=offset + i, query=query, component_ref=ref, state="not_checked"
            )
            if query is None:
                item.error_code = "vdb_component_purl_missing"
            elif time.monotonic() >= deadline:
                item.error_code = "vdb_batch_timeout"
            elif database.state != "ready":
                item.error_code = "vdb_database_not_ready"
            else:
                try:
                    lookup = self.lookup("purl", query, limit, sources=sources, deadline=deadline)
                    item.lookup = VDBBatchLookup(
                        matches=lookup.matches,
                        truncated=lookup.truncated,
                        interpretation=lookup.interpretation,
                        requested_sources=sources,
                    )
                    item.state = "checked"
                except VDBError as exc:
                    # Fixed backend codes only. No snapshot paths or raw exceptions.
                    item.error_code = str(exc)
                    item.state = "not_checked" if str(exc) == "vdb_batch_timeout" else "error"
            if item.lookup and item.lookup.matches:
                # Bound each item's retained rows before collecting the batch.
                # Avoid repeated serialization/removal over multi-MB results.
                matches = item.lookup.matches
                item.lookup.matches = []
                remaining = (MAX_OUTPUT_BYTES - 32_768) // len(queries)
                remaining -= len(item.model_dump_json().encode("utf-8"))
                for match in matches:
                    size = len(match.model_dump_json().encode("utf-8")) + 1
                    if size > remaining:
                        item.lookup.truncated = True
                        break
                    item.lookup.matches.append(match)
                    remaining -= size
            items.append(item)
        total = len(items) if total_items is None else total_items
        checked = sum(item.state == "checked" for item in items)
        output = VDBBatch(
            database=database,
            items=items,
            total_items=total,
            checked_items=checked,
            error_items=sum(item.state == "error" for item in items),
            not_checked_items=total - checked - sum(item.state == "error" for item in items),
            all_items_checked=checked == total,
            offset=offset,
            next_offset=offset + len(items) if offset + len(items) < total else None,
            truncated=(
                offset != 0
                or len(items) < total
                or any(item.lookup and item.lookup.truncated for item in items)
            ),
        )
        # Keep every requested item's status; trim evidence rows, not JSON or items.
        # The knowledge_matches interpretation survives truncation even if zero
        # matching rows fit. Consumers must honor lookup.truncated.
        while len(output.model_dump_json().encode("utf-8")) > MAX_OUTPUT_BYTES - 1024:
            largest = max(
                (item for item in items if item.lookup and item.lookup.matches),
                key=lambda item: len(item.lookup.model_dump_json().encode("utf-8")),
                default=None,
            )
            if largest is None:
                raise VDBError("vdb_output_limit")
            largest.lookup.matches.pop()
            largest.lookup.truncated = output.truncated = True
        return output

    def sbom(
        self,
        sbom_ref: str,
        offset: int,
        max_items: int,
        limit: int,
        sources: list[str],
    ) -> VDBBatch:
        policy = self.extended_policy
        deadline = time.monotonic() + policy.batch_timeout_seconds
        asset = next((asset for asset in policy.sboms if asset.ref == sbom_ref), None)
        if asset is None or self.sbom_io is None:
            raise VDBError("vdb_sbom_not_approved")
        if (
            isinstance(offset, bool)
            or offset < 0
            or isinstance(max_items, bool)
            or not 1 <= max_items <= policy.max_batch_items
        ):
            raise VDBError("vdb_invalid_sbom_page")
        try:
            document = self.sbom_io.read_file(
                policy.asset_ref,
                asset.path,
                0,
                64_000,
                policy.engagement_id,
                policy.run_id,
            )
            if document.truncated:
                raise VDBError("vdb_sbom_too_large")
            if document.sha256 != asset.sha256:
                raise VDBError("vdb_sbom_digest_mismatch")
            bom = json.loads(document.content)
            if (
                not isinstance(bom, dict)
                or bom.get("bomFormat") != "CycloneDX"
                or bom.get("specVersion") not in {"1.4", "1.5", "1.6"}
                or not isinstance(bom.get("components"), list)
            ):
                raise VDBError("vdb_invalid_sbom")
            pending = list(reversed(bom["components"]))
            metadata = bom.get("metadata", {})
            if not isinstance(metadata, dict):
                raise VDBError("vdb_invalid_sbom")
            if "component" in metadata:
                pending.append(metadata["component"])
            components = []
            while pending:
                if len(components) + len(pending) > policy.max_sbom_components:
                    raise VDBError("vdb_sbom_component_limit")
                component = pending.pop()
                if not isinstance(component, dict):
                    raise VDBError("vdb_invalid_sbom")
                nested = component.get("components", [])
                if not isinstance(nested, list):
                    raise VDBError("vdb_invalid_sbom")
                pending.extend(reversed(nested))
                components.append(component)
            if not components or offset >= len(components):
                raise VDBError("vdb_invalid_sbom_page")
            queries, refs = [], []
            for component in components[offset : offset + max_items]:
                purl = component.get("purl")
                if purl is not None and (not isinstance(purl, str) or len(purl) > 512):
                    raise VDBError("vdb_invalid_sbom")
                ref = component.get("bom-ref")
                if ref is not None and (not isinstance(ref, str) or len(ref) > 128):
                    raise VDBError("vdb_invalid_sbom")
                queries.append(purl)
                refs.append(ref)
        except VDBError:
            raise
        except (WorkspaceError, OSError):
            raise VDBError("vdb_sbom_access_denied") from None
        except (ValueError, RecursionError):
            raise VDBError("vdb_invalid_sbom") from None
        result = self.batch(
            queries,
            limit,
            sources,
            component_refs=refs,
            offset=offset,
            total_items=len(components),
            deadline=deadline,
        )
        result.sbom_ref, result.sbom_sha256 = sbom_ref, document.sha256
        return result


def register_extended_tools(server: MCPServer, policy: VDBPolicy) -> None:
    if not isinstance(policy, VDBExtendedPolicy):
        raise VDBError("vdb_extended_policy_required")
    reader = VDBExtendedReader(policy)

    def search_by_ghsa(
        asset_ref: str,
        ghsa_id: str,
        limit: int = 10,
        sources: tuple[str, ...] = (),
        engagement_id: str = "",
        run_id: str = "",
    ) -> VDBLookup:
        """Exact GHSA knowledge lookup; refuses missing/partial metadata, no target verdict."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.lookup("ghsa", ghsa_id, limit, sources=list(sources))

    def search_by_cpe_like(
        asset_ref: str,
        cpe: str,
        limit: int = 10,
        sources: tuple[str, ...] = (),
        engagement_id: str = "",
        run_id: str = "",
    ) -> VDBLookup:
        """Observed concrete CPE 2.3 vendor/product/version only; never infers product/version."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.lookup("cpe", cpe, limit, sources=list(sources))

    def search_packages(
        asset_ref: str,
        purls: list[str],
        limit: int = 10,
        sources: tuple[str, ...] = (),
        engagement_id: str = "",
        run_id: str = "",
    ) -> VDBBatch:
        """Bounded versioned PURL list; per-item errors/NOT CHECKED, no network or safe verdict."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.batch(purls, limit, list(sources))

    def search_sbom(
        asset_ref: str,
        sbom_ref: str,
        offset: int = 0,
        max_items: int = 25,
        limit: int = 10,
        sources: tuple[str, ...] = (),
        engagement_id: str = "",
        run_id: str = "",
    ) -> VDBBatch:
        """Page a policy-approved digest-pinned CycloneDX JSON; no paths, URL fetch or install."""
        reader.bind(asset_ref, engagement_id, run_id)
        return reader.sbom(sbom_ref, offset, max_items, limit, list(sources))

    for function in (search_by_ghsa, search_by_cpe_like, search_packages, search_sbom):
        function.__annotations__["asset_ref"] = Literal[policy.asset_ref]
        function.__annotations__["limit"] = Annotated[int, Field(ge=1, le=policy.max_results)]
        function.__annotations__["sources"] = Annotated[list[SourceName], Field(max_length=5)]
        if function is search_sbom:
            function.__defaults__ = (
                0,
                min(25, policy.max_batch_items),
                min(10, policy.max_results),
                (),
                "",
                "",
            )
            function.__annotations__["sbom_ref"] = (
                Annotated[
                    OpaqueRef,
                    Field(json_schema_extra={"enum": [asset.ref for asset in policy.sboms]}),
                ]
                if policy.sboms
                else OpaqueRef
            )
            function.__annotations__["offset"] = Annotated[int, Field(ge=0, le=500)]
            function.__annotations__["max_items"] = Annotated[
                int, Field(ge=1, le=policy.max_batch_items)
            ]
        else:
            function.__defaults__ = (min(10, policy.max_results), (), "", "")
        if function is search_by_ghsa:
            function.__annotations__["ghsa_id"] = Annotated[str, Field(pattern=GHSA_PATTERN)]
        if function is search_by_cpe_like:
            function.__annotations__["cpe"] = BoundedQuery
        if function is search_packages:
            function.__annotations__["purls"] = Annotated[
                list[BoundedQuery], Field(min_length=1, max_length=policy.max_batch_items)
            ]
        server.tool()(function)
