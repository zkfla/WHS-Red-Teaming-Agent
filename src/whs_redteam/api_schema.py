"""Value-free API request schema inference and merging."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from whs_redteam.models import ObservedSchemaNode, ObservedValueType

INTEGER_PATTERN = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
NUMBER_PATTERN = re.compile(
    r"^-?(?:(?:0|[1-9][0-9]*)\.[0-9]+|(?:0|[1-9][0-9]*)[eE][+-]?[0-9]+)$"
)
MAX_SCHEMA_DEPTH = 6
MAX_SCHEMA_PROPERTIES = 100
MAX_SCHEMA_ARRAY_ITEMS = 20
MAX_SCHEMA_NAME_LENGTH = 128


def canonical_endpoint_url(url: str) -> str:
    """Keep endpoint identity while dropping query values and fragments."""

    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (parsed.scheme.lower() == "http" and parsed.port == 80) or (
        parsed.scheme.lower() == "https" and parsed.port == 443
    )
    netloc = display_host
    if parsed.port is not None and not default_port:
        netloc = f"{display_host}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", "", "")
    )


def infer_query_schema(url: str) -> dict[str, ObservedSchemaNode]:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    result: dict[str, ObservedSchemaNode] = {}
    for name, observed in list(values.items())[:MAX_SCHEMA_PROPERTIES]:
        safe_name = name[:MAX_SCHEMA_NAME_LENGTH]
        nodes = tuple(
            _infer_query_value(value)
            for value in observed[:MAX_SCHEMA_ARRAY_ITEMS]
        )
        if len(nodes) == 1:
            result[safe_name] = nodes[0]
        else:
            result[safe_name] = ObservedSchemaNode(
                type=ObservedValueType.ARRAY,
                items=_merge_many(nodes),
            )
    return result


def infer_json_schema(value: Any) -> ObservedSchemaNode:
    return _infer_json_schema(value, depth=0)


def _infer_json_schema(value: Any, *, depth: int) -> ObservedSchemaNode:
    if depth >= MAX_SCHEMA_DEPTH:
        return ObservedSchemaNode(type=ObservedValueType.UNKNOWN)
    if value is None:
        return ObservedSchemaNode(type=ObservedValueType.NULL)
    if isinstance(value, bool):
        return ObservedSchemaNode(type=ObservedValueType.BOOLEAN)
    if isinstance(value, int):
        return ObservedSchemaNode(type=ObservedValueType.INTEGER)
    if isinstance(value, float):
        return ObservedSchemaNode(type=ObservedValueType.NUMBER)
    if isinstance(value, str):
        return ObservedSchemaNode(type=ObservedValueType.STRING)
    if isinstance(value, Mapping):
        return ObservedSchemaNode(
            type=ObservedValueType.OBJECT,
            properties={
                str(key)[:MAX_SCHEMA_NAME_LENGTH]: _infer_json_schema(
                    item, depth=depth + 1
                )
                for key, item in list(value.items())[:MAX_SCHEMA_PROPERTIES]
            },
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        nodes = tuple(
            _infer_json_schema(item, depth=depth + 1)
            for item in value[:MAX_SCHEMA_ARRAY_ITEMS]
        )
        return ObservedSchemaNode(
            type=ObservedValueType.ARRAY,
            items=(
                _merge_many(nodes)
                if nodes
                else ObservedSchemaNode(type=ObservedValueType.UNKNOWN)
            ),
        )
    return ObservedSchemaNode(type=ObservedValueType.UNKNOWN)


def merge_schema_nodes(
    left: ObservedSchemaNode, right: ObservedSchemaNode
) -> ObservedSchemaNode:
    if left.type is not right.type:
        return ObservedSchemaNode(type=ObservedValueType.MIXED)
    if left.type is ObservedValueType.OBJECT:
        properties = dict(left.properties)
        for name, node in right.properties.items():
            properties[name] = (
                merge_schema_nodes(properties[name], node)
                if name in properties
                else node
            )
        return ObservedSchemaNode(
            type=ObservedValueType.OBJECT, properties=properties
        )
    if left.type is ObservedValueType.ARRAY:
        if left.items is None:
            return right
        if right.items is None:
            return left
        return ObservedSchemaNode(
            type=ObservedValueType.ARRAY,
            items=merge_schema_nodes(left.items, right.items),
        )
    return left


def merge_query_schemas(
    left: dict[str, ObservedSchemaNode],
    right: dict[str, ObservedSchemaNode],
) -> dict[str, ObservedSchemaNode]:
    merged = dict(left)
    for name, node in right.items():
        merged[name] = (
            merge_schema_nodes(merged[name], node) if name in merged else node
        )
    return merged


def _infer_query_value(value: str) -> ObservedSchemaNode:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return ObservedSchemaNode(type=ObservedValueType.BOOLEAN)
    if INTEGER_PATTERN.fullmatch(value):
        return ObservedSchemaNode(type=ObservedValueType.INTEGER)
    if NUMBER_PATTERN.fullmatch(value):
        return ObservedSchemaNode(type=ObservedValueType.NUMBER)
    return ObservedSchemaNode(type=ObservedValueType.STRING)


def _merge_many(nodes: tuple[ObservedSchemaNode, ...]) -> ObservedSchemaNode:
    if not nodes:
        return ObservedSchemaNode(type=ObservedValueType.UNKNOWN)
    merged = nodes[0]
    for node in nodes[1:]:
        merged = merge_schema_nodes(merged, node)
    return merged
