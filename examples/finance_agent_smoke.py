"""Exercise the default Agent loop through real MCP servers in the isolated lab.

First retain the shipped approval policy and assert Web waits. Then run an
explicit synthetic-only read test with auto-approval of its high-risk Web tool.

Contract: the shipped deployment config requires a real model (environment
based, never 'fake'). This smoke therefore injects an explicit scripted
stand-in model at the composition root and executes only synthetic lab
targets through the real MCP adapters. No external API calls, no cost.
"""

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.config import load_app_config
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason

# Synthetic lab identity, matching the scope's graph node and target alias.
_LAB_HOST = "mock-bank"
_LAB_NODE = "host:mock-bank"
_LAB_TARGET_REF = "bank-lab"
_LAB_PATH = "/"


def _config_path() -> str:
    container = Path("/app/finance-app.yaml")
    if container.exists():
        return str(container)
    return str(Path(__file__).parents[1] / "deploy" / "mcp" / "finance" / "app.yaml")


class _ScriptedLabModel:
    """Deterministic stand-in model for the synthetic lab only (no network)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._actions: list[dict[str, Any]] = [
            self._action(
                step="step-1",
                agent="recon-agent",
                tool="recon.http_probe",
                capability="recon",
                operation="service_discovery",
            ),
            self._action(
                step="step-2",
                agent="web-api-agent",
                tool="web.http_request",
                capability="web",
                operation="http_request",
            ),
            self._action(
                step="step-3",
                agent="recon-agent",
                tool="recon.html_inventory",
                capability="recon",
                operation="endpoint_discovery",
            ),
            self._action(
                step="step-4",
                agent="web-api-agent",
                tool="web.http_security_posture",
                capability="web",
                operation="response_analysis",
            ),
        ]

    @staticmethod
    def _action(
        *,
        step: str,
        agent: str,
        tool: str,
        capability: str,
        operation: str,
    ) -> dict[str, Any]:
        return {
            "path_step_id": step,
            "agent_id": agent,
            "target_node_id": _LAB_NODE,
            "principal_ref": None,
            "capability": capability,
            "operation": operation,
            "tool_id": tool,
            "arguments": {"target_ref": _LAB_TARGET_REF, "path": _LAB_PATH},
            "reason": f"synthetic lab {tool}",
            "expected_observation": f"{tool} returns a synthetic result",
            "hypothesis": None,
            "existing_hypothesis_id": None,
            "expectation": None,
            "authentication_check": None,
            "availability_impact": False,
        }

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        # Minimal draft is purpose+steps; code owns path/run/version.
        if "steps" in props:
            return "path_draft"
        if "decision" in props:
            return "path_review"
        if "mode" in props:
            return "next_action"
        if "criteria" in props:
            return "goal_completion_review"
        if "accepted" in props:
            return "completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    def _draft(self) -> dict[str, Any]:
        def step(step_id: str, agent: str, allowed: str) -> dict[str, Any]:
            return {
                "step_id": step_id,
                "objective": f"synthetic {step_id}",
                "agent_id": agent,
                "target_node_ids": [_LAB_NODE],
                "principal_refs": [],
                "allowed_operations": [allowed],
                "depends_on": [],
                "required_fact_refs": [],
                "unknowns": [],
                "completion_criteria": [
                    {"kind": "evidence", "description": "synthetic", "min_evidence": 1}
                ],
                "required": True,
            }

        # Minimal model input only; runtime hydrates code-owned fields.
        return {
            "reason": "scripted lab draft",
            "steps": [
                step("step-1", "recon-agent", "recon.service_discovery"),
                step("step-2", "web-api-agent", "web.http_request"),
                step("step-3", "recon-agent", "recon.endpoint_discovery"),
                step("step-4", "web-api-agent", "web.response_analysis"),
            ],
        }

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return self._draft()
        if route == "path_review":
            return {"decision": "accept", "reason": "lab accept", "path": None}
        if route == "next_action":
            payload = json.loads(messages[0]["content"])
            active_path = payload["active_path"]
            for step in active_path["steps"]:
                if step["status"] == "completed":
                    continue
                action = next(a for a in self._actions if a["path_step_id"] == step["step_id"])
                receipts = [
                    record["evidence_id"]
                    for record in payload["evidence"]
                    if record["metadata"].get("path_id") == active_path["path_id"]
                    and record["metadata"].get("path_version") == active_path["version"]
                    and record["metadata"].get("path_step_id") == step["step_id"]
                    and record["metadata"].get("capability") == action["capability"]
                    and record["metadata"].get("operation") == action["operation"]
                ]
                turn: dict[str, Any] = {
                    "mode": "actions",
                    "reason": "scripted lab action",
                    "actions": [action],
                    "path": None,
                    "path_step_id": None,
                    "completion": {},
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
                if receipts:
                    turn.update(
                        {
                            "mode": "complete_step",
                            "reason": "synthetic tool result received",
                            "actions": [],
                            "path_step_id": step["step_id"],
                            "completion": {
                                "evidence_ids": receipts,
                                "outcome": "success",
                                "reason": "synthetic tool result received",
                                "criterion_results": [],
                            },
                        }
                    )
                return turn
            task = payload.get("active_task")
            raw_status = (task or {}).get("status", "") if isinstance(task, dict) else ""
            task_status = str(raw_status).lower()
            if isinstance(task, dict) and task_status == "running":
                task_receipts = [
                    str(identity).strip()
                    for step in active_path["steps"]
                    for identity in (step.get("evidence_ids") or [])
                    if str(identity).strip()
                ]
                return {
                    "mode": "complete_task",
                    "reason": "lab task evidence ready",
                    "actions": [],
                    "path": None,
                    "path_step_id": None,
                    "completion": {
                        "evidence_ids": task_receipts,
                        "outcome": "success",
                        "reason": "lab task evidence ready",
                        "criterion_results": [],
                    },
                    "evidence_request": {},
                    "hypothesis_update": {},
                }
            return {
                "mode": "complete_goal",
                "reason": "lab sequence done",
                "actions": [],
                "path": None,
                "path_step_id": None,
                "completion": {},
                "evidence_request": {},
                "hypothesis_update": {},
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = [
                identity for step in payload["path"]["steps"] for identity in step["evidence_ids"]
            ]
            return {
                "accepted": True,
                "reason": "lab goal done",
                "criteria": [
                    {
                        "criterion": criterion,
                        "met": True,
                        "evidence_ids": receipts,
                        "claims": [
                            {
                                "description": f"actual lab result for {criterion}",
                                "kind": "semantic",
                                "evidence_ids": receipts,
                            }
                        ],
                    }
                    for criterion in payload["mission"]["success_criteria"]
                ],
            }
        if route == "completion_review":
            review_payload = json.loads(messages[0]["content"])
            if isinstance(review_payload.get("task"), dict):
                excerpts = review_payload.get("evidence_excerpts", [])
                task_ids = [
                    str(item.get("evidence_id", "")).strip()
                    for item in excerpts
                    if isinstance(item, dict) and str(item.get("evidence_id", "")).strip()
                ]
                assert task_ids, "task review needs related stored evidence"
                return {
                    "accepted": True,
                    "reason": "lab task done",
                    "claims": [
                        {
                            "description": "actual lab task result observed in stored evidence",
                            "kind": "semantic",
                            "evidence_ids": task_ids,
                        }
                    ],
                }
            return {"accepted": True, "reason": "lab step done"}
        if route == "analysis_verify":
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "lab",
                "confidence": 0.8,
                "claim_supported": True,
            }
        raise AssertionError(f"no lab answer for {route!r}")

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **_: Any,
    ) -> str:
        return ""


async def exercise(*, require_approval: bool) -> None:
    config = load_app_config(_config_path())
    assert config.engagement.scope.allowed_hosts == ["mock-bank"]
    assert config.engagement.metadata.get("target_refs", {}).get("mock-bank") == "bank-lab"
    # Deployment must require a real model from the environment; the
    # scripted stand-in below is injected explicitly and never comes
    # from this file.
    assert config.llm.provider == "litellm", config.llm.provider
    assert config.llm.provider != "fake"
    assert config.llm.default_model, "deployment must name a real model"
    assert config.llm.api_key_env_var, "deployment must name a key env var"
    if not require_approval:
        # The synthetic-only run opts into auto-approval; high-risk Web tools
        # remain gated by registry risk even when the explicit list is empty.
        config.engagement.approval_policy.require_approval_for = []
        config.engagement.approval_policy.auto_approve_up_to = RiskLevel.HIGH
    with TemporaryDirectory() as directory:
        async with AsyncExitStack() as stack:
            lab_model = _ScriptedLabModel()
            engagement, deps = await build_application(
                config,
                mcp_stack=stack,
                llm=lab_model,
                evidence_dir=Path(directory) / "evidence",
                audit_dir=Path(directory) / "audit",
                approval_dir=Path(directory) / "approvals",
                orchestration_settings=OrchestrationSettings(checkpointing=False, max_steps=20),
            )
            events: list[EventEnvelope] = []

            async def collect(event: EventEnvelope) -> None:
                events.append(event)

            await deps.event_bus.subscribe("*", collect)
            expected_tools = {}
            for alias in (
                "recon.http_probe",
                "recon.html_inventory",
                "web.http_request",
                "web.http_security_posture",
            ):
                descriptor = deps.tool_catalog.get(ToolId(alias))
                assert descriptor is not None, f"missing lab tool: {alias}"
                expected_tools[alias] = str(descriptor.tool_id)
            final = await run_engagement(engagement, deps=deps)
            started = [
                e.payload["tool_id"]
                for e in events
                if e.event_type == EventType.TOOL_EXECUTION_STARTED
            ]
            nodes = await AttackGraphService(deps.graph_store).list_nodes()
            print(
                f"TRACE: approval={require_approval} termination={final.termination_reason} "
                f"actions={final.actions_by_agent} tools={started} "
                f"nodes={[(str(node.id), node.type.value) for node in nodes]}",
                flush=True,
            )
            assert expected_tools["recon.http_probe"] in started, started
            assert not [e for e in events if e.event_type == EventType.TOOL_EXECUTION_FAILED]
            if require_approval:
                assert final.termination_reason == TerminationReason.APPROVAL_PENDING, final
                assert expected_tools["web.http_request"] not in started, started
            else:
                assert final.termination_reason == TerminationReason.GOAL_ACHIEVED, final
                assert expected_tools["web.http_request"] in started, started
                assert expected_tools["recon.html_inventory"] in started, started
                assert expected_tools["web.http_security_posture"] in started, started
                disallowed = [
                    node
                    for node in nodes
                    if attrs_of(node.properties).get("path_allowlisted") is False
                ]
                assert disallowed, "HTML inventory must surface an unallowlisted test path"
                assert all(
                    "web.http_request"
                    not in facts_of(node.properties).get("attempted_operations", [])
                    for node in disallowed
                ), "Agent must not visit an explicitly unallowlisted endpoint"
                assert final.actions_by_agent.get("web-api-agent", 0) >= 1
                assert final.actions_by_agent.get("recon-agent", 0) >= 1
    print(f"PASS: real Agent -> Control -> MCP -> Graph -> Web; approval={require_approval}")


async def main() -> None:
    await exercise(require_approval=True)
    await exercise(require_approval=False)


if __name__ == "__main__":
    asyncio.run(main())
