"""Source/claim scope and immutable code bindings; model and runner fixtures are explicit."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.enums import AgentPhase, EvidenceSourceType
from redteam.core.ports.fixture_runner import FixturePolicy, FixtureResult
from redteam.evidence.artifacts.filesystem import FilesystemArtifactBackend
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.code_artifacts import prepare
from redteam.orchestration.code_artifacts import test_artifact as execute_artifact
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.completion import CompletionIssues, validate_result_claims
from redteam.orchestration.evidence_validity import (
    assess,
    current_validity,
)
from redteam.orchestration.local_tools import LocalToolContext, LocalToolRegistry
from redteam.orchestration.paths import CompletionCriterion, InMemoryPathStore, PathPlan
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.work.memory import InMemoryWorkStore
from tests.integration import _engine_llm_harness as h

PROFILE = "config/agents/specialists-p2.yaml"


class ArtifactLLM:
    def __init__(self, *, accept=True, wrong_digest=False, hook=None):
        self.calls = []
        self.accept = accept
        self.wrong_digest = wrong_digest
        self.hook = hook

    async def complete_structured(self, *, messages, response_schema, system, **kwargs):
        self.calls.append(system)
        payload = json.loads(messages[-1]["content"])
        if "source" in response_schema["properties"]:
            return {"schema_version": "1", "source": "def evaluate(value):\n    return value + 1\n",
                    "cases": [{"input": 1, "expected": 2}]}
        if self.hook:
            await self.hook()
        return {"accepted": self.accept, "reason": "Independent fixture review only",
                "artifact_digest": "0" * 64 if self.wrong_digest else payload["artifact_digest"]}


class FixtureRunner:
    def __init__(self, policy):
        self.policy = policy
        self.calls = 0

    async def run(self, program):
        self.calls += 1
        return FixtureResult(status="passed", total=len(program.cases), passed=len(program.cases))


async def world(tmp_path, llm=None):
    seeded = await h.seed_host("run-p2-local")
    store = FilesystemEvidenceStore(tmp_path / "evidence")
    runtime = h.make_runtime("run-p2-local", str(seeded.engagement.engagement_id))
    loaded = load_specialist_profile(PROFILE)
    original = h.make_tool_evidence(
        evidence_id="original", run_id=str(runtime.run_id),
        engagement_id=str(runtime.engagement_id), action_id="action", invocation_id="invocation",
        path_id="path", path_version=1, step_id="step", target_node_id=seeded.node_id,
        output={"app_version": "v1", "session_active": True},
    )
    await store.append(original)
    node = await seeded.service.get_node(seeded.node_id)
    await seeded.service.upsert_node(
        node.type, node_id=node.id, facts={"app_version": "v1", "session_active": True}, step=1,
        provenance=Provenance(step=1, source="local:graph.record", evidence_id="original",
                              run_id=str(runtime.run_id), confidence=Confidence.MEASURED),
    )
    runner = FixtureRunner(loaded.profile.fixture_policy)
    deps = SimpleNamespace(
        evidence_store=store, graph_store=seeded.store, agent_prompts=loaded.prompts,
        artifact_store=FilesystemArtifactBackend(tmp_path / "artifacts"), fixture_runner=runner,
        llm_client=llm or ArtifactLLM(),
    )
    context = LocalToolContext(deps=deps, runtime=runtime, purpose="Inspect measured behavior",
                               budget=DecisionBudget.reasoning_chain(8, 2))
    return seeded, store, original, runner, context


async def bind(context, node_id, *, reference="original", key="app_version"):
    return await assess({"evidence_id": reference,
                         "conditions": [{"node_id": node_id, "container": "fact", "key": key}]},
                        context)


async def change_fact(seeded, store, context, *, key="app_version", value="v2"):
    # A new original observation, not a rewrite of the old source.
    record = h.make_tool_evidence(
        evidence_id="later-" + key, run_id=str(context.runtime.run_id),
        engagement_id=str(context.runtime.engagement_id), action_id="later-action",
        invocation_id="later-inv", path_id="path", path_version=1, step_id="step",
        target_node_id=seeded.node_id, output={key: value},
    )
    await store.append(record)
    node = await seeded.service.get_node(seeded.node_id)
    await seeded.service.upsert_node(
        node.type, node_id=node.id, facts={key: value}, step=2,
        provenance=Provenance(step=2, source="local:graph.record",
                              evidence_id=str(record.evidence_id),
                              run_id=str(context.runtime.run_id), confidence=Confidence.MEASURED),
    )


@pytest.mark.parametrize("key,value", [("app_version", "v2"), ("session_active", False)])
async def test_related_conditions_stale_without_rewriting_original_and_resume(tmp_path, key, value):
    seeded, store, original, _, context = await world(tmp_path)
    assert (await bind(context, seeded.node_id, key=key)).payload["status"] == "valid"
    count = len(await store.list_for_run(context.runtime.run_id))
    assert (await bind(context, seeded.node_id, key=key)).ok
    assert len(await store.list_for_run(context.runtime.run_id)) == count
    await change_fact(seeded, store, context, key=key, value=value)
    restored = FilesystemEvidenceStore(tmp_path / "evidence")
    projected = await current_validity(
        restored, seeded.service, str(context.runtime.run_id), "original",
    )
    assert projected["status"] == "stale"
    assert await restored.get("original") == original
    assert (await current_validity(restored, seeded.service, str(context.runtime.run_id),
                                   "later-" + key))["status"] == "unbound"


async def test_unrelated_update_does_not_stale_and_no_guessed_expiry(tmp_path):
    seeded, store, _, _, context = await world(tmp_path)
    assert (await bind(context, seeded.node_id)).ok
    await change_fact(seeded, store, context, key="other_fact", value="independent")
    assert (await current_validity(store, seeded.service, str(context.runtime.run_id),
                                   "original"))["status"] == "valid"


async def test_only_grounded_expiry_and_known_timezone_are_used(tmp_path, monkeypatch):
    import redteam.orchestration.evidence_validity as validity

    seeded, store, _, _, context = await world(tmp_path)
    now = datetime.now(timezone.utc)
    await change_fact(
        seeded, store, context, key="expiry_at", value=(now + timedelta(hours=1)).isoformat(),
    )
    request = {"evidence_id": "original", "conditions": [{
        "node_id": seeded.node_id, "container": "fact", "key": "expiry_at", "test": "not_expired",
    }]}
    assert (await assess(request, context)).payload["status"] == "valid"

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(hours=2)

    monkeypatch.setattr(validity, "datetime", FutureClock)
    assert (await current_validity(store, seeded.service, str(context.runtime.run_id),
                                   "original"))["status"] == "stale"
    assert (await current_validity(store, seeded.service, str(context.runtime.run_id),
                                   "later-expiry_at"))["status"] == "unbound"


async def test_store_read_failure_is_inconclusive_not_valid(tmp_path):
    seeded, store, _, _, context = await world(tmp_path)
    assert (await bind(context, seeded.node_id)).ok

    class BrokenStore:
        async def list_for_run(self, run):
            raise OSError("private-value-never-returned")

    result = await current_validity(BrokenStore(), seeded.service,
                                    str(context.runtime.run_id), "original")
    assert result["status"] == "inconclusive"
    assert "private" not in json.dumps(result)


async def test_bound_receipt_does_not_invalidate_an_unrelated_claim_on_same_original(tmp_path):
    from redteam.core.enums import Verdict
    from redteam.orchestration.analysis import VerificationAssessment, append_posthoc_claim_receipt
    from redteam.orchestration.completion import _load_final_receipts

    seeded, store, _, _, context = await world(tmp_path)
    receipts = []
    for statement in ("Session-sensitive claim", "Historical public observation"):
        receipt = await append_posthoc_claim_receipt(
            evidence_store=store, run_id=str(context.runtime.run_id),
            engagement_id=str(context.runtime.engagement_id),
            evidence_ids=["original"], target_node_id=seeded.node_id,
            action_id="action", invocation_id="invocation",
            claim={"statement": statement},
            assessment=VerificationAssessment(
                verdict=Verdict.PASS, reason="Independent fixture review",
                reviewer_id="test-reviewer",
                source_refs=["original"], outcome="success", claim_supported=True,
            ),
        )
        receipts.append(receipt)
    assert (await bind(context, seeded.node_id, reference=str(receipts[0].evidence_id),
                       key="session_active")).ok
    await change_fact(seeded, store, context, key="session_active", value=False)
    usable = await _load_final_receipts(store, str(context.runtime.run_id), seeded.service)
    assert str(receipts[0].evidence_id) not in {r["receipt_id"] for r in usable}
    assert str(receipts[1].evidence_id) in {r["receipt_id"] for r in usable}
    assert (await current_validity(store, seeded.service, str(context.runtime.run_id),
                                   "original"))["status"] == "unbound"


async def test_unknown_conditions_and_cross_run_or_failed_source_rejected(tmp_path):
    seeded, store, original, _, context = await world(tmp_path)
    assert not (await bind(context, seeded.node_id, key="unobserved_expiry")).ok
    assert not (await assess({"evidence_id": "not-known"}, context)).ok
    raw = original.metadata["tool_result"] | {"success": False}
    failure = original.model_copy(update={"evidence_id": "failed",
                                          "metadata": original.metadata | {"tool_result": raw}})
    await store.append(failure)
    assert not (await assess({"evidence_id": "failed"}, context)).ok
    changed_context = replace(
        context, runtime=context.runtime.model_copy(update={"run_id": "other"}),
    )
    assert not (await assess({"evidence_id": "original"}, changed_context)).ok


async def test_completion_and_report_consume_current_validity(tmp_path):
    seeded, store, _, _, context = await world(tmp_path)
    assert (await bind(context, seeded.node_id)).ok
    await change_fact(seeded, store, context)
    issues = CompletionIssues()
    await validate_result_claims(
        criteria=[CompletionCriterion(kind="observation", description="Measured behavior")],
        evidence_ids=["original"], target_node_ids=[seeded.node_id], principal_refs=[],
        evidence_store=store, graph_service=seeded.service, run_id=str(context.runtime.run_id),
        path_store=None, mission_id=None, issues=issues,
    )
    assert "completion_evidence_current_validity_stale" in str(issues.diagnostics)
    report = await ReportingAgent().project(
        context.runtime.model_copy(update={"phase": AgentPhase.FINISHED}), graph=seeded.service,
        evidence_store=store, work_store=InMemoryWorkStore(),
    )
    assert report.evidence_validity[0]["status"] == "stale"
    from redteam.app.cli.commands.report import _render_markdown

    assert "original: stale" in _render_markdown(report)


@pytest.mark.parametrize("successful", [True, False])
async def test_unbound_meaning_does_not_replace_original_result_validation(tmp_path, successful):
    seeded, store, original, _, context = await world(tmp_path)
    raw = original.metadata["tool_result"] | {"success": successful}
    source = original.model_copy(update={
        "evidence_id": "unbound-source",
        "metadata": original.metadata | {"tool_result": raw},
    })
    await store.append(source)
    projected = await current_validity(
        store, seeded.service, str(context.runtime.run_id), "unbound-source",
    )
    assert projected["status"] == "unbound"
    assert "not a failed, expired, verified or successful source verdict" in projected["meaning"]
    draft = h.draft_path(seeded.node_id, kind="observation")
    draft["steps"][0]["step_id"] = "step"
    path = PathPlan.model_validate({
        **draft, "run_id": str(context.runtime.run_id), "path_id": "path", "version": 1,
        "status": "active", "goal": "Observe the measured behavior",
    })
    paths = InMemoryPathStore()
    await paths.register(path)
    issues = CompletionIssues()
    await validate_result_claims(
        criteria=[CompletionCriterion(kind="observation", description="Measured behavior")],
        evidence_ids=["unbound-source"], target_node_ids=[seeded.node_id], principal_refs=[],
        evidence_store=store, graph_service=seeded.service, run_id=str(context.runtime.run_id),
        path_store=paths, mission_id=None, issues=issues,
    )
    assert ("failed_original" in str(issues.diagnostics)) is not successful
    assert bool(issues) is not successful
    assert not any("current_validity" in d.code for d in issues.diagnostics)


async def test_prepare_review_fixture_and_refuse_mock_as_target_proof(tmp_path):
    seeded, store, _, runner, context = await world(tmp_path)
    prepared = await prepare({"objective": "Inspect measured behavior",
                              "source_evidence_ids": ["original"]}, context)
    assert prepared.ok, prepared.error
    assert context.budget.used == 2
    assert "Exploit" in context.deps.llm_client.calls[0] or (
        context.deps.llm_client.calls[0] != context.deps.llm_client.calls[1]
    )
    tested = await execute_artifact(prepared.payload | {}, context)
    # Exact tool schema rejects model-added convenience fields.
    assert not tested.ok
    tested = await execute_artifact({"artifact_evidence_id": prepared.refs[0]}, context)
    assert tested.ok and tested.payload["fixture_only"] is True
    assert runner.calls == 1
    assert not (await assess({"evidence_id": tested.refs[0]}, context)).ok
    assert await seeded.service.list_findings() == []
    assert (await store.get(prepared.refs[0])).artifact_ref


@pytest.mark.parametrize("change", [
    "purpose", "policy", "source", "blob", "reviewer", "tool_spoof",
])
async def test_approved_artifact_rejects_changed_bindings_without_running(tmp_path, change):
    seeded, store, _, runner, context = await world(tmp_path)
    result = await prepare({"objective": "Inspect behavior", "source_evidence_ids": ["original"]},
                           context)
    assert result.ok, result.error
    if change == "purpose":
        context = replace(context, purpose="Different task")
    elif change == "policy":
        runner.policy = runner.policy.model_copy(update={"timeout_seconds": 2})
    elif change == "source":
        assert (await bind(context, seeded.node_id)).ok
        await change_fact(seeded, store, context)
    elif change == "blob":
        artifact = await store.get(result.refs[0])
        (tmp_path / "artifacts" / artifact.artifact_ref).write_bytes(b"changed")
    elif change == "tool_spoof":
        artifact = await store.get(result.refs[0])
        spoof = artifact.model_copy(update={"evidence_id": "tool-spoof",
                                            "source_type": EvidenceSourceType.TOOL,
                                            "source_id": "untrusted-tool"})
        await store.append(spoof)
        result = replace(result, refs=("tool-spoof",))
    else:
        class ChangedPrompts:
            def binding(self, stage):
                return load_specialist_profile(PROFILE).prompts.binding(stage).model_copy(
                    update={"sha256": "0" * 64},
                )
        context.deps.agent_prompts = ChangedPrompts()
    tested = await execute_artifact({"artifact_evidence_id": result.refs[0]}, context)
    assert not tested.ok
    assert runner.calls == 0


@pytest.mark.parametrize("accept,wrong_digest", [(False, False), (True, True)])
async def test_unaccepted_or_wrong_hash_review_never_creates_executable_artifact(
    tmp_path, accept, wrong_digest,
):
    _, store, _, runner, context = await world(
        tmp_path, ArtifactLLM(accept=accept, wrong_digest=wrong_digest),
    )
    result = await prepare({"objective": "Inspect behavior", "source_evidence_ids": ["original"]},
                           context)
    assert not result.ok
    assert not any(r.artifact_ref for r in await store.list_for_run(context.runtime.run_id))
    assert runner.calls == 0


async def test_code_requests_cannot_reset_or_multiply_caller_budget(tmp_path):
    _, _, _, _, context = await world(tmp_path)
    context = replace(context, budget=DecisionBudget.reasoning_chain(1, 0))
    result = await prepare({"objective": "Inspect behavior", "source_evidence_ids": ["original"]},
                           context)
    assert not result.ok
    assert context.budget.used == 1
    assert len(context.deps.llm_client.calls) == 1


def test_optional_profile_features_have_actual_registry_consumers():
    from redteam.app.runtime_components import RuntimeComponents

    loaded = load_specialist_profile(PROFILE)
    components = RuntimeComponents(agents=None, candidate_providers=None, verifier=None,
                                   result_mappers=None, local_tools=LocalToolRegistry())
    assembled = loaded.assemble(components)
    assert not components.local_tools.descriptors()
    assert {d.tool_id for d in assembled.local_tools.descriptors()} == {
        "code.prepare", "code.test", "evidence.assess",
    }
    plain = load_specialist_profile("config/agents/specialists-p1.yaml").assemble(components)
    assert not plain.local_tools.descriptors()


def test_fixture_policy_requires_pinned_image():
    with pytest.raises(ValueError):
        FixturePolicy(image="python:latest")


async def test_real_application_assembly_wires_selected_artifact_ports(tmp_path):
    from redteam.app.bootstrap import build_application
    from redteam.capability.fixture_runner import DockerFixtureRunner
    from redteam.core.config import AppConfig, EngagementConfig
    from tests.fakes import DummyToolGateway, FakeControlPlane

    seeded = await h.seed_host("run-p2-bootstrap")
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()])
    _, deps = await build_application(
        AppConfig(engagement=EngagementConfig.model_validate(seeded.engagement.model_dump()),
                  specialist_profile=PROFILE),
        llm=ArtifactLLM(), tool_gateway=gateway, tool_catalog=gateway,
        attack_graph=seeded.store, engagement_id=seeded.engagement.engagement_id,
        control_plane=FakeControlPlane(), evidence_dir=tmp_path / "evidence",
        audit_dir=tmp_path / "audit", approval_dir=tmp_path / "approvals",
    )
    assert isinstance(deps.artifact_store, FilesystemArtifactBackend)
    assert isinstance(deps.fixture_runner, DockerFixtureRunner)
    assert deps.local_tools.get("code.prepare") is not None
    assert deps.agent_prompts.binding("code_generation").role_id != (
        deps.agent_prompts.binding("code_review").role_id
    )


async def test_registered_pipeline_flows_through_generic_dispatch_and_usage(tmp_path):
    from redteam.orchestration.code_artifacts import register_code_artifacts
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.paths import PathPlan

    seeded, store, original, runner, context = await world(tmp_path)
    world_deps = h.make_world(context.deps.llm_client, graph_store=seeded.store).deps
    world_deps.evidence_store = store
    world_deps.artifact_store = context.deps.artifact_store
    world_deps.fixture_runner = runner
    world_deps.agent_prompts = context.deps.agent_prompts
    world_deps.local_tools = LocalToolRegistry()
    register_code_artifacts(world_deps.local_tools)
    engine = InvestigationEngine(world_deps)
    path = PathPlan.model_validate(h.draft_path(seeded.node_id) | {
        "run_id": str(context.runtime.run_id), "path_id": "path", "version": 1,
        "goal": context.purpose, "status": "active",
    })
    values = await engine.execute_local_calls(
        [{"tool_id": "code.prepare", "arguments": {
            "objective": "Inspect behavior", "source_evidence_ids": ["original"],
        }}], runtime=context.runtime, path=path, budget=context.budget,
    )
    assert values[0]["result"]["ok"], values
    identity = values[0]["result"]["refs"][0]
    assert context.runtime.model_requests_used == 2
    assert len(context.runtime.local_tool_result_ids) == 1
    restored_result = await engine.local_results(context.runtime)
    assert restored_result[0]["result"]["payload"]["artifact_evidence_id"] == identity
    values = await engine.execute_local_calls(
        [{"tool_id": "code.test", "arguments": {"artifact_evidence_id": identity}}],
        runtime=context.runtime, path=path, budget=context.budget,
    )
    assert values[0]["result"]["ok"], values
    assert context.runtime.actions_executed == 0
    assert context.runtime.model_requests_used == 2 and runner.calls == 1
    assert await store.get("original") == original
    assert await seeded.service.list_findings() == []


async def test_external_metadata_cannot_register_a_validity_binding(tmp_path):
    seeded, store, original, _, context = await world(tmp_path)
    from redteam.orchestration.evidence_validity import BINDING_KIND

    spoof = original.model_copy(update={"evidence_id": "external-spoof", "metadata": {
        "record_kind": BINDING_KIND, "claim_evidence_id": "original", "schema_version": "1",
    }})
    await store.append(spoof)
    result = await current_validity(store, seeded.service, str(context.runtime.run_id), "original")
    assert result["status"] == "unbound"


async def test_condition_value_uses_the_source_grounded_snapshot_without_second_read(
    tmp_path, monkeypatch,
):
    from redteam.orchestration.evidence_validity import ReuseCondition, condition_material

    seeded, store, _, _, context = await world(tmp_path)

    async def unproved_second_read(*args):
        raise AssertionError("Do not read a newer unproved value after validating the old snapshot")

    monkeypatch.setattr(seeded.service, "get_node", unproved_second_read)
    values = await condition_material(
        store, seeded.service, str(context.runtime.run_id),
        [ReuseCondition(node_id=seeded.node_id, container="fact", key="app_version")],
    )
    assert list(values) == [f"{seeded.node_id}#fact:app_version"]
