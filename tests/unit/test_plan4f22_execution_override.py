"""Execution cannot silently discard a limit override or mutate assembled limits."""

from types import SimpleNamespace

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.core.config import OrchestrationConfig


async def test_override_rejected_before_run_entry(monkeypatch):
    entered = []

    async def execute(*args, **kwargs):
        entered.append(kwargs)

    monkeypatch.setattr("redteam.app.central_run.run_central_engagement", execute)
    with pytest.raises(ValueError, match="run_engagement_orchestration_override_removed"):
        await run_engagement(
            object(), deps=object(), orchestration_config=OrchestrationConfig(max_steps=1),
        )
    assert entered == []


async def test_normal_entry_retains_assembled_dependencies(monkeypatch):
    settings = OrchestrationConfig(max_steps=2, max_workers=1)
    deps = SimpleNamespace(settings=settings)
    engagement = object()
    result = object()

    async def execute(scope, **kwargs):
        assert scope is engagement
        assert kwargs["deps"] is deps
        assert kwargs["deps"].settings is settings
        assert kwargs["objective"] == "inspect"
        assert kwargs["run_id"] == "original-run"
        return result

    monkeypatch.setattr("redteam.app.central_run.run_central_engagement", execute)
    assert await run_engagement(
        engagement, deps=deps, objective="inspect", run_id="original-run",
    ) is result
