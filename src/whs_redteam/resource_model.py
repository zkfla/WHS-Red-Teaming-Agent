"""Infer value-free resource and identifier models from observed API metadata."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from whs_redteam.models import (
    ApiRequestSchemaRecord,
    EndpointRecord,
    IdentifierLocation,
    ObservedSchemaNode,
    ObservedValueType,
    ResourceIdentifier,
    ResourceRecord,
)

INTEGER_ID = re.compile(r"^[1-9][0-9]{0,18}$")
UUID_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
ULID_ID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")
STATIC_SPECIAL = re.compile(r"^(?:v[0-9]+|[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})$")
STATIC_ROUTE_WORDS = frozenset(
    {
        "active",
        "archive",
        "archived",
        "cancelled",
        "completed",
        "create",
        "draft",
        "failed",
        "new",
        "pending",
        "recent",
        "search",
        "status",
        "summary",
    }
)
SENSITIVE_PATH_PREDECESSORS = frozenset(
    {"invite", "magic", "password", "recover", "reset", "token", "verify"}
)


def model_resources(
    endpoints: Iterable[EndpointRecord],
    schemas: Iterable[ApiRequestSchemaRecord],
) -> list[ResourceRecord]:
    """Build deterministic records without retaining query or body values."""

    endpoint_list = list(endpoints)
    schema_list = list(schemas)
    inferred_templates = _variant_templates(endpoint_list)
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}

    for endpoint in endpoint_list:
        template, path_identifiers = _path_template(endpoint.url)
        template = inferred_templates.get(
            (endpoint.url, endpoint.method, endpoint.resource_type), template
        )
        if template != endpoint.url and not path_identifiers:
            path_identifiers = (
                ResourceIdentifier(
                    name="resource_id",
                    location=IdentifierLocation.PATH,
                    schema_path=_template_path(template),
                    confidence=0.75,
                    reason="the same observed route position contained distinct segments",
                ),
            )
        _merge_resource(
            grouped,
            template,
            endpoint.method,
            endpoint.resource_type,
            path_identifiers,
            endpoint.url,
        )

    for schema in schema_list:
        template, path_identifiers = _path_template(schema.endpoint_url)
        template = inferred_templates.get(
            (schema.endpoint_url, schema.method, schema.resource_type), template
        )
        identifiers = list(path_identifiers)
        identifiers.extend(
            ResourceIdentifier(
                name=name,
                location=IdentifierLocation.QUERY,
                schema_path=f"query.{name}",
                confidence=0.85,
                reason="the observed query field name denotes an object identifier",
            )
            for name in schema.query_schema
            if _identifier_name(name)
        )
        if schema.body_schema is not None:
            identifiers.extend(_body_identifiers(schema.body_schema))
        _merge_resource(
            grouped,
            template,
            schema.method,
            schema.resource_type,
            tuple(identifiers),
            schema.endpoint_url,
        )

    records: list[ResourceRecord] = []
    for (template, method, resource_type), value in sorted(grouped.items()):
        identifiers = tuple(
            sorted(
                value["identifiers"].values(),
                key=lambda item: (item.location.value, item.schema_path, item.name),
            )
        )
        if not identifiers:
            continue
        records.append(
            ResourceRecord(
                endpoint_template=template,
                method=method,
                resource_type=resource_type,
                identifiers=identifiers,
                observed_endpoint_count=len(value["sources"]),
            )
        )
    return records


def template_endpoint_url(url: str) -> str:
    """Replace high-confidence path identifiers for reports and evidence."""

    templated = _path_template(url)[0]
    parsed = urlsplit(templated)
    segments = parsed.path.split("/")
    for index in range(1, len(segments)):
        if segments[index - 1].lower() in SENSITIVE_PATH_PREDECESSORS:
            segments[index] = "{sensitive_value}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/".join(segments) or "/", "", "")
    )


def _merge_resource(
    grouped: dict[tuple[str, str, str], dict[str, object]],
    template: str,
    method: str,
    resource_type: str,
    identifiers: tuple[ResourceIdentifier, ...],
    observed_url: str,
) -> None:
    key = (template, method.upper(), resource_type)
    value = grouped.setdefault(key, {"sources": set(), "identifiers": {}})
    sources = value["sources"]
    assert isinstance(sources, set)
    sources.add(observed_url)
    identifier_map = value["identifiers"]
    assert isinstance(identifier_map, dict)
    for identifier in identifiers:
        identity = (identifier.location.value, identifier.schema_path, identifier.name)
        previous = identifier_map.get(identity)
        if previous is None or previous.confidence < identifier.confidence:
            identifier_map[identity] = identifier


def _path_template(url: str) -> tuple[str, tuple[ResourceIdentifier, ...]]:
    parsed = urlsplit(url)
    segments = parsed.path.split("/")
    identifiers: list[ResourceIdentifier] = []
    for index, segment in enumerate(segments):
        confidence, reason = _path_id_reason(segment)
        if confidence == 0:
            continue
        segments[index] = "{resource_id}"
        identifiers.append(
            ResourceIdentifier(
                name="resource_id",
                location=IdentifierLocation.PATH,
                schema_path=f"path[{index}]",
                confidence=confidence,
                reason=reason,
            )
        )
    path = "/".join(segments) or "/"
    return (
        urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
        tuple(identifiers),
    )


def _path_id_reason(segment: str) -> tuple[float, str]:
    if STATIC_SPECIAL.fullmatch(segment):
        return 0, ""
    if INTEGER_ID.fullmatch(segment):
        return 0.9, "the observed path segment has an integer identifier shape"
    if UUID_ID.fullmatch(segment):
        return 0.98, "the observed path segment has a UUID identifier shape"
    if ULID_ID.fullmatch(segment):
        return 0.96, "the observed path segment has a ULID identifier shape"
    if OBJECT_ID.fullmatch(segment):
        return 0.9, "the observed path segment has an object identifier shape"
    return 0, ""


def _variant_templates(
    endpoints: list[EndpointRecord],
) -> dict[tuple[str, str, str], str]:
    buckets: dict[tuple[str, str, str, int], list[EndpointRecord]] = defaultdict(list)
    for endpoint in endpoints:
        parsed = urlsplit(endpoint.url)
        segments = tuple(segment for segment in parsed.path.split("/") if segment)
        buckets[
            (parsed.scheme, parsed.netloc, endpoint.method.upper(), len(segments))
        ].append(endpoint)
    result: dict[tuple[str, str, str], str] = {}
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        paths = [
            tuple(segment for segment in urlsplit(item.url).path.split("/") if segment)
            for item in bucket
        ]
        changing = [
            index
            for index in range(len(paths[0]))
            if len({path[index] for path in paths}) > 1
        ]
        if len(changing) != 1 or changing[0] < 2:
            continue
        index = changing[0]
        if paths[0][index - 1].lower() in {"api", "v1", "v2", "v3"}:
            continue
        if any(
            STATIC_SPECIAL.fullmatch(path[index])
            or path[index].lower() in STATIC_ROUTE_WORDS
            for path in paths
        ):
            continue
        for endpoint, path in zip(bucket, paths, strict=True):
            segments = list(path)
            segments[index] = "{resource_id}"
            parsed = urlsplit(endpoint.url)
            template = urlunsplit(
                (parsed.scheme, parsed.netloc, "/" + "/".join(segments), "", "")
            )
            result[(endpoint.url, endpoint.method, endpoint.resource_type)] = template
    return result


def _identifier_name(name: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return normalized in {"id", "uuid", "resource_id", "object_id"} or normalized.endswith(
        "_id"
    )


def _body_identifiers(
    node: ObservedSchemaNode,
    path: str = "body",
) -> tuple[ResourceIdentifier, ...]:
    result: list[ResourceIdentifier] = []
    if node.type is ObservedValueType.OBJECT:
        for name, child in node.properties.items():
            child_path = f"{path}.{name}"
            if _identifier_name(name):
                result.append(
                    ResourceIdentifier(
                        name=name,
                        location=IdentifierLocation.BODY,
                        schema_path=child_path,
                        confidence=0.85,
                        reason="the observed body field name denotes an object identifier",
                    )
                )
            result.extend(_body_identifiers(child, child_path))
    elif node.type is ObservedValueType.ARRAY and node.items is not None:
        result.extend(_body_identifiers(node.items, f"{path}[]"))
    return tuple(result)


def _template_path(template: str) -> str:
    segments = urlsplit(template).path.split("/")
    index = segments.index("{resource_id}")
    return f"path[{index}]"
