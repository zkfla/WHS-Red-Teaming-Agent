import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from whs_redteam.web.app import _report_page_html, create_app  # noqa: E402


def test_preview_health_and_index() -> None:
    client = TestClient(create_app())
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert client.get("/api/ai/health").json()["configured"] is False
    index = client.get("/")
    assert index.status_code == 200
    assert "WHS Red-Teaming Preview" in index.text


def test_preview_requires_known_run_for_status() -> None:
    client = TestClient(create_app())
    response = client.get("/api/runs/does-not-exist")
    assert response.status_code == 404


def test_report_page_escapes_untrusted_text() -> None:
    rendered = _report_page_html(
        {
            "run_id": "run-1",
            "planning": {"mode": "hybrid", "provider": "openai_compatible"},
            "exploitability_decision": {"verdict": "CONFIRMED"},
            "findings": [{"category": "<script>alert(1)</script>"}],
        }
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered
