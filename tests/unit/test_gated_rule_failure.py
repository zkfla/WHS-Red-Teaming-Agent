"""A kernel ruleset failure must not connect the workroom or refund its grant."""

from unittest.mock import AsyncMock

import pytest

from redteam.capability.gated_window import GatedNetworkError, GatedWindowExtension
from redteam.capability.shell_workroom import workroom_key_for
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import GrantRefused, NetworkPurpose, WindowState
from tests.unit.test_gated_window import _policy, _stub_manager


@pytest.mark.parametrize("purpose", [NetworkPurpose.INSTALL, NetworkPurpose.TARGET])
async def test_ruleset_apply_failure_stays_closed_and_quarantined(tmp_path, monkeypatch, purpose):
    policy = _policy()
    policy = policy.model_copy(update={"budget": policy.budget.model_copy(
        update={"install_capacity": policy.budget.global_capacity},
    )})
    caps = (policy.default_install_caps if purpose is NetworkPurpose.INSTALL
            else policy.default_target_caps)
    ledger = SQLiteNetworkGrantLedger(
        str(tmp_path / "network.db"), engagement_id="gate-failure", budget=policy.budget,
    )
    await ledger.setup()
    manager = _stub_manager()
    monkeypatch.setattr(manager, "ensure_workroom", AsyncMock(return_value="fixture-room"))
    calls = []

    async def docker(argv, stdin, timeout, limit=None):
        calls.append(argv)
        if argv[:2] == ("image", "inspect"):
            return 0, policy.sidecar_image.encode(), b"", False, False
        if argv[:2] == ("exec", "-i"):
            assert stdin is not None and b"quota name qb drop" in stdin
            return 1, b"", b"quota qb: Could not process rule: No such file", False, False
        if argv[0] == "inspect":
            # No surviving sidecar, so owned test cleanup may remove its name.
            return 1, b"", b"No such object", False, False
        return 0, b"", b"", False, False

    ext = GatedWindowExtension(manager=manager, ledger=ledger, run_docker=docker, policy=policy)
    transition = AsyncMock(side_effect=AssertionError("must not connect any network"))
    rpc = AsyncMock(side_effect=AssertionError("must not launch the packet admission daemon"))
    monkeypatch.setattr(ext, "_transition_network", transition)
    monkeypatch.setattr(ext, "_rpc_loop", rpc)
    settle = AsyncMock(wraps=ledger.settle_window)
    monkeypatch.setattr(ledger, "settle_window", settle)

    with pytest.raises(GatedNetworkError, match="ruleset_apply_failed"):
        await ext.open_window(
            run_id="run", instance_id="instance", epoch=1,
            caps=caps, holder_action_id="action",
            holder_invocation_id="invocation",
        )
    transition.assert_not_awaited()
    rpc.assert_not_awaited()
    assert any(argv[:2] == ("rm", "-f") for argv in calls)
    key = workroom_key_for("run", "instance")
    assert not ext.is_transmit_open(key)
    assert ext.expected_networks(key) == frozenset({"none"})
    settle.assert_awaited_once()
    kwargs = settle.await_args.kwargs
    assert kwargs["meter"] is None and kwargs["verified_closed"] is False
    stored = await ledger.settle_window(**kwargs)
    assert stored.state is WindowState.QUARANTINED
    assert stored.charged_bytes == caps.max_output_bytes
    assert stored.released_bytes == stored.released_deliveries == 0
    retry = await ledger.request_window(
        run_id="run", instance_id="instance", epoch=2, caps=caps,
    )
    assert isinstance(retry, GrantRefused) and retry.code == "quarantined"
