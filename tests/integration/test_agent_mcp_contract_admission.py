"""Current configuration and Catalog bindings are checked at actual invocation."""

from dataclasses import replace

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.enums import GraphNodeType, PolicyVerdict
from redteam.core.ids import GraphNodeId, RunId
from redteam.orchestration.engine import ActionIntent, InvestigationEngine, validate_current_action
from redteam.orchestration.nodes import execute_node, policy_node
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h
from tests.integration.test_execution_agent_e2e import draft_path, engagement, turn_action
from tests.unit.test_execution_agent_bridge import execution_descriptor


@pytest.mark.parametrize("opaque", [False, True])
async def test_pinned_seed_keeps_port_through_compilation_policy_and_execution(opaque):
    host = "example.test:8443"
    configured = h.make_engagement(host)
    if opaque:
        configured.metadata["target_refs"] = {host: "pinned-mcp-reference"}
    llm = h.ScriptedLLM()
    gateway = DummyToolGateway()
    deps = replace(
        build_test_application(
            tool_catalog=h.FakeCatalog([h.inventory_descriptor()]),
            tool_gateway=gateway,
            llm_client=llm,
        ),
        engagement=configured,
    )
    runtime = h.make_runtime("run-pinned-port", str(configured.engagement_id))
    graph = AttackGraphService(deps.graph_store)
    [node] = await seed_from_engagement(graph, configured, run_id=runtime.run_id)
    assert str(node.id) == "host:" + host
    assert facts_of(node.properties)["target_ref"] == ("pinned-mcp-reference" if opaque else host)
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, GraphObservation())
    raw = h.draft_path(str(node.id))
    llm.add("path_review", h.review_accept())
    path = await engine.register_path(
        PathPlan.model_validate(raw), runtime, context, GraphObservation()
    )
    intent = h.turn_actions(str(node.id))["actions"][0]
    intent["arguments"] = {}
    compiled, errors = await engine.compile_actions(
        path, [ActionIntent.model_validate(intent)], runtime, context
    )
    assert not errors and len(compiled) == 1
    step = compiled[0].plan_step
    assert step.arguments["host"] == host
    assert step.arguments["target_ref"] == ("pinned-mcp-reference" if opaque else host)
    state = initial_graph_state(runtime)
    proposal = ActionProposal(
        action_id="action-pinned",
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        agent_id=h.RECON_AGENT,
        capability=step.capability,
        operation=step.operation,
        target=step.resolved_target(),
        arguments=step.arguments,
        reason="inspect explicit pinned target",
        created_at=h.utcnow(),
    )
    state.update(plan_step=step, action_proposal=proposal)
    state = await policy_node(state, deps=deps)
    assert state["policy_decision"].verdict == PolicyVerdict.ALLOW
    state = await execute_node(state, deps=deps)
    assert state["tool_result"].success and len(gateway.invocations) == 1
    assert gateway.invocations[0].arguments["host"] == host


async def prepared(*, static=False):
    configured = engagement()
    tool = execution_descriptor()
    raw_path, raw_intent = draft_path(), turn_action()["actions"][0]
    if static:
        configured.metadata["apk_ref"] = "apk-policy"
        tool = tool.model_copy(
            update={
                "tool_id": "analysis.decode_apk",
                "tool_name": "decode_apk",
                "operations": ["static_analysis"],
                "input_schema": {
                    "type": "object",
                    "properties": {"apk_ref": {"type": "string"}},
                    "required": ["apk_ref"],
                    "additionalProperties": False,
                },
            }
        )
        raw_path["steps"][0]["allowed_operations"] = ["analysis.static_analysis"]
        raw_intent.update(operation="static_analysis", tool_id="analysis.decode_apk", arguments={})
    llm = h.ScriptedLLM()
    deps = replace(
        build_test_application(tool_catalog=h.FakeCatalog([tool]), llm_client=llm),
        engagement=configured,
    )
    runtime = RuntimeState(
        run_id=RunId("run-test"),
        engagement_id=configured.engagement_id,
        current_goal=raw_path["goal"],
        remaining_budget=4,
    )
    service = AttackGraphService(deps.graph_store)
    await seed_from_engagement(service, configured, run_id=runtime.run_id)
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, GraphObservation())
    llm.add("path_review", h.review_accept())
    path = await engine.register_path(
        PathPlan.model_validate(raw_path), runtime, context, GraphObservation()
    )
    candidates, errors = await engine.compile_actions(
        path, [ActionIntent.model_validate(raw_intent)], runtime, context
    )
    assert not errors and len(candidates) == 1
    assert await validate_current_action(deps, runtime, candidates[0].plan_step) == []
    return deps, runtime, service, candidates[0]


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "path",
        "asset_ref",
        "denied",
        "graph_path",
        "graph_asset",
        "tool_unbound",
        "schema",
    ],
)
@pytest.mark.parametrize("phase", ["before_policy", "before_execution"])
async def test_changed_execution_binding_never_reaches_gateway(change, phase):
    deps, runtime, service, candidate = await prepared()
    gateway = DummyToolGateway()
    deps = replace(deps, tool_gateway=gateway)
    step = candidate.plan_step
    proposal = ActionProposal(
        action_id="action-current-recipe",
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        agent_id="analysis-agent",
        capability=step.capability,
        operation=step.operation,
        target=step.resolved_target(),
        arguments=step.arguments,
        reason="configured recipe",
        created_at=h.utcnow(),
    )
    state = initial_graph_state(runtime)
    state.update(plan_step=step, action_proposal=proposal)
    if phase == "before_execution":
        state = await policy_node(state, deps=deps)
        assert state["policy_decision"].verdict == PolicyVerdict.ALLOW
    if change == "disabled":
        deps.engagement.metadata["execution_agent"]["enabled"] = False
    elif change == "path":
        deps.engagement.metadata["execution_agent"]["assets"]["workspace-test"]["path"] = (
            "other.txt"
        )
    elif change == "asset_ref":
        deps.engagement.metadata["asset_ref"] = "other-policy"
    elif change == "denied":
        deps.engagement.scope.denied_assets.append("workspace-test")
    elif change in {"graph_path", "graph_asset"}:
        node = await service.get_node(GraphNodeId("asset:workspace-test"))
        facts = dict(facts_of(node.properties))
        facts["execution_workspace_path" if change == "graph_path" else "asset_ref"] = "../other"
        await service.upsert_node(GraphNodeType.ASSET, node_id=node.id, facts=facts)
    elif change == "tool_unbound":
        tool = deps.tool_catalog.get(candidate.plan_step.tool_id)
        tool.operations = []
    else:
        deps.tool_catalog.get(candidate.plan_step.tool_id).input_schema = {
            "type": "object",
            "required": ["new_required_field"],
        }
    assert await validate_current_action(deps, runtime, candidate.plan_step)
    if phase == "before_policy":
        state = await policy_node(state, deps=deps)
        assert state["policy_decision"].verdict == PolicyVerdict.DENY
    else:
        state = await execute_node(state, deps=deps)
        assert state["tool_result"].success is False
    assert gateway.invocations == []


async def test_apk_activation_is_bound_to_selected_asset_and_rechecked():
    deps, runtime, service, candidate = await prepared(static=True)
    assert candidate.plan_step.arguments == {"apk_ref": "apk-policy"}
    await service.upsert_node(GraphNodeType.ASSET, key="other-apk", facts={"apk_ref": "other"})
    node = await service.get_node(GraphNodeId("asset:workspace-test"))
    facts = dict(facts_of(node.properties))
    facts["apk_ref"] = ""
    await service.upsert_node(GraphNodeType.ASSET, node_id=node.id, facts=facts)
    problems = await validate_current_action(deps, runtime, candidate.plan_step)
    assert any("no longer active" in problem for problem in problems)
    # Another APK still activates the role, but it cannot authorize this asset.
    engine = InvestigationEngine(deps)
    context = await engine.context(runtime, GraphObservation())
    path = await deps.path_store.get_active(str(runtime.run_id))
    raw_intent = turn_action()["actions"][0]
    raw_intent.update(operation="static_analysis", tool_id="analysis.decode_apk", arguments={})
    compiled, errors = await engine.compile_actions(
        path, [ActionIntent.model_validate(raw_intent)], runtime, context
    )
    assert not compiled and errors
