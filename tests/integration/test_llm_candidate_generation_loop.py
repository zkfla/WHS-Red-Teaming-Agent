"""RC#1 integration: a model-generated candidate travels the full planning
loop -- provider generation -> CandidateCompiler -> Control Plane -> execute --
only when ``llm_candidate_generation`` is on, and it runs against the graph's
observed target, never the model's word or the normalised node-id template.

The unit tests in ``test_llm_candidate_provider.py`` cover the provider in
isolation; this exercises the wiring in ``nodes/plan.py`` that turned the flag
into a real runtime switch, through the same node functions the compiled graph
schedules. The model proposes ``parameter_fuzz`` -- an operation no web
playbook emits -- so an executed ``parameter_fuzz`` invocation can only have
come from generation, not from the deterministic frontier.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import endpoint_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel
from redteam.core.ids import EngagementId, RunId, ToolId
from redteam.orchestration.nodes import execute_node, observe_node, plan_node, policy_node
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.fakes import DummyToolGateway, build_test_application

RUN = RunId("run_gen")
ENGAGEMENT = EngagementId("eng_gen")
# The node id normalises the identifier segment to a template; the concrete
# address the endpoint was observed at is a separate graph fact.
TEMPLATE_PATH = "/orders/{id}"
OBSERVED_PATH = "/orders/1234"
GEN_OPERATION = "parameter_fuzz"  # never emitted by a web playbook


class _ScriptedLLM:
    """Answers the generation call with a fixed action; a single surviving
    candidate needs no LLM selection, so the choice call never fires here."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions
        self.generation_calls = 0

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        if "actions" in response_schema.get("properties", {}):
            self.generation_calls += 1
            return {"actions": self._actions}
        return {"step_id": "llm-0"}

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


def _generated_action(endpoint_id: str) -> dict[str, Any]:
    return {
        "capability": "web",
        "operation": GEN_OPERATION,
        "target_node_id": endpoint_id,
        "tool_id": "web.parameter_fuzz",
        # A model claim that, if trusted, would target the bare template.
        "observed": TEMPLATE_PATH,
        "arguments": {"path": TEMPLATE_PATH},
        "rationale": "sibling object id worth fuzzing",
    }


def _build(*, generation: bool) -> tuple[Any, DummyToolGateway, _ScriptedLLM]:
    gateway = DummyToolGateway(tools=[_fuzz_tool()])
    llm = _ScriptedLLM([])
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(llm_candidate_generation=generation, checkpointing=False),
    )
    return deps, gateway, llm


async def _seed_endpoint(deps: Any, llm: _ScriptedLLM) -> None:
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
    )
    llm._actions = [_generated_action(str(endpoint_id))]


async def test_generated_candidate_runs_through_control_at_the_observed_target() -> None:
    deps, gateway, llm = _build(generation=True)
    await _seed_endpoint(deps, llm)

    state = await observe_node(
        initial_graph_state(RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT)), deps=deps
    )
    state = await plan_node(state, deps=deps)

    proposal = state["action_proposal"]
    assert llm.generation_calls == 1, "generation seam was not exercised"
    assert proposal is not None, "the generated candidate did not reach a proposal"
    assert (proposal.capability, proposal.operation) == ("web", GEN_OPERATION)
    # #3: the executable target is the graph's observed path, not the node-id
    # template and not the model's asserted `observed`.
    assert proposal.target.path == OBSERVED_PATH

    state = await policy_node(state, deps=deps)
    assert [(e.capability, e.operation) for e in deps.control_plane.evaluated] == [
        ("web", GEN_OPERATION)
    ]

    await execute_node(state, deps=deps)
    assert [inv.operation for inv in gateway.invocations] == [GEN_OPERATION]


async def test_generation_off_never_reaches_control_or_execution() -> None:
    deps, gateway, llm = _build(generation=False)
    await _seed_endpoint(deps, llm)

    state = await observe_node(
        initial_graph_state(RuntimeState(run_id=RUN, engagement_id=ENGAGEMENT)), deps=deps
    )
    state = await plan_node(state, deps=deps)

    # With the flag off the model is never consulted, and the deterministic
    # frontier offers no parameter_fuzz -- so nothing of the kind executes.
    assert llm.generation_calls == 0
    if state["action_proposal"] is not None:
        state = await policy_node(state, deps=deps)
        await execute_node(state, deps=deps)
    assert not any(inv.operation == GEN_OPERATION for inv in gateway.invocations)
