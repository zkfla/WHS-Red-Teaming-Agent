"""No gate effects or budget charges before a pinned local image is verified."""

from unittest.mock import AsyncMock

import pytest

from redteam.capability.gated_window import GatedNetworkError, GatedWindowExtension
from redteam.capability.shell_assembly import build_gated_provider_config
from tests.unit.test_gated_window import _policy, _stub_manager
from tests.unit.test_shell_runtime_008 import _gated_policy_file


@pytest.mark.parametrize("image", [None, "t005-gate-sidecar", "image;evil", "sha256:bad"])
def test_unselected_or_mutable_sidecar_is_config_error(tmp_path, image):
    with pytest.raises(ValueError, match="gated_sidecar_image_"):
        build_gated_provider_config(_gated_policy_file(tmp_path, sidecar_image=image))


@pytest.mark.parametrize("case", ["missing", "tag", "unavailable", "wrong_id", "timeout", "cut"])
async def test_image_failure_never_reserves_or_creates_a_workroom(case):
    policy = _policy()
    if case in {"missing", "tag"}:
        policy = policy.model_copy(update={
            "sidecar_image": None if case == "missing" else "mutable:tag",
        })
    ledger = AsyncMock()
    manager = _stub_manager()
    manager.ensure_workroom = AsyncMock(side_effect=AssertionError("must not create room"))
    pin = "sha256:" + "ab" * 32
    docker = AsyncMock(return_value=(
        1 if case == "unavailable" else 0,
        ("sha256:" + "cd" * 32 if case == "wrong_id" else pin).encode(), b"",
        case == "timeout", case == "cut",
    ))
    extension = GatedWindowExtension(manager=manager, ledger=ledger, run_docker=docker,
                                     policy=policy)
    with pytest.raises(GatedNetworkError, match="gated_sidecar_image_"):
        await extension.open_window(
            run_id="run", instance_id="instance", epoch=1,
            caps=policy.default_target_caps, holder_action_id="action",
            holder_invocation_id="invocation",
        )
    ledger.request_window.assert_not_awaited()
    ledger.settle_window.assert_not_awaited()
    manager.ensure_workroom.assert_not_awaited()
    if case in {"missing", "tag"}:
        docker.assert_not_awaited()
    else:
        assert docker.await_args.args[0][:2] == ("image", "inspect")
        assert docker.await_count == 1


async def test_sidecar_creation_uses_operator_pin_and_never_pulls():
    policy = _policy()
    docker = AsyncMock(return_value=(0, b"", b"", False, False))
    extension = GatedWindowExtension(manager=_stub_manager(), ledger=AsyncMock(),
                                     run_docker=docker, policy=policy)
    await extension._create_sidecar("test-owned", "test-room")
    argv = docker.await_args_list[0].args[0]
    assert argv[-1] == policy.sidecar_image and "--pull=never" in argv
    assert "t005-gate-sidecar" not in argv
