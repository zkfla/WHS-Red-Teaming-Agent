"""Unavailable local fixtures must not open a window or pull an image."""

import pytest

from tests.integration import test_gated_install_docker as proof


@pytest.mark.parametrize("missing", [proof.ROOM_IMAGE, proof.SIDECAR_IMG, proof.MOCK_IMAGE])
async def test_missing_fixture_stops_before_resource_creation(tmp_path, monkeypatch, missing):
    checked = []

    async def available(image):
        checked.append(image)
        return image != missing

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not create resources or implicitly pull images")

    monkeypatch.setattr(proof, "image_available", available)
    monkeypatch.setattr(proof, "docker", forbidden)
    with pytest.raises(pytest.skip.Exception, match="not available locally"):
        await proof.test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(
            tmp_path, monkeypatch,
        )
    assert checked[-1] == missing
    assert list(tmp_path.iterdir()) == []
