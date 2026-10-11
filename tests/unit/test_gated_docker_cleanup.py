"""Gate Docker proofs clean test-owned resources when rule admission fails."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.capability.gated_window import GatedNetworkError
from tests.integration import test_gated_window_docker as proof


async def test_failed_window_proof_removes_owned_containers_before_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    async def docker(*argv, **kwargs):
        calls.append(argv)
        return 0, b"", b""

    monkeypatch.setattr(proof, "image_available", AsyncMock(return_value=True))
    monkeypatch.setattr(proof, "docker", docker)
    monkeypatch.setattr(proof, "_mock", AsyncMock(return_value=("fixture-mock", "10.0.0.9")))
    monkeypatch.setattr(proof.ShellWorkroomManager, "ensure_workroom",
                        AsyncMock(return_value="fixture-room"))
    monkeypatch.setattr(proof.ShellWorkroomManager, "exec_script",
                        AsyncMock(return_value=SimpleNamespace(exit_code=0)))
    monkeypatch.setattr(proof.GatedWindowExtension, "open_window",
                        AsyncMock(side_effect=GatedNetworkError("ruleset_apply_failed")))

    with pytest.raises(GatedNetworkError, match="ruleset_apply_failed"):
        await proof.test_window_e2e_busy_static_settle(tmp_path)
    removed = [argv[2] for argv in calls if argv[:2] == ("rm", "-f")]
    assert len([name for name in removed if name.startswith("whs-shell-")]) == 2
    assert len([name for name in removed if name.startswith("gwin-")]) == 2
    assert "fixture-mock" in removed
    created = next(argv[-1] for argv in calls if argv[:2] == ("network", "create"))
    assert ("network", "rm", created) in calls
    assert calls.index(("rm", "-f", "fixture-mock")) < calls.index(("network", "rm", created))
