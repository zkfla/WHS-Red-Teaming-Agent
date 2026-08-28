import pytest

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import DnsPolicyViolation
from whs_redteam.policy.dns import resolve_and_pin_targets


def test_public_dns_is_pinned_to_chromium() -> None:
    config = EngagementConfig(name="test", targets=("https://example.test",))
    pins = resolve_and_pin_targets(config, resolver=lambda _: ("93.184.216.34",))
    assert pins.addresses == {"example.test": ("93.184.216.34",)}
    assert pins.chromium_args() == ["--host-resolver-rules=MAP example.test 93.184.216.34"]


def test_private_dns_requires_explicit_permission() -> None:
    config = EngagementConfig(name="test", targets=("https://internal.example.test",))
    with pytest.raises(DnsPolicyViolation, match="private"):
        resolve_and_pin_targets(config, resolver=lambda _: ("10.0.0.10",))


def test_private_dns_can_be_explicitly_allowed() -> None:
    config = EngagementConfig(
        name="test",
        targets=("https://internal.example.test",),
        allow_private_dns=True,
    )
    pins = resolve_and_pin_targets(config, resolver=lambda _: ("10.0.0.10",))
    assert pins.addresses["internal.example.test"] == ("10.0.0.10",)


def test_metadata_address_is_always_blocked() -> None:
    config = EngagementConfig(name="test", targets=("http://169.254.169.254",))
    with pytest.raises(DnsPolicyViolation, match="forbidden"):
        resolve_and_pin_targets(config)
