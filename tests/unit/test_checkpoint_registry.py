"""Per-run codec registries stay isolated; legacy default path is separate."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from redteam.orchestration.checkpointing import (
    STATE_CODEC_KIND,
    StateCodecRegistry,
    build_checkpointer,
    consume_state_codec_contributions,
    ensure_state_types_allowed,
    explicit_allowlist,
    legacy_allowlist,
    open_checkpointer,
    register_state_codec,
    register_state_type,
    registered_state_types,
    state_type_allowlist,
)


class ExternalWidget(BaseModel):
    label: str = "widget"


class ExternalFlag(str, Enum):
    ALPHA = "alpha"


def _contribution(
    kind=STATE_CODEC_KIND,
    contribution_id="tests.unit.test_checkpoint_registry:ExternalWidget",
    version="7",
):
    return SimpleNamespace(kind=kind, contribution_id=contribution_id, version=version)


def test_selection_does_not_leak_into_another_run_or_legacy() -> None:
    selected = StateCodecRegistry()
    other = StateCodecRegistry()
    assert consume_state_codec_contributions([_contribution()], registry=selected) == 1
    assert (ExternalWidget.__module__, "ExternalWidget") in explicit_allowlist(selected)
    assert (ExternalWidget.__module__, "ExternalWidget") not in explicit_allowlist(other)
    assert (ExternalWidget.__module__, "ExternalWidget") not in legacy_allowlist()
    assert (ExternalWidget.__module__, "ExternalWidget") not in state_type_allowlist()


def test_consume_without_destination_is_loud_not_silent_global() -> None:
    with pytest.raises(ValueError, match="explicit registry"):
        consume_state_codec_contributions([_contribution()])
    with pytest.raises(ValueError, match="explicit registry"):
        register_state_codec(_contribution())
    with pytest.raises(ValueError, match="not both"):
        register_state_codec(_contribution(), registry=StateCodecRegistry(), legacy=True)
    assert (ExternalWidget.__module__, "ExternalWidget") not in legacy_allowlist()


def test_wrong_kind_and_malformed_id_fail_loudly() -> None:
    registry = StateCodecRegistry()
    with pytest.raises(TypeError, match="state_codec"):
        register_state_codec(_contribution(kind="tool"), registry=registry)
    with pytest.raises(ValueError, match="contribution_id"):
        register_state_codec(_contribution(contribution_id="no-separator"), registry=registry)
    assert registry.entries() == ()


def test_legacy_path_stays_separate_from_selection() -> None:
    before = set(legacy_allowlist())
    register_state_type(ExternalWidget.__module__, "ExternalWidget", version="7")
    try:
        assert (ExternalWidget.__module__, "ExternalWidget") in legacy_allowlist()
        assert (ExternalWidget.__module__, "ExternalWidget") not in explicit_allowlist(
            StateCodecRegistry()
        )
        assert (ExternalWidget.__module__, "ExternalWidget") not in state_type_allowlist()
    finally:
        from redteam.orchestration import checkpointing

        checkpointing._EXTERNAL_STATE_TYPES.pop((ExternalWidget.__module__, "ExternalWidget"))
    assert set(legacy_allowlist()) == before


def test_ensure_uses_the_same_run_allowlist() -> None:
    widget = ExternalWidget()
    with pytest.raises(TypeError, match="unregistered checkpoint state type"):
        ensure_state_types_allowed({"widget": widget})
    selected = StateCodecRegistry()
    selected.register(ExternalWidget.__module__, "ExternalWidget", version="7")
    ensure_state_types_allowed({"widget": widget}, registry=selected)


def test_serializer_restores_a_selected_external_type() -> None:
    selected = StateCodecRegistry()
    selected.register(ExternalWidget.__module__, "ExternalWidget", version="7")
    checkpointer = build_checkpointer(enabled=True, registry=selected)
    assert checkpointer is not None
    restored = checkpointer.serde.loads_typed(
        checkpointer.serde.dumps_typed({"widget": ExternalWidget(label="kept")})
    )
    assert isinstance(restored["widget"], ExternalWidget)
    assert restored["widget"].label == "kept"


async def test_selected_outer_model_survives_sqlite_and_other_registry_refuses(
    tmp_path,
) -> None:
    selected = StateCodecRegistry()
    selected.register(ExternalWidget.__module__, "ExternalWidget", version="7")
    other = StateCodecRegistry()
    path = tmp_path / "selected.sqlite3"
    config = {"configurable": {"thread_id": "thread-selected", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "ts": "2026-01-01T00:00:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "channel_values": {"widget": ExternalWidget(label="kept")},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    async with open_checkpointer(backend="sqlite", path=str(path), registry=selected) as saver:
        assert saver is not None
        await saver.aput(config, checkpoint, {}, {})
        restored = await saver.aget(config)
    assert restored is not None
    assert isinstance(restored["channel_values"]["widget"], ExternalWidget)
    assert restored["channel_values"]["widget"].label == "kept"
    async with open_checkpointer(backend="sqlite", path=str(path), registry=other) as other_saver:
        assert other_saver is not None
        with pytest.raises(TypeError, match="unregistered checkpoint state type"):
            await other_saver.aget(config)


def test_strenum_value_is_rejected_without_loosening_strict_checks() -> None:
    selected = StateCodecRegistry()
    selected.register(ExternalWidget.__module__, "ExternalWidget", version="7")
    checkpointer = build_checkpointer(enabled=True, registry=selected)
    assert checkpointer is not None
    with pytest.raises(TypeError, match="unregistered checkpoint state type"):
        checkpointer.serde.dumps_typed({"flag": ExternalFlag.ALPHA})
    with pytest.raises(TypeError, match="unregistered checkpoint state type"):
        ensure_state_types_allowed({"flag": ExternalFlag.ALPHA}, registry=selected)


def test_registered_versions_are_recorded() -> None:
    registry = StateCodecRegistry([("some.mod", "SomeType", "3")])
    assert registry.versions() == {("some.mod", "SomeType"): "3"}
    assert registered_state_types().get((ExternalWidget.__module__, "ExternalWidget")) is None
