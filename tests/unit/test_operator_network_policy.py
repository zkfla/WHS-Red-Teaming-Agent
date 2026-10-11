"""Reject unusable operator routes on load without DNS, Docker or packets."""

import pytest

from redteam.capability.gated_network_policy import GatedNetworkPolicy, InstallProxyConfig
from redteam.capability.shell_policy import load_shell_workroom_policy
from tests.unit.test_install_proxy_route import _install_policy
from tests.unit.test_shell_runtime_008 import _policy_file


@pytest.mark.parametrize("field", ["endpoint", "mirror_upstream"])
@pytest.mark.parametrize("value", [
    "http://", "http://mirror:0", "http://mirror:70000", "http://mirror:bad",
    "https://user:S3CR3T@mirror", "https://mirror/packages", "https://mirror/?key=S3CR3T",
    "https://mirror/#S3CR3T", "https://mirror\n", "ftp://mirror", "https://mirror?",
])
def test_proxy_addresses_are_credential_free_http_origins(field, value):
    data = dict(endpoint="http://proxy:3128", mirror_upstream="https://mirror:8443",
                pinned_hosts=("192.0.2.2",))
    data[field] = value
    with pytest.raises(ValueError):
        InstallProxyConfig(**data)


@pytest.mark.parametrize("pin", ["proxy.local", "", "10.0.0.01", "0:0:0:0:0:0:0:1"])
def test_proxy_pins_are_canonical_literals(pin):
    with pytest.raises(ValueError):
        InstallProxyConfig(endpoint="http://proxy:3128", mirror_upstream="https://mirror",
                           pinned_hosts=(pin,))


def test_proxy_and_target_pin_overlap_rejected_on_policy_load():
    data = _install_policy().model_dump(mode="json")
    data["install_proxy"]["pinned_hosts"] = data["registry"]["targets"]["t"]
    with pytest.raises(ValueError):
        GatedNetworkPolicy.model_validate(data)


def test_disabled_install_capacity_and_distinct_proxy_pin_are_valid():
    data = _install_policy().model_dump(mode="json")
    data["budget"]["install_capacity"] = dict(max_flow_init_deliveries=0, max_output_bytes=0)
    policy = GatedNetworkPolicy.model_validate(data)
    assert policy.budget.install_capacity.max_output_bytes == 0


def test_shell_default_timeout_cannot_exceed_its_ceiling(tmp_path):
    with pytest.raises(ValueError):
        load_shell_workroom_policy(_policy_file(
            tmp_path, default_timeout_seconds=20, max_timeout_seconds=10,
        ))
