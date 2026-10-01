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
        self.calls = []
        self.reference_context_calls = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        self.calls.append((messages, system))
        payload = json.loads(messages[0]["content"])
        self.reference_context_calls += int(
            bool(payload.get("Untrusted methodology reference DATA"))
        )
        properties = response_schema.get("properties", {})
        if "path_id" in properties and "steps" in properties:
            target = payload["methodology_reference_options"][0]["target_node_id"]
            return {
                "path_id": "reference-demo",
                "run_id": "placeholder",
                "version": 1,
                "status": "draft",
                "reason": "승인된 자료 조회 데모",
                "goal": payload["goal"],
                "steps": [
                    {
                        "step_id": "read-reference",
                        "objective": "참고 자료를 MCP로 조회",
                        "agent_id": "methodology-agent",
                        "target_node_ids": [target],
                        "allowed_operations": ["analysis.file_read"],
                        "completion_criteria": [{"kind": "evidence", "description": "조회 결과"}],
                    }
                ],
            }
        if "decision" in properties:
            return {"decision": "accept", "reason": "합성 데모의 읽기 경로입니다."}
        if "claim_supported" in properties:
            return {
                "meaning": "조회 관측",
                "outcome": "success",
                "reason": "조회 결과만 관측",
                "confidence": 0.8,
                "claim_supported": False,
            }
        if "mode" in properties:
            pending = next(
                (
                    item
                    for item in payload["methodology_reference_options"]
                    if item["status"] == "pending"
                ),
                None,
            )
            if pending is None:
                # Deliberately no semantic goal-completion claim from a demo model.
                return {
                    "mode": "hold",
                    "reason": "참고 데이터 전달만 확인. 실검증은 하지 않았습니다.",
                }
            action = {
                key: pending[key]
                for key in (
                    "agent_id",
                    "target_node_id",
                    "capability",
                    "operation",
                    "tool_id",
                    "arguments",
                )
            }
            action.update(path_step_id="read-reference", reason="승인된 참고 데이터 조회")
            return {"mode": "actions", "reason": "참고 데이터 조회 데모", "actions": [action]}
        raise ValueError("demo_unexpected_decision_schema")


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
                    "recording_llm_calls": len(llm.calls),
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
