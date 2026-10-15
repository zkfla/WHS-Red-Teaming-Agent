"""Opt-in direct Docker control-chain proof on test-owned resources.

Real: open_central_run -> build_application(direct_target_shell=...), the real
Control Plane approval for the MEDIUM direct target descriptor (engagement
ApprovalPolicy.risk_threshold=MEDIUM), a human resolve, and resume of the same
invocation into the direct execution policy on the owned Kali workroom.
Scripted: the worker model only. The mock target is a python container on a
per-run internal bridge. Scope and port are enforced by the direct policy.
The same original is consumed by the worker follow-up and the real Planner.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid

import pytest

from redteam.app.central_run import open_central_run
from redteam.attack_graph.identity import host_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.capability import shell_preparation
from redteam.capability.direct_target import DirectTargetConfig, prepare_target_network
from redteam.capability.shell_manifest import SHELL_NAMESPACE
from redteam.capability.shell_runtime import prepare_shell_runtime
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.contracts.execution import ExecutionState
from redteam.core.enums import ApprovalStatus, EvidenceSourceType, RiskLevel
from redteam.core.ids import GraphNodeId, RunId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_development import call, turn
from tests.integration.test_central_parallel import limits
from tests.integration.test_direct_target_route_docker import (
    ALLOWED_SCRIPT,
    DOCKER_CONTEXT,
    KALI_IMAGE,
    LABEL_KEY,
    MOCK_IMAGE,
    PROOF_ENV,
    RECEIPT_DIR,
    TARGET_PORT,
    _attached_networks,
    _docker_text,
    _owned_ids,
    _receipt,
    _wait_for_mock,
)
from tests.integration.test_gated_window_docker import NETWORK_FIXTURE
from tests.integration.test_probe_workers import PROFILE, drive

TARGET_TOOL = SHELL_NAMESPACE + ".shell_target_run"
PROBE_KEY = "program-observation"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get(PROOF_ENV) != "1", reason="explicit REDTEAM_DIRECT_TARGET_PROOF=1 required"
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable"),
]


def _count(text: str, needle: str) -> int:
    return text.count(needle)


class DirectTargetProbe(FakeLLMClient):
    """Scripted worker model: select the direct target tool once, then settle."""

    def __init__(self, *, target_host: str, script: str):
        super().__init__()
        self.envelopes: list[dict] = []
        self.planner_inputs: list[dict] = []
        self.target_host = target_host
        self.script = script

    async def complete_native(self, messages, tools, system=None, **kwargs):
        # Snapshot at call time: the native loop appends tool results to the same list later.
        self.envelopes.append(
            {"messages": copy.deepcopy(messages), "tools": tools, "system": system}
        )
        payload = json.loads(messages[-1]["content"])
        originals = payload["probe"]["original_evidence_ids"]
        if originals:
            response = turn(
                call(
                    "return",
                    "task.settle",
                    {
                        "outcome": "inconclusive",
                        "reason": "Direct target run finished; preserve observation",
                        "evidence_ids": originals,
                    },
                )
            )
        elif exposed_tool_name(TARGET_TOOL) in {spec.name for spec in tools}:
            response = turn(
                call(
                    "program",
                    TARGET_TOOL,
                    {
                        "language": "python3",
                        "script": self.script,
                        "target_ref": self.target_host,
                        "port": TARGET_PORT,
                    },
                )
            )
        else:
            response = turn(call("detail", "tool.get_detail", {"tool_id": TARGET_TOOL}))
        self.native_responses = [response]
        return await super().complete_native(messages, tools, system=system, **kwargs)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        if "updates" in response_schema["properties"]:
            self.planner_inputs.append(json.loads(messages[-1]["content"]))
            self._canned_structured = {
                "reason": "The owned target observation needs independent corroboration",
                "termination": "held",
                "resume_conditions": ["New authorized evidence arrives"],
            }
        return await super().complete_structured(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **kwargs,
        )


async def _register(
    session, key: str, target_node: GraphNodeId, *, actions: int = 1, requests: int = 8
) -> str:
    """Register one probe on the owned mock node using the public admission API."""
    state = session.deps.central_state
    service = AttackGraphService(session.deps.graph_store)
    mission = await state.get_mission(session.runtime.mission_id)
    read = {str(target_node): content_hash(await service.get_node(target_node))}
    proposal = HypothesisProbeProposal.model_validate(
        {
            "updates": [],
            "tasks": [
                {
                    "task_key": key,
                    "title": "Direct target probe",
                    "objective": "Run one scripted probe on the owned target",
                    "role_id": "probe-worker",
                    "target_node_ids": [target_node],
                    "completion_criteria": [
                        {"kind": "observation", "description": "Inspect the target response"}
                    ],
                    "hypothesis_revisions": {},
                    "probe_limits": {"max_actions": actions, "max_model_requests": requests},
                }
            ],
        }
    )
    receipt = await register_probe_tasks(
        session.deps,
        proposal_id=key,
        proposal=proposal,
        expected_revision=mission.revision,
        expected_binding_hash=await state.transaction(lambda tx: tx.binding()[1], write=False),
        graph_read=read,
        known_targets={target_node},
        known_principals=set(),
        max_tasks=4,
    )
    return receipt["task_ids"][0]


async def _remove_owned(runtime, tag: str) -> None:
    if runtime is not None:
        source = runtime.adapter.program_source
        for key in list(source._known):
            await source.remove_workroom(key)
    for cid in await _owned_ids("container", tag):
        await _docker_text("rm", "-f", cid)
    for nid in await _owned_ids("network", tag):
        await _docker_text("network", "rm", nid)
    assert await _owned_ids("container", tag) == []
    assert await _owned_ids("network", tag) == []


async def test_direct_control_chain_approval_resume_one_execution(tmp_path, monkeypatch):
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        pytest.skip("choose a named DOCKER_CONTEXT for this proof")
    code, _, _ = await _docker_text("image", "inspect", "--format", "{{.Id}}", MOCK_IMAGE)
    if code != 0:
        pytest.skip("mock image not present locally: " + MOCK_IMAGE)
    code, image_id, _ = await _docker_text("image", "inspect", "--format", "{{.Id}}", KALI_IMAGE)
    if code != 0 or image_id != KALI_IMAGE:
        pytest.skip("pinned Kali image ID not present locally")

    cache = tmp_path / "prep-cache"
    monkeypatch.setattr(shell_preparation, "_cache_directory", lambda: cache)
    policy_path = tmp_path / "shell-policy.json"
    policy_path.write_text(json.dumps({"image": KALI_IMAGE, "docker_context": DOCKER_CONTEXT}))

    tag = uuid.uuid4().hex[:12]
    label = f"{LABEL_KEY}={tag}"
    network_name = "control-chain-net-" + tag
    mock_name = "control-chain-mock-" + tag
    run_id = RunId("control_chain_" + tag)
    runtime = None
    try:
        runtime = await prepare_shell_runtime(str(policy_path), docker_context=DOCKER_CONTEXT)
        assert runtime is not None and runtime.environment_fingerprint

        code, network_id, err = await _docker_text(
            "network", "create", "--driver", "bridge", "--internal", "--label", label, network_name
        )
        assert code == 0, err[:300]
        code, _, err = await _docker_text(
            "run",
            "-d",
            "--name",
            mock_name,
            "--network",
            network_name,
            "--label",
            label,
            "--cap-drop",
            "ALL",
            MOCK_IMAGE,
            "sleep",
            "infinity",
        )
        assert code == 0, err[:300]
        code, _, err = await _docker_text("cp", str(NETWORK_FIXTURE), mock_name + ":/tmp/mock.py")
        assert code == 0, err[:300]
        code, _, err = await _docker_text(
            "exec",
            "-d",
            mock_name,
            "python3",
            "/tmp/mock.py",
            "serve",
            "--bind",
            "0.0.0.0",
            "--tcp-port",
            str(TARGET_PORT),
            "--udp-port",
            "18081",
            "--dns-port",
            "18053",
            "--log-dir",
            RECEIPT_DIR,
        )
        assert code == 0, err[:300]
        await _wait_for_mock(mock_name)
        code, mock_ip, err = await _docker_text(
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            mock_name,
        )
        assert code == 0 and mock_ip, err[:300]
        assert _count(await _receipt(mock_name, "mock-tcp.log"), "ACCEPT") == 0

        network = DirectTargetConfig(
            targets={mock_ip: (mock_ip,)},
            target_ports={mock_ip: (TARGET_PORT,)},
            target_network_id=network_id,
        )
        prepared = await prepare_target_network(
            runtime.policy, network, shell_fingerprint=runtime.environment_fingerprint
        )

        original, settings = limits()
        scope = original.scope.model_copy(
            update={
                # The IP seeds the owned host node; the endpoint retains the port scope.
                "allowed_hosts": [
                    *original.scope.allowed_hosts,
                    mock_ip,
                    f"{mock_ip}:{TARGET_PORT}",
                ],
            }
        )
        original = original.model_copy(
            update={
                "scope": scope,
                # The MEDIUM direct descriptor now needs a human decision. Control is unchanged.
                "approval_policy": ApprovalPolicy(risk_threshold=RiskLevel.MEDIUM),
            }
        )
        config = AppConfig(
            engagement=EngagementConfig.model_validate(
                original.model_dump(exclude={"engagement_id", "created_at"}),
            )
        )
        settings = settings.with_overrides(specialist_profile_path=str(PROFILE))
        llm = DirectTargetProbe(
            target_host=mock_ip,
            script=ALLOWED_SCRIPT.format(host=mock_ip, port=TARGET_PORT),
        )

        async with open_central_run(
            config,
            directory=tmp_path / "run",
            settings=settings,
            run_id=run_id,
            llm=llm,
            builtin_adapters=[runtime.adapter],
            builtin_output_limits=runtime.output_limits,
            direct_target_shell=(runtime, prepared.provider),
        ) as session:
            # The probe's target is the owned mock host, not the unit fixture node.
            # Keep one action available so the worker can inspect/settle the original
            # instead of being auto-settled at its execution budget boundary.
            identity = await _register(session, PROBE_KEY, host_node_id(mock_ip), actions=2)
            (outcome,) = await drive(session)

            # Pending approval: the model chose the call, but no packet has reached the mock.
            pending = outcome.runtime.pending_approval_id
            assert pending is not None
            assert _count(await _receipt(mock_name, "mock-tcp.log"), "ACCEPT") == 0
            (execution,) = await session.deps.central_state.list_executions()

            await session.deps.control_plane.resolve_approval(
                pending,
                ApprovalStatus.APPROVED,
                "fixture:operator",
            )
            recovered, recovery = await session.driver.recover_central_instance(outcome.runtime)
            assert recovery.ready
            await session.driver.runner_for(recovered).resume_recovered(recovered)

            receipt = await _receipt(mock_name, "mock-tcp.log")
            assert _count(receipt, "ACCEPT") == 1 and "DATA bytes=12" in receipt
            task = await session.deps.central_state.get_task(identity)
            assert task.result.outcome == "inconclusive"
            assert task.status.value == "completed"
            assert len(await session.deps.central_state.list_executions()) == 1

            resumed = await session.deps.central_state.get_execution(str(execution.invocation_id))
            assert resumed.state is ExecutionState.SUCCEEDED
            assert not resumed.wait_reason

            records = await session.deps.evidence_store.list_for_run(session.runtime.run_id)
            (raw,) = [item for item in records if item.source_type is EvidenceSourceType.TOOL]
            assert str(raw.invocation_id) == str(execution.invocation_id)
            output = raw.metadata["tool_result"]["output"]
            assert output["exit_code"] == 0 and "DIRECT-OK" in output["stdout"]
            assert raw.metadata["tool_result"]["metadata"]["direct_target_network"] == (
                "attached_for_execution"
            )
            # Worker follow-up received the original evidence, and the task result kept it.
            assert raw.evidence_id in task.result.evidence_ids
            # Snapshots taken at call time: the settle request carried the original evidence id.
            consumed = [
                env
                for env in llm.envelopes
                if str(raw.evidence_id)
                in json.loads(env["messages"][-1]["content"])["probe"]["original_evidence_ids"]
            ]
            assert consumed, {
                "raw": str(raw.evidence_id),
                "originals": [
                    json.loads(env["messages"][-1]["content"])["probe"] for env in llm.envelopes
                ],
                "envelope_count": len(llm.envelopes),
            }
            (room,) = list(runtime.adapter.program_source._known.values())
            assert await _attached_networks(room) == {}, "workroom must detach after the call"
            await SpecialistPlanner(session.deps).advance(session.runtime)
            planner = llm.planner_inputs[-1]
            (observation,) = planner["original_observations"]["records"]
            assert observation["provenance"]["invocation_id"] == str(raw.invocation_id)
            assert observation["execution"]["accounting"]["exit_code"] == 0
            assert "DIRECT-OK" in observation["program_output"]["stdout"]
    finally:
        await _remove_owned(runtime, tag)
