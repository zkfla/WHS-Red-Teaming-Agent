import os
import subprocess
import sys
from pathlib import Path

from whs_redteam.audit import AuditLogger
from whs_redteam.config import load_config
from whs_redteam.inventory import InventoryStore
from whs_redteam.orchestration.orchestrator import AgentOrchestrator
from whs_redteam.run_service import RunService


def test_injection_module_imports_in_a_clean_process() -> None:
    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import whs_redteam.injection"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr


def test_sqli_url_service_creates_value_free_resumable_profile(
    tmp_path: Path,
) -> None:
    service = RunService.from_sqli_url(
        "https://example.test/products?id=94821&search=private-value&token=super-private-token-value",
        output_dir=tmp_path,
        approval_secret_env="TEST_APPROVAL_SECRET",
    )

    assert service.config_path is not None
    assert service.config_path.exists()
    assert service.engagement.targets == ("https://example.test/products",)
    fixture_targets = {
        (fixture.target_url, fixture.parameter)
        for fixture in service.engagement.injection_fixtures
    }
    assert fixture_targets == {
        ("https://example.test/products", "id"),
        ("https://example.test/products", "search"),
    }
    assert all(
        fixture.checks == frozenset({"sqli"})
        for fixture in service.engagement.injection_fixtures
    )

    serialized = service.config_path.read_text(encoding="utf-8")
    assert "94821" not in serialized
    assert "private-value" not in serialized
    assert "super-private-token-value" not in serialized

    loaded = load_config(service.config_path).engagement
    assert loaded.name == service.engagement.name
    assert loaded.targets == service.engagement.targets
    assert loaded.injection_fixtures == service.engagement.injection_fixtures
    assert loaded.enabled_capabilities == service.engagement.enabled_capabilities
    assert (
        loaded.approval_required_capabilities
        == service.engagement.approval_required_capabilities
    )


def test_generated_profile_keeps_same_config_hash_after_yaml_reload(
    tmp_path: Path,
) -> None:
    service = RunService.from_sqli_url(
        "https://example.test/products?id=7",
        output_dir=tmp_path / "profiles",
        planner_mode="hybrid",
        ai_model="compatibility-model",
    )
    reloaded = RunService.from_config_path(service.config_path)
    with InventoryStore(service.engagement.inventory_db_path) as inventory:
        direct_hash = AgentOrchestrator(
            service.engagement,
            inventory,
            AuditLogger(service.engagement.audit_log_path),
        ).config_hash
    with InventoryStore(reloaded.engagement.inventory_db_path) as inventory:
        reloaded_hash = AgentOrchestrator(
            reloaded.engagement,
            inventory,
            AuditLogger(reloaded.engagement.audit_log_path),
        ).config_hash
    assert reloaded_hash == direct_hash
