from pathlib import Path

from whs_redteam.inventory import InventoryStore
from whs_redteam.models import EndpointRecord, FormRecord, PageRecord


def test_inventory_persists_and_deduplicates_records(tmp_path: Path) -> None:
    database = tmp_path / "inventory.db"
    with InventoryStore(database) as store:
        store.save_page(
            "run-1",
            PageRecord(url="https://example.test/", final_url="https://example.test/", depth=0),
        )
        form = FormRecord(
            page_url="https://example.test/",
            action_url="https://example.test/search",
            method="GET",
            input_names=("q",),
            input_types=("search",),
            in_scope=True,
            policy_reason="request is within the approved scope",
        )
        store.save_form("run-1", form)
        store.save_form("run-1", form)
        endpoint = EndpointRecord(
            url="https://example.test/api/items",
            method="GET",
            resource_type="fetch",
            status_code=200,
        )
        store.save_endpoint("run-1", endpoint)
        store.save_endpoint("run-1", endpoint.model_copy(update={"status_code": 304}))

        assert store.counts("run-1") == {
            "pages": 1,
            "forms": 1,
            "endpoints": 1,
            "deferred_links": 0,
        }
        row = store.connection.execute("SELECT status_code FROM endpoints").fetchone()
        assert row["status_code"] == 304


def test_run_ids_keep_inventory_separate(tmp_path: Path) -> None:
    with InventoryStore(tmp_path / "inventory.db") as store:
        page = PageRecord(url="https://example.test/", final_url="https://example.test/", depth=0)
        store.save_page("run-a", page)
        store.save_page("run-b", page)
        assert store.counts("run-a")["pages"] == 1
        assert store.counts("run-b")["pages"] == 1


def test_latest_inventory_pages_exclude_failed_and_redacted_urls(tmp_path: Path) -> None:
    with InventoryStore(tmp_path / "inventory.db") as store:
        store.save_page(
            "older",
            PageRecord(
                url="https://example.test/old",
                final_url="https://example.test/old",
                depth=0,
            ),
        )
        store.save_page(
            "latest",
            PageRecord(
                url="https://example.test/good",
                final_url="https://example.test/good",
                depth=0,
            ),
        )
        store.save_page(
            "latest",
            PageRecord(
                url="https://example.test/private?token=[REDACTED]",
                final_url="https://example.test/private?token=[REDACTED]",
                depth=1,
            ),
        )
        store.save_page(
            "latest",
            PageRecord(
                url="https://example.test/failed",
                final_url="https://example.test/failed",
                depth=1,
                error="navigation failed",
            ),
        )
        assert store.latest_page_urls(10) == ["https://example.test/good"]


def test_page_urls_are_scoped_to_the_requested_exploration_run(
    tmp_path: Path,
) -> None:
    with InventoryStore(tmp_path / "inventory.db") as store:
        store.save_page(
            "run-a",
            PageRecord(
                url="https://example.test/a",
                final_url="https://example.test/a",
                depth=0,
            ),
        )
        store.save_page(
            "run-b",
            PageRecord(
                url="https://example.test/b",
                final_url="https://example.test/b",
                depth=0,
            ),
        )

        assert store.page_urls("run-a", 10) == ["https://example.test/a"]
        assert store.page_urls("run-b", 10) == ["https://example.test/b"]
