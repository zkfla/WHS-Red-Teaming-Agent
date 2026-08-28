from pathlib import Path

from whs_redteam.config import EngagementConfig
from whs_redteam.inventory import InventoryStore
from whs_redteam.openapi import import_openapi_schemas


def test_openapi_imports_query_shapes_under_configured_targets(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "openapi.yaml"
    spec.write_text(
        """
openapi: 3.0.3
info: {title: test, version: '1'}
paths:
  /search:
    get:
      parameters:
        - {name: q, in: query, schema: {type: string}}
        - {name: token, in: query, schema: {type: string}}
  /items:
    parameters:
      - {name: id, in: query, schema: {type: integer}}
    get: {}
    post: {}
  /items/{item_id}:
    get: {}
""",
        encoding="utf-8",
    )
    config = EngagementConfig(
        name="openapi-test",
        targets=("https://example.test",),
        openapi_spec_path=spec,
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
    )
    store = InventoryStore(config.inventory_db_path)
    try:
        imported = import_openapi_schemas(config, store, "run-1")
        schemas = store.list_api_request_schemas("run-1")
        endpoints = store.list_endpoints("run-1")
        persisted = config.inventory_db_path.read_bytes()
    finally:
        store.close()

    assert imported == 3
    assert len(schemas) == 3
    assert {schema.method for schema in schemas} == {"GET", "POST"}
    search = next(schema for schema in schemas if schema.endpoint_url.endswith("/search"))
    assert set(search.query_schema) == {"q", "token"}
    assert any(endpoint.url.endswith("/items") for endpoint in endpoints)
    assert b"secret" not in persisted
