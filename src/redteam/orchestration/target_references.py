"""Addresses cited by stored results, without asserting measured presence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import TargetRef, target_display_name
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import new_id
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.target_binding import network_identity

_TARGET = TypeAdapter(TargetRef)


def resolve_reference(entry: Any, records: dict[str, Any], run_id: str):
    """Resolve an explicit raw location and its original execution binding.

    Returns a target only when the cited value is the actual address. This
    grants neither observation status nor scope permission.
    """
    if not isinstance(entry, dict):
        return None
    source = records.get(str(entry.get("source_evidence_id", "")))
    if source is None or str(source.run_id) != str(run_id):
        return None
    try:
        raw = ToolResult.model_validate(raw_from_metadata(source.metadata))
        proposal = ActionProposal.model_validate(source.metadata.get("proposal"))
        target = _TARGET.validate_python(entry.get("target"))
        if (
            source.source_type is not EvidenceSourceType.TOOL
            or source.action_id != raw.action_id
            or source.invocation_id != raw.invocation_id
            or proposal.action_id != raw.action_id
            or proposal.run_id != source.run_id
            or proposal.engagement_id != source.engagement_id
            or source.metadata.get("tool_id") != str(raw.tool_id)
            or source.metadata.get("action_id") != str(raw.action_id)
            or source.metadata.get("invocation_id") != str(raw.invocation_id)
        ):
            return None
        for key, actual in (
            ("source_action_id", raw.action_id),
            ("source_invocation_id", raw.invocation_id),
        ):
            if entry.get(key) is not None and str(entry[key]) != str(actual):
                return None
        # An address reference cannot nominate an unrelated graph identity.
        canonical = target.model_copy(update={"graph_node_id": None})
        if node_id_for_target(target) != node_id_for_target(canonical):
            return None
        if network_identity(target) is None:
            return None
        path = entry.get("raw_path")
        if not isinstance(path, list) or not path or path[0] != "output":
            return None
        value: Any = raw.model_dump(mode="json")
        for part in path:
            if isinstance(value, dict) and isinstance(part, str):
                value = value[part]
            elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
                value = value[part]
            else:
                return None
        if value != target_display_name(target):
            return None
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return target


def referenced_targets(records: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Read companions; conflicting concrete addresses remain unresolved."""
    targets: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for record in records.values():
        if str(record.run_id) != str(run_id):
            continue
        entries = record.metadata.get("target_references", [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            target = resolve_reference(entry, records, run_id)
            if target is None:
                continue
            identity = str(node_id_for_target(target))
            if identity in targets and targets[identity] != target:
                ambiguous.add(identity)
            else:
                targets[identity] = target
    return {key: value for key, value in targets.items() if key not in ambiguous}


async def register_references(store, runtime, entries: list[Any]) -> list[str]:
    """Persist verified model citations as operational references only."""
    if not isinstance(entries, list):
        raise ValueError("target references must be a list of explicit raw citations")
    records = {
        str(record.evidence_id): record
        for record in await store.list_for_run(runtime.run_id)
        if str(record.engagement_id) == str(runtime.engagement_id)
    }
    accepted = []
    for entry in entries:
        target = resolve_reference(entry, records, str(runtime.run_id))
        if target is None:
            continue
        source = records[str(entry["source_evidence_id"])]
        accepted.append(
            {
                "target": target.model_dump(mode="json"),
                "source_evidence_id": str(source.evidence_id),
                "raw_path": list(entry["raw_path"]),
                "source_action_id": str(source.action_id),
                "source_invocation_id": str(source.invocation_id),
            }
        )
    if accepted:
        payload = {"record_kind": "target_reference", "target_references": accepted}
        await store.append(
            EvidenceRecord(
                evidence_id=new_id("evidence"),
                run_id=runtime.run_id,
                engagement_id=runtime.engagement_id,
                source_type=EvidenceSourceType.SYSTEM,
                source_id="orchestration.target_reference",
                content_type="application/json",
                digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                summary="Addresses referenced by preserved tool output; presence unconfirmed",
                created_at=datetime.now(timezone.utc),
                metadata=payload,
            )
        )
    return [str(node_id_for_target(_TARGET.validate_python(item["target"]))) for item in accepted]
