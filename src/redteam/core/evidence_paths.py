"""Shared output-relative pointers into preserved evidence, without I/O."""

from dataclasses import dataclass
from typing import Any, Literal

EVIDENCE_FIELD_CONTRACT_VERSION = 2

RAW_PATH_RULE = (
    "The stored raw payload is the full ToolResult dict, and the pointer "
    "addresses its preserved output: a segment is a dict key or a list "
    "index, written as decimal text or as an integer. The output-relative "
    "form lists keys under that output; the equivalent envelope-prefixed "
    "form may start with the envelope key 'output', which is consumed once "
    "(['output'] addresses the whole output, as does an empty path)."
)

REVIEW_FIELD_RULE = (
    RAW_PATH_RULE + " Scalars are read as redacted text; objects and arrays are displayed as "
    "bounded scalar leaves with their paths and explicit omissions, never sliced JSON. "
    "text_span addresses characters of a redacted string only."
)


def validate_raw_path(path: list[str | int]) -> list[str | int]:
    """One coordinate contract for assessment storage and field readers."""
    if not isinstance(path, list) or len(path) > 32 or any(
        type(part) not in (str, int)
        or (isinstance(part, int) and part < 0)
        or (isinstance(part, str) and (
            len(part) > 512 or (part.startswith("-") and part[1:].isdigit())
        )) for part in path
    ):
        raise ValueError("invalid field path")
    return path


@dataclass(frozen=True)
class OutputField:
    """Resolution over an already redacted output; no I/O or verdict authority."""

    status: Literal["ok", "field_unavailable", "text_span_requires_text",
                    "text_span_out_of_bounds"]
    value: Any = None


def resolve_output_field(
    output: Any, path: list[str | int], *,
    text_offset: int | None = None, text_length: int | None = None,
) -> OutputField:
    validate_raw_path(path)
    found, value = walk_output(output, path)
    if not found:
        return OutputField("field_unavailable")
    if text_offset is not None or text_length is not None:
        if not isinstance(value, str):
            return OutputField("text_span_requires_text")
        if (type(text_offset) is not int or type(text_length) is not int
                or text_offset < 0 or text_length <= 0
                or text_offset + text_length > len(value)):
            return OutputField("text_span_out_of_bounds")
    return OutputField("ok", value)


def _locate(raw: Any, path: list[str | int]) -> tuple[bool, Any]:
    node = raw
    for part in path:
        if isinstance(node, dict):
            key = part if isinstance(part, str) else str(part)
            if key not in node:
                return False, None
            node = node[key]
        elif isinstance(node, (list, tuple)):
            try:
                node = node[part if isinstance(part, int) else int(part)]
            except (IndexError, TypeError, ValueError):
                return False, None
        else:
            return False, None
    return True, node


def walk_output(output: Any, path: list[str | int]) -> tuple[bool, Any]:
    """Try the exact pointer first; drop one leading output key only on failure."""
    found, value = _locate(output, path)
    if found:
        return found, value
    if path and path[0] == "output":
        return _locate(output, path[1:])
    return False, None
