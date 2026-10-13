"""Generic optional LLM connection fingerprint on the component binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from redteam.app.bootstrap import build_application
from redteam.app.runtime_components import binding_for_resume, default_component_registry
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.component_binding import (
    ComponentMismatchError,
    RuntimeComponentBinding,
    builtin_component_binding,
)
from redteam.orchestration.metered_llm import RunMeteredLLM
from tests.fakes import DummyToolGateway
from tests.integration.test_central_development import engagement as _engagement


def test_absent_fingerprint_omitted_from_dump() -> None:
    binding = builtin_component_binding()
    assert binding.llm_connection_fingerprint is None
    assert "llm_connection_fingerprint" not in binding.model_dump()
    assert "llm_connection_fingerprint" not in binding.model_dump(mode="json")


def test_legacy_dict_without_field_loads_as_none() -> None:
    raw = builtin_component_binding().model_dump()
    assert "llm_connection_fingerprint" not in raw
    assert RuntimeComponentBinding.model_validate(raw).llm_connection_fingerprint is None


def test_none_vs_none_resumes() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding()
    assert binding_for_resume(stored, registry=registry) == stored
    assert binding_for_resume(None, registry=registry) == builtin_component_binding()


def test_matching_fingerprint_resumes() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding().model_copy(
        update={"llm_connection_fingerprint": "fp-a"},
    )
    resumed = binding_for_resume(
        stored, registry=registry, llm_connection_fingerprint="fp-a",
    )
    assert resumed == stored


def test_changed_fingerprint_refuses() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding().model_copy(
        update={"llm_connection_fingerprint": "fp-a"},
    )
    with pytest.raises(ComponentMismatchError):
        binding_for_resume(stored, registry=registry, llm_connection_fingerprint="fp-b")


def test_added_connection_refuses() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding()
    with pytest.raises(ComponentMismatchError):
        binding_for_resume(stored, registry=registry, llm_connection_fingerprint="fp-b")


def test_removed_connection_refuses() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding().model_copy(
        update={"llm_connection_fingerprint": "fp-a"},
    )
    with pytest.raises(ComponentMismatchError):
        binding_for_resume(stored, registry=registry)


def test_legacy_checkpoint_with_pinned_connection_refuses() -> None:
    with pytest.raises(ComponentMismatchError):
        binding_for_resume(None, llm_connection_fingerprint="fp-a")


def test_fingerprint_participates_in_frozen_equality() -> None:
    base = builtin_component_binding()
    pinned = base.model_copy(update={"llm_connection_fingerprint": "fp-a"})
    assert pinned != base
    assert pinned.model_copy(update={"llm_connection_fingerprint": "fp-a"}) == pinned


_PINNED_FP = "fp-assembly-go"


class _PinnedFakeClient(FakeLLMClient):
    connection_fingerprint = _PINNED_FP


def _bounded_policy() -> LLMRequestPolicy:
    return LLMRequestPolicy(mode="bounded", max_requests=10)


async def _assemble_application(tmp_path: Path, llm: Any) -> Any:
    engagement = _engagement()
    config = AppConfig(
        engagement=EngagementConfig.model_validate(
            engagement.model_dump(exclude={"engagement_id", "created_at"}),
        ),
        orchestration=OrchestrationConfig(llm_request_policy=_bounded_policy(),
                                          central_state_path=str(tmp_path / "state.db")),
    )
    gateway = DummyToolGateway(tools=[])
    return await build_application(
        config, llm=llm,
        tool_gateway=gateway, tool_catalog=gateway,
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )


async def test_build_application_retains_unwrapped_connection_fingerprint(tmp_path: Path) -> None:
    _, deps = await _assemble_application(tmp_path, _PinnedFakeClient())
    assert isinstance(deps.llm_client, RunMeteredLLM)
    assert deps.component_binding is not None
    assert deps.component_binding.llm_connection_fingerprint == _PINNED_FP


async def test_assembly_without_fingerprint_preserves_legacy_binding(tmp_path: Path) -> None:
    _, deps = await _assemble_application(tmp_path, FakeLLMClient())
    assert deps.component_binding.llm_connection_fingerprint is None
    assert "llm_connection_fingerprint" not in deps.component_binding.model_dump()
