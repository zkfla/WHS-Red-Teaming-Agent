"""Real image and instance program sources through the application assembly."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from redteam.app.bootstrap import build_application
from redteam.capability.shell_assembly import build_shell_adapter, shell_server_output_limits
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import default_program_manifest_path, workroom_key_for
from redteam.core.config import load_app_config
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.local_tools import LocalToolContext


@pytest.mark.skipif(
    os.environ.get("SHELL_PROGRAM_INTEGRATION") != "1",
    reason="requires the generated manifest and its locally built image",
)
async def test_real_program_sources_reach_registered_application_helpers(tmp_path):
    manifest = json.loads(Path(default_program_manifest_path()).read_text(encoding="utf-8"))
    policy = ShellWorkroomPolicy(image=manifest["image_digest"])
    adapter = build_shell_adapter(policy)
    source = adapter.program_source
    assert await source.probe(), "the opt-in check requires a running local Docker daemon"
    _, deps = await build_application(
        load_app_config("examples/app.yaml"), llm=FakeLLMClient(),
        builtin_adapters=[adapter], builtin_concurrency={adapter.server_id: policy.max_jobs},
        builtin_output_limits=shell_server_output_limits(policy), shell_programs=source,
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )
    subject = SimpleNamespace(run_id="program-source-" + uuid4().hex, instance_id="first")
    context = LocalToolContext(deps=deps, runtime=subject)
    registry = deps.local_tools
    assert registry is not None
    registered_ids = {str(tool.tool_id) for tool in deps.tool_catalog.all_descriptors()}
    listed = await registry.call("program.list", {"query": "nmap"}, context)
    assert listed.ok and listed.payload["program_manifest_status"] == "published"
    assert listed.payload["workroom_status"] == "not_running"
    card = next(card for card in listed.payload["cards"] if card["tool_name"] == "nmap")
    detail = await registry.call("program.describe", {"tool_id": card["tool_id"]}, context)
    assert detail.ok and detail.payload["owner_scope"] == "image"
    assert detail.payload["version"] and detail.payload["provenance"]
    assert detail.payload["executable"].startswith("/")

    key = workroom_key_for(subject.run_id, subject.instance_id)
    try:
        installed = await source.exec_script(
            key, "bash",
            "printf '#!/bin/sh\\nprintf instance-only\\n' > .local/bin/source_probe; "
            "chmod +x .local/bin/source_probe; source_probe",
            20.0,
        )
        assert installed.exit_code == 0 and installed.stdout == "instance-only"
        live = await registry.call("program.list", {"query": "source_probe"}, context)
        assert live.ok and live.payload["workroom_status"] == "listed"
        assert live.payload["total"] == 1
        assert live.payload["cards"][0]["callable"] is False
        live_detail = await registry.call(
            "program.describe", {"tool_id": live.payload["cards"][0]["tool_id"]}, context,
        )
        assert live_detail.ok and live_detail.payload["owner_scope"] == "instance"
        other_context = LocalToolContext(
            deps=deps, runtime=SimpleNamespace(run_id=subject.run_id, instance_id="second"),
        )
        other = await registry.call("program.list", {"query": "source_probe"}, other_context)
        assert other.ok and other.payload["total"] == 0
        assert other.payload["workroom_status"] == "not_running"
        assert {str(tool.tool_id) for tool in deps.tool_catalog.all_descriptors()} == registered_ids
    finally:
        assert await source.remove_workroom(key)
