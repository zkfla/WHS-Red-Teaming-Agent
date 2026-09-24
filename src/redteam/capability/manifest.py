"""Capability-owned candidate metadata manifest (mobile / analysis).

This module is *static reference data* plus the small amount of logic needed to
keep that data honest against reality. It deliberately does **not** modify the
shared gateway or transport: the extra metadata a ``ToolDescriptor`` cannot
carry (target kind, output schema, the required/optional argument split) lives
here, in a Capability-owned file, instead of on the frozen core contract.

Two consumers:

* Tests compare each entry's argument contract against a **live** ``tools/list``
  discovery snapshot via :func:`CandidateManifest.validate_against_discovery`,
  so drift between what we pinned and what a server actually exposes fails
  loudly rather than silently.
* :meth:`CandidateManifest.list_candidate_metadata` returns the enriched,
  read-only view (``target_kind``, ``output_schema``, ...) for anything that
  wants richer selection metadata than the registry snapshot, without reaching
  into the gateway.

The concrete entries live in :mod:`redteam.capability.mobile_analysis_manifest`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from redteam.core.enums import RiskLevel


class ManifestError(ValueError):
    """Raised when the manifest is internally inconsistent."""


class ManifestDiscoveryMismatch(ValueError):
    """Raised when the manifest disagrees with a live discovery snapshot.

    This is the manifest-level analogue of the config/discovery check already
    enforced in ``bootstrap._build_gateway``: an explicitly pinned candidate
    whose real ``tools/list`` schema no longer matches must fail, not degrade.
    """


class TargetKind(str, Enum):
    """What a tool operates on. Not a core enum -- Capability-local metadata."""

    APK_FILE = "apk_file"
    DECODED_PROJECT = "decoded_project"
    ANDROID_DEVICE = "android_device"


class ToolOrigin(str, Enum):
    EXTERNAL = "external"          # a pinned third-party MCP server
    LOCAL_FALLBACK = "local_fallback"  # an in-repo, dependency-light server


@dataclass(frozen=True, slots=True)
class ToolManifestEntry:
    """Ground-truth metadata for one selected remote tool."""

    tool_id: str
    server_id: str
    tool_name: str
    capability: str
    operations: tuple[str, ...]
    required_arguments: tuple[str, ...]
    optional_arguments: tuple[str, ...]
    target_kind: TargetKind | None
    risk: RiskLevel
    origin: ToolOrigin
    known_input_schema: dict[str, Any] | None = None
    known_output_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.operations:
            raise ManifestError(f"{self.tool_id}: at least one canonical operation is required")
        overlap = set(self.required_arguments) & set(self.optional_arguments)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise ManifestError(f"{self.tool_id}: arguments both required and optional: {names}")

    @property
    def argument_names(self) -> frozenset[str]:
        return frozenset(self.required_arguments) | frozenset(self.optional_arguments)

    def metadata(self) -> dict[str, Any]:
        """Enriched, JSON-friendly view (the whitelist-only candidate metadata)."""

        return {
            "tool_id": self.tool_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "capability": self.capability,
            "operations": list(self.operations),
            "required_arguments": list(self.required_arguments),
            "optional_arguments": list(self.optional_arguments),
            "target_kind": self.target_kind.value if self.target_kind else None,
            "risk": self.risk.value,
            "origin": self.origin.value,
            "output_schema": self.known_output_schema,
        }


def argument_contract(input_schema: Mapping[str, Any]) -> tuple[frozenset[str], frozenset[str]]:
    """Split a JSON-Schema ``object`` into (required, optional) property names."""

    properties = input_schema.get("properties")
    property_names = (
        frozenset(properties.keys()) if isinstance(properties, Mapping) else frozenset()
    )
    required = input_schema.get("required")
    required_names = frozenset(required) if isinstance(required, (list, tuple)) else frozenset()
    # A schema that lists a name as required but not in properties is still a
    # required argument for our purposes.
    required_names = required_names & property_names if property_names else required_names
    optional_names = property_names - required_names
    return required_names, optional_names


def _discovered_name(tool: Any) -> str:
    if isinstance(tool, Mapping):
        return str(tool["name"])
    return str(tool.name)


def _discovered_schema(tool: Any) -> Mapping[str, Any]:
    if isinstance(tool, Mapping):
        schema = tool.get("input_schema") or tool.get("inputSchema") or {}
    else:
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = getattr(tool, "inputSchema", {})
    return schema if isinstance(schema, Mapping) else {}


class CandidateManifest:
    """An immutable set of :class:`ToolManifestEntry`, keyed by ``tool_id``."""

    def __init__(self, entries: Iterable[ToolManifestEntry]) -> None:
        collected: dict[str, ToolManifestEntry] = {}
        for entry in entries:
            if entry.tool_id in collected:
                raise ManifestError(f"duplicate tool_id in manifest: {entry.tool_id}")
            collected[entry.tool_id] = entry
        self._entries = tuple(collected.values())
        self._by_id = collected

    def entries(self) -> tuple[ToolManifestEntry, ...]:
        return self._entries

    def by_tool_id(self, tool_id: str) -> ToolManifestEntry | None:
        return self._by_id.get(tool_id)

    def by_server(self, server_id: str) -> list[ToolManifestEntry]:
        return [entry for entry in self._entries if entry.server_id == server_id]

    def by_capability(self, capability: str) -> list[ToolManifestEntry]:
        return [entry for entry in self._entries if entry.capability == capability]

    def server_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for entry in self._entries:
            seen.setdefault(entry.server_id, None)
        return tuple(seen)

    def allowed_tools_for_server(self, server_id: str) -> tuple[str, ...]:
        """Remote tool names to put in ``..._ALLOWED_TOOLS_JSON`` for this server."""

        return tuple(entry.tool_name for entry in self.by_server(server_id))

    def tool_operations_for_server(self, server_id: str) -> dict[str, list[str]]:
        """The ``..._TOOL_OPERATIONS_JSON`` binding this manifest expects."""

        return {
            entry.tool_name: list(entry.operations) for entry in self.by_server(server_id)
        }

    def list_candidate_metadata(
        self, *, capability: str | None = None
    ) -> list[dict[str, Any]]:
        """Enriched metadata for every entry (optionally one capability)."""

        entries = self.by_capability(capability) if capability else list(self._entries)
        return [entry.metadata() for entry in entries]

    def validate_against_discovery(
        self, server_id: str, discovered_tools: Iterable[Any]
    ) -> None:
        """Assert the pinned entries for ``server_id`` match a live ``tools/list``.

        ``discovered_tools`` items may be ``RemoteTool``-like objects (``.name``,
        ``.input_schema``) or plain mappings. Raises
        :class:`ManifestDiscoveryMismatch` on the first disagreement.
        """

        entries = self.by_server(server_id)
        if not entries:
            raise ManifestError(f"no manifest entries for server {server_id!r}")

        by_name = {_discovered_name(tool): tool for tool in discovered_tools}
        for entry in entries:
            tool = by_name.get(entry.tool_name)
            if tool is None:
                raise ManifestDiscoveryMismatch(
                    f"{server_id}: pinned tool {entry.tool_name!r} is absent from live discovery"
                )
            live_required, live_optional = argument_contract(_discovered_schema(tool))
            if frozenset(entry.required_arguments) != live_required:
                raise ManifestDiscoveryMismatch(
                    f"{entry.tool_id}: required arguments drifted "
                    f"(manifest={sorted(entry.required_arguments)}, live={sorted(live_required)})"
                )
            if frozenset(entry.optional_arguments) != live_optional:
                raise ManifestDiscoveryMismatch(
                    f"{entry.tool_id}: optional arguments drifted "
                    f"(manifest={sorted(entry.optional_arguments)}, live={sorted(live_optional)})"
                )
