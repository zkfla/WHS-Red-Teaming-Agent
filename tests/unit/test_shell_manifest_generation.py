"""Offline generation preserves image provenance without external index requests."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("offline", [True, False])
def test_manifest_uses_installed_image_and_only_fetches_index_when_enabled(
    tmp_path, monkeypatch, offline,
):
    script = (Path(__file__).resolve().parents[2]
              / "deploy/docker/shell-workroom/generate-manifest.py")
    spec = importlib.util.spec_from_file_location("shell_manifest_generator", script)
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    output = tmp_path / "programs.json"
    image_id = "sha256:" + "a" * 64
    requested = ["dnsutils"]
    calls = []

    def inspect(ref):
        calls.append(("inspect", ref))
        return image_id

    def source(image):
        assert image == image_id
        return "http://fixture.invalid/kali"

    def probe(image, packages):
        assert image == image_id and packages == requested
        return [{"requested_package": "dnsutils", "package": "bind9-dnsutils",
                 "version": "fixture-version", "source_package": "bind9",
                 "executables": [{"command": "dig", "path": "/usr/bin/dig"}]}]

    def fetch(mirror):
        if offline:
            raise AssertionError("offline mode must not fetch any mirror index")
        calls.append(("index", mirror))
        return {("bind9-dnsutils", "fixture-version"): "main"}, "fixture-index"

    monkeypatch.setattr(generator, "read_packages", lambda: requested)
    monkeypatch.setattr(generator, "docker_inspect_id", inspect)
    monkeypatch.setattr(generator, "apt_source_from_image", source)
    monkeypatch.setattr(generator, "run_probe", probe)
    monkeypatch.setattr(generator, "component_index", fetch)
    argv = [str(script), "--image", "local:fixture", "--output", str(output)]
    if offline:
        argv.append("--offline")
    monkeypatch.setattr("sys.argv", argv)
    assert generator.main() == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    expected = [("inspect", "local:fixture")]
    if not offline:
        expected.append(("index", "http://fixture.invalid/kali"))
    assert calls == expected
    assert manifest["image_digest"] == image_id
    assert manifest["base_pinned"] is False
    assert manifest["origin_index"] == ("offline-unqueried" if offline else "fixture-index")
    assert manifest["origin_unresolved"] == (["bind9-dnsutils==fixture-version"] if offline else [])
    assert manifest["programs"][0]["origin"] == (None if offline else "kali-rolling/main")
    assert manifest["programs"][0]["executables"][0]["command"] == "dig"
