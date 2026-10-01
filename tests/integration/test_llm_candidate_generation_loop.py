"""Model-generated candidate travels the full LLM-led planning loop.

Migration: retired llm_candidate_generation on/off flag (settings now
Literal[True]) and old observe/plan/policy node seam. Explicit scripted
model proposes parameter_fuzz (never emitted by any web playbook) through
the production InvestigationEngine; execution must land on the graph's
observed target, never the node-id template nor the model-claimed string.
The off-case is retired: settings reject False, so we assert that instead
of executing nothing. No rule restoration, no guard monkeypatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h

RUN = RunId("run_gen")
ENGAGEMENT = EngagementId("eng_gen")
# The node id normalises the identifier segment to a template; the concrete
# address the endpoint was observed at is a separate graph fact.
TEMPLATE_PATH = "/orders/{id}"
OBSERVED_PATH = "/orders/1234"
GEN_OPERATION = "parameter_fuzz"  # never emitted by a web playbook


class _GenLLM(h.ScriptedLLM):
    def __init__(self, endpoint_id: str = "") -> None:
        super().__init__()
        self._endpoint_id = endpoint_id
        self.action_calls = 0

    def _draft(self) -> dict[str, Any]:
        return {
            "path_id": "path-draft-gen",
            "run_id": "run-x",
            "mission_id": None,
            "version": 1,
            "parent_version": None,
            "status": "draft",
            "reason": "scripted gen draft",
            "goal": "fuzz observed object",
            "steps": [
                {
                    "step_id": "step-1",
                    "objective": "fuzz sibling id",
                    "agent_id": "web-api-agent",
                    "target_node_ids": [self._endpoint_id],
                    "principal_refs": [],
                    "allowed_operations": ["web.parameter_fuzz"],
                    "depends_on": [],
                    "required_fact_refs": [],
                    "unknowns": [],
                    "completion_criteria": [
                        {"kind": "evidence", "description": "result", "min_evidence": 1}
                    ],
                    "required": True,
                    "status": "pending",
                    "outcome": None,
                    "evidence_ids": [],
                }
            ],
            "hypothesis_ids": [],
            "evidence_ids": [],
            "changed_step_ids": [],
            "review_records": {},
        }

    def _action(self) -> dict[str, Any]:
        return {
            "mode": "actions",
            "reason": "model fuzz",
            "actions": [
                {
                    "path_step_id": "step-1",
                    "agent_id": "web-api-agent",
                    "target_node_id": self._endpoint_id,
                    "principal_ref": None,
                    "capability": "web",
                    "operation": GEN_OPERATION,
                    "tool_id": "web.parameter_fuzz",
                    "arguments": {"path": TEMPLATE_PATH},
                    "reason": "sibling object id worth fuzzing",
                    "expected_observation": "fuzz result",
                    "hypothesis": None,
                    "existing_hypothesis_id": None,
                    "expectation": None,
                    "authentication_check": None,
                    "availability_impact": False,
                }
            ],
            "path": None,
            "path_step_id": None,
            "completion": {},
            "evidence_request": {},
            "hypothesis_update": {},
        }

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "path_draft":
            return self._draft()
        if route == "path_review":
            return h.review_accept()
        if route == "next_action":
            self.action_calls += 1
            return self._action()
        raise AssertionError(f"no gen answer for {route!r}")

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        return ""


def _fuzz_tool() -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("web.parameter_fuzz"),
        server_id="test",
        tool_name="parameter_fuzz",
        operations=[GEN_OPERATION],
        capability_tags=["web"],
        target_types=["endpoint"],
        risk_level=RiskLevel.LOW,
    )


def _build() -> tuple[Any, DummyToolGateway, _GenLLM]:
    gateway = DummyToolGateway(tools=[_fuzz_tool()])
    llm = _GenLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(checkpointing=False),
    )
    return deps, gateway, llm


async def _seed_endpoint(deps: Any, llm: _GenLLM) -> str:
    service = AttackGraphService(deps.graph_store)
    endpoint_id = endpoint_node_id("GET", TEMPLATE_PATH, host="a.local")
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        node_id=endpoint_id,
        attrs={"web_inspected": True, "status_code": 200, "path_allowlisted": True},
        facts={
            "host": "a.local",
            "path": OBSERVED_PATH,
            "method": "GET",
            "observed_target": OBSERVED_PATH,
        },
        provenance=Provenance(
            source="tool:recon.inventory",
            run_id=str(RUN),
            evidence_id="ev-gen-seed",
            confidence=Confidence.MEASURED,
        ),
    )
    llm._endpoint_id = str(endpoint_id)
    # Ground the endpoint for the new known-target gate: the explicit model
    # only plans against evidence-grounded targets, and the real run sets
    # current_goal explicitly instead of leaving it None.
    await deps.evidence_store.append(
        h.make_tool_evidence(
            evidence_id="ev-gen-seed",
            run_id=str(RUN),
            engagement_id=str(ENGAGEMENT),
            action_id="action-gen-seed",
            invocation_id="inv-gen-seed",
            path_id="path-gen-seed",
            path_version=1,
            step_id="step-0",
            target_node_id=str(endpoint_id),
            output={
                "host": "a.local",
                "path": OBSERVED_PATH,
                "method": "GET",
                "observed_target": OBSERVED_PATH,
            },
        )
    )
    return str(endpoint_id)


async def test_generated_candidate_runs_through_control_at_the_observed_target() -> None:
    deps, gateway, llm = _build()
    # Seeded id lives on the model for draft/action; no separate local needed.
    await _seed_endpoint(deps, llm)
    from redteam.attack_graph.observation import GraphObservation

    runtime = RuntimeState(
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        remaining_budget=10,
        current_goal="inspect observed object",
    )
    engine = InvestigationEngine(deps)
    path, compiled, note = await engine.decide(runtime, GraphObservation())
    assert note is None
    assert llm.action_calls == 1, "generation seam was not exercised"
    assert len(compiled) == 1
    assert (compiled[0].plan_step.capability, compiled[0].plan_step.operation) == (
        "web",
        GEN_OPERATION,
    )
    assert compiled[0].plan_step.resolved_target().path == OBSERVED_PATH
    assert str(compiled[0].tool.tool_id) == "web.parameter_fuzz"


def test_generation_flag_is_retired_to_always_on() -> None:
    with pytest.raises(Exception, match="llm_candidate_generation|Literal|True"):
        OrchestrationSettings.model_validate(
            {"llm_candidate_generation": False, "checkpointing": False}
        )
    assert OrchestrationSettings.model_validate({}).llm_candidate_generation is True
