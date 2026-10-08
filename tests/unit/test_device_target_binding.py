"""Known device identities cannot be redirected by explicit/aliased arguments."""

import pytest

from redteam.core.contracts.target import DeviceTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.engine import validate_argument_target
from redteam.orchestration.target_binding import validate_network_arguments


def target():
    return DeviceTarget(
        device_ref="approved-ref",
        device_serial="approved-serial",
        package_name="org.approved.app",
        asset_id="approved-app",
    )


@pytest.mark.parametrize(
    "field",
    [
        "device",
        "device_ref",
        "device_serial",
        "packageName",
        "package_name",
    ],
)
def test_explicit_device_identity_mismatch_is_rejected(field):
    with pytest.raises(ValueError, match="device identity"):
        validate_network_arguments(target(), {field: "other-installation"})


@pytest.mark.parametrize("shared", ["device_ref", "device_serial", "package_name"])
def test_provider_alias_does_not_bypass_device_identity(shared):
    validate_network_arguments(
        target(),
        {"remote_identity": getattr(target(), shared)},
        argument_bindings={"remote_identity": shared},
    )
    with pytest.raises(ValueError, match="device identity"):
        validate_network_arguments(
            target(),
            {"remote_identity": "other-installation"},
            argument_bindings={"remote_identity": shared},
        )


def test_missing_argument_is_left_for_schema_validation_and_known_values_pass():
    validate_network_arguments(target(), {"command": "getprop"})
    validate_argument_target(
        target(),
        {
            "device_ref": "approved-ref",
            "device": "approved-ref",
            "device_serial": "approved-serial",
            "packageName": "org.approved.app",
        },
    )


def test_provider_can_declare_a_serial_alias_without_default_ref_overriding_it():
    tool = ToolDescriptor(
        tool_id="mobile.vendor.read",
        server_id="vendor",
        tool_name="read",
        input_schema={"properties": {"device": {"type": "string"}}},
        argument_bindings={"device": "device_serial"},
    )
    bound = CandidateCompiler._bind_target_arguments({}, target(), tool, "response_analysis")
    assert bound == {"device": "approved-serial"}
    validate_network_arguments(target(), bound, argument_bindings=tool.argument_bindings)
