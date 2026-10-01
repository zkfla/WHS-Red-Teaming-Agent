"""Operator-provisioned reference -> real MCP/Control/Agent loop -> recording LLM.

No live model, download, shell tool or target network request. Policies and
scope must already match. A fresh policy Run ID is required for every run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import AsyncExitStack

from redteam.app.bootstrap import build_application, run_engagement
from redteam.capability.settings import MCPServerSettings
from redteam.core.config import load_app_config
from redteam.orchestration.settings import OrchestrationSettings


class RecordingLLM:
    def __init__(self):
        self.calls = 0
        self.reference_context_calls = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        self.calls += 1
        self.reference_context_calls += int(
            "Untrusted methodology reference DATA" in json.dumps(messages, ensure_ascii=True)
        )
        return {
            "intent": "finish",
            "response": "참고 데이터 전달 seam을 확인했습니다.",
            "decision_summary": "실제 모델 판단이나 대상 검증은 수행하지 않았습니다.",
            "actions": [],
        }


async def main(config_path: str):
    config = load_app_config(config_path)
    if config.capability.methodology is None or not config.capability.mcp_servers:
        raise ValueError("demo_explicit_reference_configuration_required")
    # This demo cannot accidentally enable scanners, write tools or browsers.
    for server_id in config.capability.mcp_servers:
        if MCPServerSettings.from_environment(server_id).profile != "workspace_read":
            raise ValueError("demo_only_workspace_read_profile_allowed")
    llm = RecordingLLM()
    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config,
            mcp_stack=stack,
            llm=llm,
            orchestration_settings=OrchestrationSettings(
                planning_mode="llm_first",
                max_steps=250,
                checkpoint_backend="memory",
                enforce_verification_reserve=False,
            ),
        )
        final = await run_engagement(
            engagement, deps, objective="승인된 자료 조회·데이터 전달만 확인"
        )
        records = await deps.evidence_store.list_for_run(final.run_id)
        progress = deps.methodology_library.progress(records)
        print(
            json.dumps(
                {
                    "run_id": str(final.run_id),
                    "actions_executed": final.actions_executed,
                    "reference_progress": progress,
                    "recording_llm_calls": llm.calls,
                    "reference_context_calls": llm.reference_context_calls,
                    "live_llm_tested": False,
                    "target_network_tools_enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not all(item["status"] == "verified" for item in progress):
            raise RuntimeError("demo_reference_verification_incomplete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-config", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.app_config))
