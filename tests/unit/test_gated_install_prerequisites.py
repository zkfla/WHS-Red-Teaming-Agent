"""Unavailable local fixtures must not open a window or pull an image.

plan7a replaced the old fixed `shell-workroom:t003-draft`/`t005-gate-sidecar`
tags with real, content-hashed auto-resolution (`_workroom_image`/
`sidecar_image`/`quota_supported`); each of their failure paths is checked
here with the resolver patched out, never a live build or `docker` call.
"""

from __future__ import annotations

import pytest

from tests.integration import test_gated_install_docker as proof


async def _forbidden(*args, **kwargs):
    raise AssertionError("must not create resources or implicitly pull images")


async def test_missing_workroom_image_stops_before_resource_creation(tmp_path, monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(proof, "_workroom_image", _none)
    monkeypatch.setattr(proof, "docker", _forbidden)
    with pytest.raises(pytest.skip.Exception, match="workroom image could not be built"):
        await proof.test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(
            tmp_path, monkeypatch,
        )
    assert list(tmp_path.iterdir()) == []


async def test_missing_sidecar_image_stops_before_resource_creation(tmp_path, monkeypatch):
    async def _workroom():
        return "whs-shell-workroom:deadbeef"

    async def _no_sidecar():
        return None

    monkeypatch.setattr(proof, "_workroom_image", _workroom)
    monkeypatch.setattr(proof, "sidecar_image", _no_sidecar)
    monkeypatch.setattr(proof, "docker", _forbidden)
    with pytest.raises(pytest.skip.Exception, match="sidecar image could not be built"):
        await proof.test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(
            tmp_path, monkeypatch,
        )
    assert list(tmp_path.iterdir()) == []


async def test_unsupported_kernel_quota_stops_before_resource_creation(tmp_path, monkeypatch):
    async def _workroom():
        return "whs-shell-workroom:deadbeef"

    async def _sidecar():
        return "whs-gate-sidecar:deadbeef"

    async def _unsupported(_image):
        return False

    monkeypatch.setattr(proof, "_workroom_image", _workroom)
    monkeypatch.setattr(proof, "sidecar_image", _sidecar)
    monkeypatch.setattr(proof, "quota_supported", _unsupported)
    monkeypatch.setattr(proof, "docker", _forbidden)
    with pytest.raises(pytest.skip.Exception, match="does not support nft named quota"):
        await proof.test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(
            tmp_path, monkeypatch,
        )
    assert list(tmp_path.iterdir()) == []


async def test_missing_mock_image_stops_before_resource_creation(tmp_path, monkeypatch):
    async def _workroom():
        return "whs-shell-workroom:deadbeef"

    async def _sidecar():
        return "whs-gate-sidecar:deadbeef"

    async def _supported(_image):
        return True

    checked = []

    async def available(image):
        checked.append(image)
        return False

    monkeypatch.setattr(proof, "_workroom_image", _workroom)
    monkeypatch.setattr(proof, "sidecar_image", _sidecar)
    monkeypatch.setattr(proof, "quota_supported", _supported)
    monkeypatch.setattr(proof, "image_available", available)
    monkeypatch.setattr(proof, "docker", _forbidden)
    with pytest.raises(pytest.skip.Exception, match="not available locally"):
        await proof.test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(
            tmp_path, monkeypatch,
        )
    assert checked == [proof.MOCK_IMAGE]
    assert list(tmp_path.iterdir()) == []
