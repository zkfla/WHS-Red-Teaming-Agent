from __future__ import annotations

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import GraphNodeType
from redteam.orchestration.agents.base import available_steps
from redteam.orchestration.agents.builtin import ANALYSIS_AGENT, MOBILE_AGENT


def test_mobile_agent_no_longer_proposes_a_mobile_static_analysis_action() -> None:
    assert MOBILE_AGENT.playbook
    assert all(step.capability == "analysis" for step in MOBILE_AGENT.playbook)
    assert all(step.operation == "static_analysis" for step in MOBILE_AGENT.playbook)
    assert all(str(step.tool_id).startswith("analysis.") for step in MOBILE_AGENT.playbook)


def test_analysis_agent_declares_the_apktool_pipeline() -> None:
    assert ANALYSIS_AGENT.capabilities == ["analysis"]
    assert [str(step.tool_id) for step in ANALYSIS_AGENT.playbook] == [
        "analysis.decode_apk",
        "analysis.get_manifest",
        "analysis.list_smali_files",
        "analysis.search_in_files",
    ]
    assert len({step.attempt_record_key for step in ANALYSIS_AGENT.playbook}) == 4
    assert all(step.capability == "analysis" for step in ANALYSIS_AGENT.playbook)
    assert all(step.operation == "static_analysis" for step in ANALYSIS_AGENT.playbook)


async def test_analysis_pipeline_binds_asset_then_decode_project_facts() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    asset = await service.upsert_node(
        GraphNodeType.ASSET,
        key="insecurebankv2",
        attrs={"in_scope": True},
        facts={"asset_ref": "/work/input/InsecureBankv2.apk"},
    )

    initial = await available_steps(
        ANALYSIS_AGENT,
        service.view(),
        GraphObservation(),
        step=1,
    )

    assert [step.step_id for step in initial] == ["decode-apk"]
    assert initial[0].arguments == {"apk_path": "/work/input/InsecureBankv2.apk"}

    await service.record_attempt(asset.id, initial[0].attempt_record_key, step=1)
    await service.upsert_node(
        GraphNodeType.ASSET,
        node_id=asset.id,
        attrs={"analysis_decoded": True},
        facts={"project_dir": "/work/output/InsecureBankv2"},
        step=1,
    )
    observation = await service.observe(step=2)

    followups = await available_steps(
        ANALYSIS_AGENT,
        service.view(),
        observation,
        step=2,
    )

    assert [step.step_id for step in followups] == [
        "inspect-manifest",
        "list-smali",
        "search-sensitive-strings",
    ]
    assert all(
        step.arguments["project_dir"] == "/work/output/InsecureBankv2"
        for step in followups
    )
