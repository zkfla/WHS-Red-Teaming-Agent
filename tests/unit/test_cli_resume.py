"""``redteam resume <run_id> <engagement.yaml>`` -- CLI wiring.

Parses/registers the command, reads the run's original engagement id from its
durable checkpoint, reaches the canonical ``build_application`` (never a
CLI-only runtime), and fails closed whenever the run cannot be recovered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from redteam.app.cli import _build_parser
from redteam.app.cli.commands import resume as resume_command
from redteam.core.ids import EngagementId
from redteam.orchestration.checkpointing import (
    StateCodecRegistry,
    build_checkpointer,
    open_checkpointer,
    open_identity_reader,
    read_checkpoint_identity,
)
from redteam.orchestration.component_binding import (
    BUILTIN_COMPONENT_IDS,
    RuntimeComponentBinding,
)


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.setenv("REDTEAM_RUNTIME_BACKEND", "legacy")

_CONSTRUCTED: list[str] = []


class _IdentityProbe(BaseModel):
    label: str = "probe"

    def __init__(self, **data: Any) -> None:
        _CONSTRUCTED.append("probe")
        super().__init__(**data)






@pytest.fixture(autouse=True)
def _explicit_fake_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real config loader needs an explicit provider."""
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")








def test_resume_is_registered_as_a_cli_command() -> None:
    parser = _build_parser()
    args = parser.parse_args(["resume", "run_1", "engagement.yaml"])

    assert args.handler is resume_command.run
    assert args.run_id == "run_1"
    assert args.engagement_path == "engagement.yaml"
























async def test_identity_reads_msgpack_without_running_constructors_and_strict_holds(
    tmp_path: Path,
) -> None:
    _CONSTRUCTED.clear()
    selected = StateCodecRegistry()
    selected.register(_IdentityProbe.__module__, "_IdentityProbe", version="7")
    binding = RuntimeComponentBinding(
        profile_id="builtin",
        version="1",
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )
    run_id = "run-identity-msgpack"
    path = tmp_path / "identity.sqlite3"
    marker = _IdentityProbe(label="kept")
    _CONSTRUCTED.clear()
    config = {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "ts": "2026-01-01T00:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000002",
        "channel_values": {
            "runtime": {
                "engagement_id": "eng-identity",
                "mission_id": "mission-1",
                "component_binding": binding.model_dump(mode="json"),
                "extra": marker,
            }
        },
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    async with open_checkpointer(backend="sqlite", path=str(path), registry=selected) as saver:
        assert saver is not None
        await saver.aput(config, checkpoint, {}, {})
    _CONSTRUCTED.clear()
    found = await read_checkpoint_identity(
        enabled=True, backend="sqlite", path=str(path), run_id=run_id
    )
    assert found is not None
    assert found.engagement_id == EngagementId("eng-identity")
    assert found.mission_id == "mission-1"
    assert found.component_binding is not None
    assert found.component_binding.profile_id == "builtin"
    assert _CONSTRUCTED == []
    async with open_checkpointer(
        backend="sqlite", path=str(path), registry=selected
    ) as strict_saver:
        assert strict_saver is not None
        restored = await strict_saver.aget(config)
    assert restored is not None
    extra = restored["channel_values"]["runtime"]["extra"]
    assert isinstance(extra, _IdentityProbe)
    assert extra.label == "kept"


async def test_identity_handles_legacy_json_envelope_kwargs_without_construction() -> None:
    _CONSTRUCTED.clear()
    selected = StateCodecRegistry()
    selected.register(_IdentityProbe.__module__, "_IdentityProbe", version="7")
    binding = RuntimeComponentBinding(
        profile_id="builtin",
        version="1",
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )
    envelope_id = _IdentityProbe.__module__.split(".") + ["_IdentityProbe"]
    payload = {
        "runtime": {
            "engagement_id": "eng-legacy",
            "mission_id": "mission-legacy",
            "component_binding": binding.model_dump(mode="json"),
            "evil": {
                "lc": 2,
                "type": "constructor",
                "id": envelope_id,
                "kwargs": {"label": "kept"},
            },
        }
    }
    raw = ("json", json.dumps(payload).encode("utf-8"))
    async with open_identity_reader(backend="memory") as reader:
        assert reader is not None
        projected = reader.serde.loads_typed(raw)
    assert projected["runtime"]["engagement_id"] == "eng-legacy"
    assert projected["runtime"]["evil"] == {"label": "kept"}
    assert _CONSTRUCTED == []
    strict = build_checkpointer(enabled=True, registry=selected)
    assert strict is not None
    revived = strict.serde.loads_typed(raw)
    evil = revived["runtime"]["evil"]
    assert isinstance(evil, _IdentityProbe)
    assert evil.label == "kept"
