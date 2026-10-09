"""Positive interface cases for the kernel-closed observer checks.

Owned by Capability (TASK-005). No docker: pure string fixtures.
"""

from __future__ import annotations

from redteam.capability.gate_observer import ClosedReport, check_interfaces_closed


def test_single_data_iface_with_matching_default_is_closed() -> None:
    addr = "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 10.0.0.5/24 scope global eth0"
    route = "default via 10.0.0.1 dev eth0"
    result = check_interfaces_closed(addr, route)
    assert isinstance(result, ClosedReport)
    assert result.closed is True
    assert "eth0" in result.detail


def test_loopback_only_with_no_default_is_closed() -> None:
    addr = "1: lo    inet 127.0.0.1/8 scope host lo"
    result = check_interfaces_closed(addr, "")
    assert isinstance(result, ClosedReport)
    assert result.closed is True
    assert "lo-only" in result.detail


def test_second_data_iface_fails_closed() -> None:
    first = "2: eth0    inet 10.0.0.5/24 scope global eth0"
    second = "3: eth1    inet 10.0.0.6/24 scope global eth1"
    addr = first + "\n" + second
    result = check_interfaces_closed(addr, "")
    assert isinstance(result, ClosedReport)
    assert result.closed is False
