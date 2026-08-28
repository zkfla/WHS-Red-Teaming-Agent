import json
from pathlib import Path

from whs_redteam.api_schema import (
    canonical_endpoint_url,
    infer_json_schema,
    infer_query_schema,
)
from whs_redteam.audit import AuditLogger
from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ApiRequestSchemaRecord,
    ObservedValueType,
)
from whs_redteam.policy.budget import RequestBudget
from whs_redteam.policy.scope import ScopeGuard
from whs_redteam.tools.browser import WebExplorer
from whs_redteam.tools.network_gate import NetworkGate


def test_query_and_json_inference_keeps_names_and_types_only() -> None:
    query = infer_query_schema(
        "https://example.test/api?customer=alice&limit=10&active=true&tag=a&tag=b"
    )
    body = infer_json_schema(
        {
            "email": "alice@example.test",
            "count": 2,
            "profile": {"verified": False},
            "roles": ["user", "reviewer"],
        }
    )
    serialized = json.dumps(
        {
            "query": {
                name: node.model_dump(mode="json")
                for name, node in query.items()
            },
            "body": body.model_dump(mode="json"),
        }
    )

    assert query["customer"].type is ObservedValueType.STRING
    assert query["limit"].type is ObservedValueType.INTEGER
    assert query["active"].type is ObservedValueType.BOOLEAN
    assert query["tag"].type is ObservedValueType.ARRAY
    assert body.properties["profile"].type is ObservedValueType.OBJECT
    assert body.properties["roles"].items is not None
    assert body.properties["roles"].items.type is ObservedValueType.STRING
    assert "alice" not in serialized
    assert "reviewer" not in serialized


def test_canonical_endpoint_url_drops_all_query_values() -> None:
    assert canonical_endpoint_url(
        "HTTPS://Example.Test:443/api/orders?customer=alice&token=secret#fragment"
    ) == "https://example.test/api/orders"


def test_schema_inference_bounds_untrusted_shape_complexity() -> None:
    body = infer_json_schema(
        {
            **{f"field-{index}": index for index in range(150)},
            "nested": {"a": {"b": {"c": {"d": {"e": {"f": "value"}}}}}},
        }
    )
    array = infer_json_schema(list(range(10_000)))

    assert len(body.properties) == 100
    assert array.type is ObservedValueType.ARRAY
    assert array.items is not None
    assert array.items.type is ObservedValueType.INTEGER


def test_inventory_merges_observed_request_shapes_without_values(tmp_path: Path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        first = ApiRequestSchemaRecord(
            endpoint_url="https://example.test/api/orders",
            method="POST",
            resource_type="fetch",
            query_schema=infer_query_schema(
                "https://example.test/api/orders?page=1"
            ),
            body_schema=infer_json_schema({"product_id": 7}),
            request_content_type="application/json",
        )
        second = first.model_copy(
            update={
                "query_schema": infer_query_schema(
                    "https://example.test/api/orders?sort=recent"
                ),
                "body_schema": infer_json_schema(
                    {"product_id": 8, "quantity": 2}
                ),
            }
        )
        store.save_api_request_schema("run-1", first)
        store.save_api_request_schema("run-1", second)
        records = store.list_api_request_schemas("run-1")
        raw = store.connection.execute(
            "SELECT query_schema, body_schema FROM api_request_schemas"
        ).fetchone()
    finally:
        store.close()

    assert len(records) == 1
    assert set(records[0].query_schema) == {"page", "sort"}
    assert records[0].body_schema is not None
    assert set(records[0].body_schema.properties) == {"product_id", "quantity"}
    persisted = f"{raw['query_schema']} {raw['body_schema']}"
    assert "recent" not in persisted


def test_browser_response_capture_never_persists_request_values(tmp_path: Path) -> None:
    config = EngagementConfig(
        name="schema-capture",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    logger = AuditLogger(config.audit_log_path)
    store = InventoryStore(config.inventory_db_path)
    explorer = WebExplorer(
        config,
        store,
        NetworkGate(
            ScopeGuard(config, logger), RequestBudget(10, 10, 60), logger
        ),
        logger,
        run_id="run-browser-schema",
    )
    explorer._current_page_url = "https://example.test/app?user=alice"

    class FakeRequest:
        resource_type = "fetch"
        url = "https://example.test/api/orders?customer=alice&limit=10"
        method = "POST"
        headers = {"content-type": "application/json; charset=utf-8"}
        post_data_json = {
            "email": "alice@example.test",
            "quantity": 2,
            "token": "must-not-persist",
        }

    class FakeResponse:
        request = FakeRequest()
        status = 201
        headers = {"content-type": "application/json"}

    try:
        explorer._record_response(FakeResponse())
        endpoint = store.connection.execute("SELECT * FROM endpoints").fetchone()
        schema = store.connection.execute(
            "SELECT * FROM api_request_schemas"
        ).fetchone()
        serialized = json.dumps(dict(schema), ensure_ascii=False)
    finally:
        store.close()

    assert endpoint["url"] == "https://example.test/api/orders"
    assert endpoint["source_page"].endswith("user=[REDACTED]")
    assert "alice" not in serialized
    assert "must-not-persist" not in serialized
    assert '"token"' in schema["body_schema"]
