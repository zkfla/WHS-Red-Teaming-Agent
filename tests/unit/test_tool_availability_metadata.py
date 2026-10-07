"""Owner-declared availability effect on ToolDescriptor via provider metadata."""

from __future__ import annotations

import pytest

from redteam.capability.adapters.mcp.streamable_http import _apply_provider_metadata
from redteam.core.contracts.tool import PROVIDER_METADATA_AVAILABILITY_IMPACT, ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


def _descriptor(**updates):
    values = dict(
        tool_id=ToolId("recon.provider.ping"),
        server_id="provider",
        tool_name="ping",
        description="harmless probe",
        input_schema={"type": "object"},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    values.update(updates)
    return ToolDescriptor(**values)


def test_availability_impact_defaults_to_false() -> None:
    assert _descriptor().availability_impact is False


def test_owner_true_roundtrips_through_provider_metadata() -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_AVAILABILITY_IMPACT: True})
    assert descriptor.availability_impact is True


def test_owner_false_roundtrips_through_provider_metadata() -> None:
    descriptor = _descriptor(availability_impact=True)
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_AVAILABILITY_IMPACT: False})
    assert descriptor.availability_impact is False


def test_absent_key_keeps_default() -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {})
    assert descriptor.availability_impact is False


def test_none_metadata_keeps_default() -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, None)
    assert descriptor.availability_impact is False


@pytest.mark.parametrize("bad", ["true", "True", "1", 1, 0, 1.0, 0.0, [], {}, (True,)])
def test_invalid_type_does_not_coerce(bad: object) -> None:
    descriptor = _descriptor()
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_AVAILABILITY_IMPACT: bad})
    assert descriptor.availability_impact is False


@pytest.mark.parametrize("bad", ["true", 1, 0, None, [], {}])
def test_invalid_type_does_not_clear_declared_true(bad: object) -> None:
    descriptor = _descriptor(availability_impact=True)
    _apply_provider_metadata(descriptor, {PROVIDER_METADATA_AVAILABILITY_IMPACT: bad})
    assert descriptor.availability_impact is True


def test_descriptor_model_roundtrip_preserves_owner_value() -> None:
    descriptor = _descriptor(availability_impact=True)
    restored = ToolDescriptor.model_validate(descriptor.model_dump())
    assert restored.availability_impact is True
