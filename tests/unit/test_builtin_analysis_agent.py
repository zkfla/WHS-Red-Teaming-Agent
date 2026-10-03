from __future__ import annotations

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import GraphNodeType
from redteam.orchestration.agents.base import available_steps
from redteam.orchestration.agents.builtin import ANALYSIS_AGENT, MOBILE_AGENT


def test_mobile_agent_declares_launch_and_runtime_observation_pipeline() -> None:
    assert MOBILE_AGENT.capabilities == ["mobile"]
    assert [str(step.tool_id) for step in MOBILE_AGENT.playbook] == [
        "mobile.mobile_launch_app",
        "mobile.mobile_get_device_logs",
        "mobile.mobile_list_crashes",
        "mobile.mobile_get_crash",
    ]
    assert len({step.attempt_record_key for step in MOBILE_AGENT.playbook}) == 4
    assert all(step.capability == "mobile" for step in MOBILE_AGENT.playbook)
    assert all(step.assessment_kind == "observation" for step in MOBILE_AGENT.playbook)


async def test_mobile_pipeline_binds_configured_device_package_and_observed_crash() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    asset = await service.upsert_node(
        GraphNodeType.ASSET,
        key="insecurebankv2",
        attrs={"in_scope": True},
        facts={
            "device_serial": "emulator-5554",
            "device_ref": "redteam_x86",
            "package_name": "com.android.insecurebankv2",
        },
    )

    initial = await available_steps(
        MOBILE_AGENT,
        service.view(),
        GraphObservation(),
        step=1,
    )

    assert [step.step_id for step in initial] == ["launch-mobile-app"]
    assert initial[0].arguments == {
        "device": "redteam_x86",
        "packageName": "com.android.insecurebankv2",
    }

    await service.record_attempt(asset.id, initial[0].attempt_record_key, step=1)
    await service.upsert_node(
        GraphNodeType.ASSET,
        node_id=asset.id,
        attrs={"mobile_app_launched": True},
        step=1,
    )
    observation = await service.observe(step=2)

    followups = await available_steps(
        MOBILE_AGENT,
        service.view(),
        observation,
        step=2,
    )

    assert [step.step_id for step in followups] == [
        "observe-device-logs",
        "list-mobile-crashes",
    ]
    assert followups[0].arguments == {
        "device": "redteam_x86",
        "filter": ["process=com.android.insecurebankv2"],
        "limit": 200,
    }
    assert followups[1].arguments == {"device": "redteam_x86"}

    await service.upsert_node(
        GraphNodeType.ASSET,
        node_id=asset.id,
        attrs={"mobile_crash_observed": True},
        facts={"crash_id": "crash-17"},
        step=2,
    )
    observation = await service.observe(step=3)

    with_crash = await available_steps(
        MOBILE_AGENT,
        service.view(),
        observation,
        step=3,
    )

    crash_step = next(step for step in with_crash if step.step_id == "inspect-mobile-crash")
    assert crash_step.arguments == {"device": "redteam_x86", "id": "crash-17"}


def test_analysis_agent_keeps_apktool_pipeline_before_optional_jadx_step() -> None:
    assert ANALYSIS_AGENT.capabilities == ["analysis"]
    tool_ids = [str(step.tool_id) for step in ANALYSIS_AGENT.playbook]
    assert tool_ids[:4] == [
        "analysis.decode_apk",
        "analysis.get_manifest",
        "analysis.list_smali_files",
        "analysis.search_in_files",
    ]
    assert tool_ids[4:] == ["analysis.get_android_manifest"]
    assert ANALYSIS_AGENT.playbook[4].require_healthy_tool is True
    assert len({step.attempt_record_key for step in ANALYSIS_AGENT.playbook}) == 5
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

    for followup in followups:
        await service.record_attempt(asset.id, followup.attempt_record_key, step=2)
    await service.upsert_node(
        GraphNodeType.ASSET,
        node_id=asset.id,
        attrs={
            "manifest_analyzed": True,
            "smali_inventoried": True,
            "sensitive_strings_searched": True,
        },
        step=2,
    )

    optional = await available_steps(
        ANALYSIS_AGENT,
        service.view(),
        await service.observe(step=3),
        step=3,
    )

    assert [step.step_id for step in optional] == ["inspect-jadx-manifest"]
    assert optional[0].arguments == {}
