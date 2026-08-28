from pathlib import Path

from whs_redteam.api_schema import infer_json_schema, infer_query_schema
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ApiRequestSchemaRecord,
    EndpointRecord,
    IdentifierLocation,
)
from whs_redteam.resource_model import model_resources, template_endpoint_url


def test_resource_model_finds_path_query_and_nested_body_identifiers() -> None:
    endpoints = [
        EndpointRecord(
            url="https://example.test/api/orders/123",
            method="GET",
            resource_type="fetch",
        ),
        EndpointRecord(
            url="https://example.test/api/orders/456",
            method="GET",
            resource_type="fetch",
        ),
    ]
    schemas = [
        ApiRequestSchemaRecord(
            endpoint_url="https://example.test/api/orders/123",
            method="GET",
            resource_type="fetch",
            query_schema=infer_query_schema(
                "https://example.test/api/orders/123?order_id=456&page=1"
            ),
            body_schema=infer_json_schema(
                {"ownerId": "user-1", "line": {"product_id": 7}}
            ),
        )
    ]

    resources = model_resources(endpoints, schemas)

    assert len(resources) == 1
    resource = resources[0]
    assert resource.endpoint_template == (
        "https://example.test/api/orders/{resource_id}"
    )
    locations = {
        (item.location, item.schema_path) for item in resource.identifiers
    }
    assert (IdentifierLocation.PATH, "path[3]") in locations
    assert (IdentifierLocation.QUERY, "query.order_id") in locations
    assert (IdentifierLocation.BODY, "body.ownerId") in locations
    assert (IdentifierLocation.BODY, "body.line.product_id") in locations
    assert all("page" not in item.schema_path for item in resource.identifiers)


def test_static_api_versions_are_not_resource_identifiers() -> None:
    resources = model_resources(
        [
            EndpointRecord(
                url="https://example.test/api/v1/orders",
                method="GET",
                resource_type="fetch",
            )
        ],
        [],
    )
    assert resources == []


def test_different_collection_routes_are_not_treated_as_dynamic_ids() -> None:
    resources = model_resources(
        [
            EndpointRecord(
                url="https://example.test/api/users",
                method="GET",
                resource_type="fetch",
            ),
            EndpointRecord(
                url="https://example.test/api/orders",
                method="GET",
                resource_type="fetch",
            ),
        ],
        [],
    )
    assert resources == []


def test_route_status_words_are_not_treated_as_dynamic_ids() -> None:
    resources = model_resources(
        [
            EndpointRecord(
                url="https://example.test/api/orders/pending",
                method="GET",
                resource_type="fetch",
            ),
            EndpointRecord(
                url="https://example.test/api/orders/completed",
                method="GET",
                resource_type="fetch",
            ),
        ],
        [],
    )
    assert resources == []


def test_high_confidence_path_identifiers_are_templated_for_reports() -> None:
    assert template_endpoint_url(
        "https://example.test/api/orders/123"
    ) == "https://example.test/api/orders/{resource_id}"
    assert template_endpoint_url(
        "https://example.test/reset/raw-reset-secret"
    ) == "https://example.test/reset/{sensitive_value}"


def test_resource_inventory_round_trips_without_observed_values(
    tmp_path: Path,
) -> None:
    resources = model_resources(
        [
            EndpointRecord(
                url="https://example.test/api/orders/123",
                method="GET",
                resource_type="fetch",
            )
        ],
        [],
    )
    with InventoryStore(tmp_path / "inventory.db") as store:
        store.replace_resources("run-1", resources)
        restored = store.list_resources("run-1")
        raw = store.connection.execute(
            "SELECT endpoint_template, identifiers FROM resources"
        ).fetchone()

    assert restored == resources
    assert "123" not in raw["endpoint_template"]
    assert "123" not in raw["identifiers"]
