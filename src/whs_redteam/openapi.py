"""Small, fail-closed OpenAPI 3 importer for value-free endpoint metadata."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

import yaml

from whs_redteam.api_schema import canonical_endpoint_url
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ApiRequestSchemaRecord,
    EndpointRecord,
    ObservedSchemaNode,
    ObservedValueType,
)

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def import_openapi_schemas(
    config: EngagementConfig,
    inventory: InventoryStore,
    run_id: str,
) -> int:
    """Import static OpenAPI query metadata under the configured target roots."""

    if config.openapi_spec_path is None:
        return 0
    path = Path(config.openapi_spec_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read OpenAPI specification: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid OpenAPI YAML: {path}") from exc
    if not isinstance(raw, dict) or not str(raw.get("openapi", "")).startswith("3."):
        raise ValueError("only OpenAPI 3.x specifications are supported")
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI specification has no paths object")

    imported = 0
    for path_template, path_item in sorted(paths.items()):
        if (
            not isinstance(path_template, str)
            or not path_template.startswith("/")
            or "{" in path_template
            or not isinstance(path_item, dict)
        ):
            continue
        path_parameters = _parameters(path_item.get("parameters"))
        for method, operation in sorted(path_item.items()):
            method = str(method).upper()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = path_parameters + _parameters(operation.get("parameters"))
            query_schema: dict[str, ObservedSchemaNode] = {}
            for parameter in parameters:
                if parameter.get("in") != "query" or not isinstance(parameter.get("name"), str):
                    continue
                name = parameter["name"][:128]
                schema = parameter.get("schema")
                if not isinstance(schema, dict):
                    continue
                query_schema[name] = _schema_node(schema)
            for target in config.targets:
                endpoint = canonical_endpoint_url(
                    urljoin(
                        target.rstrip("/") + "/",
                        path_template.lstrip("/"),
                    )
                )
                # Only import paths that are actually inside this engagement target.
                if not config._fixture_url_in_target(endpoint, target):
                    continue
                resource_type = "openapi"
                inventory.save_endpoint(
                    run_id,
                    EndpointRecord(
                        url=endpoint,
                        method=method,
                        resource_type=resource_type,
                    ),
                )
                inventory.save_api_request_schema(
                    run_id,
                    ApiRequestSchemaRecord(
                        endpoint_url=endpoint,
                        method=method,
                        resource_type=resource_type,
                        query_schema=query_schema,
                    ),
                )
                imported += 1
    return imported


def _parameters(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and "$ref" not in item]


def _schema_node(schema: dict[str, object], depth: int = 0) -> ObservedSchemaNode:
    if depth >= 6:
        return ObservedSchemaNode(type=ObservedValueType.UNKNOWN)
    schema_type = schema.get("type")
    if schema_type == "integer":
        return ObservedSchemaNode(type=ObservedValueType.INTEGER)
    if schema_type == "number":
        return ObservedSchemaNode(type=ObservedValueType.NUMBER)
    if schema_type == "boolean":
        return ObservedSchemaNode(type=ObservedValueType.BOOLEAN)
    if schema_type == "string":
        return ObservedSchemaNode(type=ObservedValueType.STRING)
    if schema_type == "array":
        items = schema.get("items")
        return ObservedSchemaNode(
            type=ObservedValueType.ARRAY,
            items=_schema_node(items, depth + 1) if isinstance(items, dict) else None,
        )
    if schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict):
            return ObservedSchemaNode(
                type=ObservedValueType.OBJECT,
                properties={
                    str(name)[:128]: _schema_node(value, depth + 1)
                    for name, value in list(properties.items())[:100]
                    if isinstance(value, dict)
                },
            )
    return ObservedSchemaNode(type=ObservedValueType.UNKNOWN)
