"""Reference-candidate extraction shared by mappers and store readers.

This module owns the TargetReference entry shape a mapper emits and the
root knowledge/store layer consumes. A reference candidate is never a fact:
it only says this run preserved origin mentioned this absolute address
at this exact raw location, so a later page read is not blocked as an
unresolved target. Whether the address may be acted on is still decided by
the existing Control scope check on the real follow-up action (known and
allowed are different layers).

Rules honored here, so every producer and consumer agrees:

- Only an absolute http/https URL string actually present in the
  preserved output becomes a candidate. Addresses assembled in code (for
  example joining a relative path onto a base) are never emitted: when the
  joined address differs from the raw value it is not auto-accepted.
- raw_path always starts at output and addresses the value
  inside the preserved ToolResult JSON (evidence.metadata
  tool_result). The consumer re-reads that exact location and only
  admits the candidate on code equality with the target address.
- source_action_id/source_invocation_id are the origin own
  identifiers, never synthesized. A mapper fills what it knows (action and
  invocation travel on the ToolResult); the evidence id is attached by
  the layer that owns the record (UPDATE companion writer).
- No graph node or fact is created here. The TargetRef carries no
  graph_node_id: node identity is derived by the consumer, never
  asserted by the producer.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from redteam.core.contracts.target import AnyTarget, TargetRef, UrlTarget

__all__ = [
    "TargetReference",
    "is_absolute_url",
    "reference_address",
    "is_reference_grounded",
]

_MAX_URL_CHARS = 2048


class TargetReference(BaseModel):
    """One address this run preserved origin actually mentioned.

    target holds the TargetRef JSON (model_dump of a core target
    contract). raw_path locates the address string inside the preserved
    ToolResult JSON starting at output. The source fields echo the
    origin record own identifiers; they stay None while a mapper drafts
    the entry and are filled by the record-owning layer before storing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: TargetRef
    source_evidence_id: str | None = None
    raw_path: list[str | int] = Field(min_length=1)
    source_action_id: str | None = None
    source_invocation_id: str | None = None

    @field_validator("raw_path")
    @classmethod
    def _raw_path_starts_at_output(cls, value: list[str | int]) -> list[str | int]:
        if not value or value[0] != "output":
            raise ValueError("reference raw_path must start at output")
        return value

    @field_validator(
        "source_evidence_id", "source_action_id", "source_invocation_id"
    )
    @classmethod
    def _source_ids_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("reference source ids must not be blank")
        return value


def is_absolute_url(value: Any) -> bool:
    """Whether value is an absolute http(s) URL string usable as-is.

    Only what is actually present may become a candidate: relative paths,
    scheme-less hosts and joined addresses are rejected here, never
    repaired.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_URL_CHARS:
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def reference_address(target: AnyTarget) -> str | None:
    """The concrete address string a reference target points at, if any."""
    if isinstance(target, UrlTarget):
        return target.url
    host = getattr(target, "host", None)
    if isinstance(host, str) and host:
        return host
    domain = getattr(target, "domain", None)
    if isinstance(domain, str) and domain:
        return domain
    return None



def _value_at_path(document: Any, raw_path: list[str | int]) -> tuple[bool, Any]:
    """Read raw_path out of a JSON-shaped document without guessing."""
    node = document
    for part in raw_path:
        if isinstance(part, int):
            if not isinstance(node, list) or not 0 <= part < len(node):
                return False, None
            node = node[part]
        else:
            if not isinstance(node, dict) or part not in node:
                return False, None
            node = node[part]
    return True, node


def is_reference_grounded(
    entry: TargetReference,
    *,
    tool_result: dict[str, Any],
    action_id: str | None,
    invocation_id: str | None,
    evidence_id: str | None = None,
) -> bool:
    """Whether entry echoes its claimed origin exactly.

    The same-origin check the store/register layer applies before admitting
    a candidate: raw_path must start at output and read a value out of the
    preserved ToolResult JSON that code-equals the target address, and the
    entry source ids must equal the origin record own ids. Anything
    assembled, joined or re-pointed fails.
    """
    if not entry.raw_path or entry.raw_path[0] != "output":
        return False
    address = reference_address(entry.target)
    if not address:
        return False
    found, value = _value_at_path(tool_result, entry.raw_path)
    if not found or value != address:
        return False
    if entry.source_action_id is not None and entry.source_action_id != action_id:
        return False
    if entry.source_invocation_id is not None and entry.source_invocation_id != invocation_id:
        return False
    if evidence_id is not None and entry.source_evidence_id is not None and entry.source_evidence_id != evidence_id:
        return False
    return True
