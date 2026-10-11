"""Real policy/adapter pip install through a pinned proxy, local mocks only."""

import asyncio
import json
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.capability.gated_manifest import SHELL_INSTALL_RUN, build_gated_descriptors
from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_strategy import GatedExecutionPolicy
from redteam.capability.gated_window import GATED_LABEL, GatedWindowExtension, opaque_window_tag
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    ShellWorkroomManager,
    container_name_for,
    workroom_key_for,
)
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
)
from redteam.core.contracts.target import DomainTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, AgentInstanceId, EngagementId, InvocationId, RunId, ToolId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from tests.integration.test_gated_window_docker import SIDECAR_IMG, docker, image_available

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable")
ROOM_IMAGE = "shell-workroom:t003-draft"
MOCK_IMAGE = "python:3.11-slim"


async def test_real_pip_install_uses_proxy_and_direct_mirror_is_blocked(tmp_path, monkeypatch):
    # Like the other gated Docker proofs, require already-local fixtures
    # before creating any resources. Missing fixtures are not a gate proof
    # failure and must not trigger Docker's implicit image download.
    for image in (ROOM_IMAGE, SIDECAR_IMG, MOCK_IMAGE):
        if not await image_available(image):
            pytest.skip("gated install fixture image not available locally: " + image)
    suffix = uuid.uuid4().hex[:10]
    run_id, instance = "pip" + suffix, "i1"
    key = workroom_key_for(run_id, instance)
    epoch = time.time_ns()
    # Predict the isolated mock bridge; this is not an operator permission.
    monkeypatch.setattr("redteam.capability.gated_strategy.time.time_ns", lambda: epoch)
    sidecar = opaque_window_tag(run_id, instance, epoch)
    network = sidecar.replace("gwin-", "gwn-", 1)
    mirror, proxy = "pip-mirror-" + suffix, "pip-proxy-" + suffix
    manager = ShellWorkroomManager(ShellWorkroomPolicy(
        image=ROOM_IMAGE, allow_unpinned_image=True,
        default_timeout_seconds=90, max_timeout_seconds=120,
    ))
    adapter = BuiltinShellAdapter(manager.policy, workroom=manager)
    wheel = tmp_path / "whs_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("whs_probe.py", "MARKER = 'installed-through-pinned-proxy'\n")
        archive.writestr("whs_probe-1.0.dist-info/METADATA",
                         "Metadata-Version: 2.1\nName: whs-probe\nVersion: 1.0\n")
        archive.writestr("whs_probe-1.0.dist-info/WHEEL",
                         "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                         "Tag: py3-none-any\n")
        archive.writestr("whs_probe-1.0.dist-info/RECORD", "")
    try:
        code, _, err = await docker("network", "create", "--internal", "--label", GATED_LABEL,
                                     network)
        assert code == 0, err.decode()
        code, _, err = await docker(
            "run", "-d", "--name", mirror, "--network", network,
            "--mount", f"type=bind,source={tmp_path},target=/mirror,readonly",
            MOCK_IMAGE, "python", "-m", "http.server", "8080",
            "--directory", "/mirror",
        )
        assert code == 0, err.decode()
        code, out, _ = await docker("inspect", "--format",
                                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                    mirror)
        assert code == 0
        mirror_ip = out.decode().strip()
        script = tmp_path / "proxy.py"
        script.write_text(
            "import sys,time\nsys.path.insert(0,'/code')\n"
            "from install_proxy_engine import PinningForwardProxy\n"
            f"p=PinningForwardProxy({{{mirror_ip!r}:({mirror_ip!r},)}},allow_ports=(8080,))\n"
            "p.start('0.0.0.0',8080)\n"
            "while True: time.sleep(1)\n", encoding="utf-8",
        )
        source_dir = Path(__file__).resolve().parents[2] / "src/redteam/capability"
        code, _, err = await docker(
            "run", "-d", "--name", proxy, "--network", network,
            "--mount", f"type=bind,source={source_dir},target=/code,readonly",
            "--mount", f"type=bind,source={tmp_path},target=/fixture,readonly",
            MOCK_IMAGE, "python", "/fixture/proxy.py",
        )
        assert code == 0, err.decode()
        code, out, _ = await docker("inspect", "--format",
                                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                                    proxy)
        assert code == 0
        proxy_ip = out.decode().strip()
        capacity = NetworkCapacity(max_flow_init_deliveries=50, max_output_bytes=1000000)
        budget = NetworkBudget(global_capacity=capacity, target_capacity=capacity,
                               install_capacity=capacity)
        caps = WindowCaps(max_flow_init_deliveries=30, max_output_bytes=500000,
                          duration_seconds=120, purpose=NetworkPurpose.INSTALL)
        config = GatedNetworkPolicy(
            budget=budget, default_install_caps=caps,
            default_target_caps=caps.model_copy(update={"purpose": NetworkPurpose.TARGET}),
            registry=PinnedRegistry(mirrors={"mirror": (mirror_ip,), "proxy": (proxy_ip,)}),
            install_proxy=InstallProxyConfig(endpoint=f"http://{proxy_ip}:8080",
                                             pinned_hosts=(proxy_ip,),
                                             mirror_upstream=f"http://{mirror_ip}:8080"),
        )
        ledger = SQLiteNetworkGrantLedger(str(tmp_path / "ledger.db"),
                                          engagement_id="pip-proof", budget=budget)
        await ledger.setup()
        extension = GatedWindowExtension(manager=manager, ledger=ledger,
                                         run_docker=manager._run_docker, policy=config)
        manager.set_execution_admission(extension)
        evidence = FilesystemEvidenceStore(tmp_path / "evidence")
        policy = GatedExecutionPolicy(extension, scope_validator=lambda *a, **k: (True, [], None),
                                      evidence_store=evidence)
        adapter.set_gated_policy(policy, build_gated_descriptors(manager.policy))
        await asyncio.sleep(1)
        recipe = (
            "python3 -m venv /workroom/venv && "
            f"/workroom/venv/bin/pip install --disable-pip-version-check --no-deps "
            f"--no-index --trusted-host {mirror_ip} --find-links http://{mirror_ip}:8080 "
            "whs-probe==1.0 && /workroom/venv/bin/python -c 'import whs_probe;"
            "print(whs_probe.MARKER)' && "
            f"if curl --noproxy '*' --max-time 2 -s http://{mirror_ip}:8080/ >/dev/null; "
            "then echo DIRECT_BYPASS; exit 7; else echo DIRECT_BLOCKED; fi"
        )
        invocation = ToolInvocation(
            run_id=RunId(run_id), action_id=ActionId("pip-action"),
            invocation_id=InvocationId("pip-inv-" + suffix),
            tool_id=ToolId("analysis." + SHELL_INSTALL_RUN),
            arguments={"workroom_key": key, "target_ref": "mirror", "language": "bash",
                       "script": recipe,
                       "timeout_seconds": 90},
            execution_context=ExecutionContext(engagement_id=EngagementId("pip-proof"),
                run_id=RunId(run_id), instance_id=AgentInstanceId(instance),
                approved_target=DomainTarget(domain="mirror")),
            created_at=datetime.now(timezone.utc),
        )
        result = await adapter.invoke_contextual(invocation)
        (tmp_path / "result.json").write_text(json.dumps(result.output), encoding="utf-8")
        assert result.success is True, result.error
        assert result.output["exit_code"] == 0, result.output
        assert "installed-through-pinned-proxy" in result.output["stdout"]
        assert "DIRECT_BLOCKED" in result.output["stdout"]
        assert "DIRECT_BYPASS" not in result.output["stdout"]
        assert result.metadata.get("gated_evidence_id")
    finally:
        await adapter.aclose()
        for name in (container_name_for(key), sidecar, proxy, mirror):
            await docker("rm", "-f", name)
        await docker("network", "rm", network)
