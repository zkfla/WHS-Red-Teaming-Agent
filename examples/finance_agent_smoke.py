"""Exercise the default Agent loop through real MCP servers in the isolated lab.

First retain the shipped approval policy and assert Web waits. Then run an
explicit synthetic-only read test with auto-approval of its high-risk Web tool.

Contract: the shipped deployment config requires a real model (environment
based, never 'fake'). This smoke therefore injects an explicit scripted
stand-in model at the composition root and executes only synthetic lab
targets through the real MCP adapters. No external API calls, no cost. The
stand-in answers ``complete_native`` tool calls plus the genuine verification
and settlement reviews.
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
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.tool_discovery import TOOL_GET_DETAIL_ID, TOOL_LIST_CARDS_ID

# Synthetic lab identity, matching the scope's target alias.
_LAB_TARGET_REF = "bank-lab"
_LAB_PATH = "/"


def _config_path() -> str:
    container = Path("/app/finance-app.yaml")
    if container.exists():
        return str(container)
    return str(Path(__file__).parents[1] / "deploy" / "mcp" / "finance" / "app.yaml")


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


def _executed_evidence_ids(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _harvest_lab_cards(messages: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Collect (tool_name -> tool_ids) from card list results seen so far.

    Same card payload shape the approved-MCP SessionLLM harvests, duplicated
    here because the compose gate mounts only this example file (no tests/
    tree), so the example must stay self-contained.
    """
    found: dict[str, set[str]] = {}
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        payload = body.get("payload") if isinstance(body, dict) else None
        cards = payload.get("cards") if isinstance(payload, dict) else None
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            name = card.get("tool_name")
            identity = card.get("tool_id")
            if isinstance(name, str) and isinstance(identity, str):
                found.setdefault(name, set()).add(identity)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


class _ScriptedLabModel:
    """Deterministic stand-in model for the synthetic lab only (no network).

    Speaks the native product contract: ``complete_native`` with
    ``NativeCompletion``/``NativeToolCall`` payloads addressed to the exposed
    tool names (via ``exposed_tool_name``), plus the genuine
    ``complete_structured`` reviews for verification and task/goal settlement.
    Strategy artifacts (path drafts/reviews, turn-action modes, step completion
    intents) are not scripted because they are no longer the execution contract.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.turns = 0
        self._listed = False
        self._details_requested = False
        self._remote_sequence: list[tuple[str, dict[str, Any]]] = []
        self._issued_remote: list[str] = []
        self._settlement_evidence: list[str] = []
        self._task_call: str | None = None
        self._goal_call: str | None = None
        self._remote_errors: dict[str, str] = {}

    def use_registered_tools(self, tool_ids: list[str]) -> None:
        """Inject the catalog-resolved tool ids after discovery, before run.

        The lab registers server-prefixed tool ids; the scripted sequence must
        use those actual identities, never hardcoded alias guesses.
        """
        assert len(tool_ids) == 4, f"lab sequence needs four registered tools: {tool_ids}"
        self._remote_sequence = [
            (str(tool_id), {"target_ref": _LAB_TARGET_REF, "path": _LAB_PATH})
            for tool_id in tool_ids
        ]

    def error_summary(self) -> dict[str, str]:
        """Short safe per-call reasons for TRACE; never full responses."""
        return dict(self._remote_errors)

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

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
        if route == "analysis_verify":
            return {
                "meaning": "scripted",
                "outcome": "success",
                "reason": "lab",
                "confidence": 0.8,
                "claim_supported": True,
            }
        if route == "task_completion_review":
            review_payload = json.loads(messages[0]["content"])
            receipts = list(review_payload.get("verified_source_ids", []))
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "lab task done",
                "claims": [
                    {
                        "description": "actual lab task result observed in stored evidence",
                        "kind": "observation",
                        "evidence_ids": receipts,
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "goal review must name every proven origin"
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
                                "kind": "observation",
                                "evidence_ids": receipts,
                                "target_node_ids": [],
                                "principal_refs": [],
                            }
                        ],
                    }
                    for criterion in payload["mission"]["success_criteria"]
                ],
            }
        raise AssertionError(
            f"legacy strategy route is not part of the native loop: {route!r}"
        )

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        sequence = self._remote_sequence
        assert sequence, "lab model needs the registered tool ids injected before run"
        names = _exposed_names(tools)
        list_name = exposed_tool_name(TOOL_LIST_CARDS_ID)
        detail_name = exposed_tool_name(TOOL_GET_DETAIL_ID)
        self.turns += 1
        if not self._listed:
            # Lazy discovery-request contract (same as the approved-MCP
            # SessionLLM): remote tools are advertised only after the model
            # asks for cards and then for each needed schema.
            assert list_name in names, "native model needs the card discovery helper"
            assert detail_name in names, "native model needs the detail lookup helper"
            self._listed = True
            self.calls.append({"turn": self.turns, "tool_calls": ["list"]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-lab-list",
                        name=list_name,
                        arguments={"kind": "mcp_tool"},
                    ),
                ),
                text="",
            )
        if not self._details_requested:
            advertised = {
                tool_id
                for ids in _harvest_lab_cards(messages).values()
                for tool_id in ids
            }
            for tool_id, _ in sequence:
                assert tool_id in advertised, (
                    f"native model needs the advertised lab tool: {tool_id}"
                )
            self._details_requested = True
            self.calls.append({"turn": self.turns, "tool_calls": ["detail"]})
            return NativeCompletion(
                tool_calls=tuple(
                    NativeToolCall(
                        call_id=f"call-lab-detail-{index + 1}",
                        name=detail_name,
                        arguments={"tool_id": tool_id},
                    )
                    for index, (tool_id, _) in enumerate(sequence)
                ),
                text="",
            )
        remote = [exposed_tool_name(tool_id) for tool_id, _ in sequence]
        for index, (tool_id, _) in enumerate(sequence):
            assert remote[index] in names, f"native model needs the registered lab tool: {tool_id}"
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        # Settlement helpers are local tools: bindings resolve them by
        # registered name even when lazy discovery has not advertised their
        # schemas, so no advertisement assert here (unlike the remote MCP
        # tools above, which must be advertised before first use).
        settled = [
            call_id
            for call_id in self._issued_remote
            if (_tool_result(messages, call_id) or {}).get("executed") is True
        ]
        if len(settled) < len(self._issued_remote):
            # A gated, refused, or failed call returns its reason instead of
            # executing; the run then suspends (e.g. APPROVAL_PENDING) with no
            # retry from this script. Keep only the short safe reason.
            for call_id in self._issued_remote:
                if call_id in settled or call_id in self._remote_errors:
                    continue
                body = _tool_result(messages, call_id) or {}
                self._remote_errors[call_id] = str(body.get("error") or "no result yet")[:200]
            self.calls.append({"turn": self.turns, "tool_calls": []})
            return NativeCompletion(tool_calls=(), text="")
        if len(self._issued_remote) < len(sequence):
            index = len(self._issued_remote)
            call_id = f"call-lab-{index + 1}"
            self._issued_remote.append(call_id)
            self.calls.append({"turn": self.turns, "tool_calls": [remote[index]]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=call_id,
                        name=remote[index],
                        arguments=dict(sequence[index][1]),
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            evidenced = _executed_evidence_ids(messages)
            assert evidenced, "task settlement needs the executed call evidence"
            self._settlement_evidence = evidenced
            self._task_call = "call-lab-task"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "lab task evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            self.calls.append({"turn": self.turns, "tool_calls": []})
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-lab-goal"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "lab goal evidence ready",
                            "evidence_ids": list(self._settlement_evidence),
                        },
                    ),
                ),
                text="",
            )
        self.calls.append({"turn": self.turns, "tool_calls": []})
        return NativeCompletion(tool_calls=(), text="")

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
            lab_model.use_registered_tools(
                [
                    expected_tools["recon.http_probe"],
                    expected_tools["web.http_request"],
                    expected_tools["recon.html_inventory"],
                    expected_tools["web.http_security_posture"],
                ]
            )
            final = await run_engagement(engagement, deps=deps)
            started = [
                e.payload["tool_id"]
                for e in events
                if e.event_type == EventType.TOOL_EXECUTION_STARTED
            ]
            nodes = await AttackGraphService(deps.graph_store).list_nodes()
            scripted = [entry.get("tool_calls", []) for entry in lab_model.calls]
            print(
                f"TRACE: approval={require_approval} termination={final.termination_reason} "
                f"actions={final.actions_by_agent} tools={started} "
                f"nodes={[(str(node.id), node.type.value) for node in nodes]} "
                f"script_turns={lab_model.turns} script_calls={scripted} "
                f"remote_errors={lab_model.error_summary()}",
                flush=True,
            )
            assert expected_tools["recon.http_probe"] in started, started
            assert not [e for e in events if e.event_type == EventType.TOOL_EXECUTION_FAILED]
            if require_approval:
                assert final.termination_reason == TerminationReason.APPROVAL_PENDING, (
                    final.termination_reason,
                    lab_model.turns,
                    lab_model.error_summary(),
                )
                assert expected_tools["web.http_request"] not in started, started
            else:
                assert final.termination_reason == TerminationReason.GOAL_ACHIEVED, (
                    final.termination_reason,
                    lab_model.turns,
                    lab_model.error_summary(),
                )
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
