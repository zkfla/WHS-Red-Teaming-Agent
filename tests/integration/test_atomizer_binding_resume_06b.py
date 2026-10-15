"""Plan7-06B atomizer profile binding and checkpoint resume regression.

Drives the production open_central_run resume path with the split v3 profile
(config/agents/native-probes.yaml) and its legacy single-stage counterpart
(native-probes-legacy.yaml). Every changed-identity fixture is a labelled copy
of the shipped agents tree under tmp_path that alters exactly one identity input:
profile content, profile version, atomizer prompt bytes, or legacy planner
prompt bytes. Models are scripted doubles; no target, provider, or approval is
contacted. Identity is checked by the production load_specialist_profile
fingerprint and the production state.bind_run binding hash.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.app.central_run import open_central_run
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.completion import CompletionCriterion, criterion_id
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import TARGET, gateway
from tests.integration.test_central_parallel import limits
from tests.integration.test_hypothesis_planner import probe as legacy_probe

SHIPPED_AGENTS = Path(__file__).resolve().parents[2] / "config" / "agents"
SPLIT_PROFILE = "native-probes.yaml"
LEGACY_PROFILE = "native-probes-legacy.yaml"
REFUSAL = "run_binding_changed_new_run_required"


def _obs(description):
    return criterion_id(CompletionCriterion(kind="observation", description=description))


def _plan_two_questions():
    return {
        "updates": [],
        "questions": [
            {
                "question_id": "q1",
                "objective": "Inspect the first response",
                "target_node_ids": [TARGET],
                "completion_criteria": [{"kind": "observation", "description": "Inspect first"}],
            },
            {
                "question_id": "q2",
                "objective": "Inspect the second response",
                "target_node_ids": [TARGET],
                "completion_criteria": [{"kind": "observation", "description": "Inspect second"}],
            },
        ],
        "reason": "Two useful scoped observations",
        "termination": "continue",
    }


def _atom_two_tasks():
    return {
        "tasks": [
            {
                "task_key": "t1",
                "question_id": "q1",
                "objective": "Probe first",
                "title": "First probe",
                "role_id": "probe-worker",
                "target_node_ids": [TARGET],
                "completion_criterion_ids": [_obs("Inspect first")],
                "hypothesis_revisions": {},
                "probe_limits": {"max_actions": 1, "max_model_requests": 4},
            },
            {
                "task_key": "t2",
                "question_id": "q2",
                "objective": "Probe second",
                "title": "Second probe",
                "role_id": "probe-worker",
                "target_node_ids": [TARGET],
                "completion_criterion_ids": [_obs("Inspect second")],
                "hypothesis_revisions": {},
                "probe_limits": {"max_actions": 1, "max_model_requests": 4},
            },
        ]
    }


class ScriptedSplitDouble(FakeLLMClient):
    """Scripted stage-1 plan and stage-2 atomization; counts each model ask."""

    def __init__(self, *, plan, atoms):
        super().__init__()
        self.plan, self.atoms = plan, atoms
        self.plan_calls = 0
        self.atom_calls = 0

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        props = response_schema.get("properties", {})
        if "questions" in props:
            self.plan_calls += 1
            return self.plan
        if "tasks" in props:
            self.atom_calls += 1
            return self.atoms
        return await super().complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs
        )


def _fixture_profile(root, edit=None, *, profile=SPLIT_PROFILE):
    """Labelled fixture: a byte copy of the shipped agents tree, optionally edited once."""
    tree = root / "agents"
    shutil.copytree(SHIPPED_AGENTS, tree)
    if edit is not None:
        edit(tree)
    return tree / profile


def _replace_once(path, pattern: bytes, replacement):
    data = path.read_bytes()
    assert len(re.findall(pattern, data)) == 1, pattern
    path.write_bytes(re.sub(pattern, replacement, data))


def _edit_profile_content(tree):
    _replace_once(
        tree / SPLIT_PROFILE,
        rb"max_tasks_per_strategy: (\d+)",
        lambda m: b"max_tasks_per_strategy: " + str(int(m.group(1)) + 1).encode(),
    )


def _edit_profile_version(tree):
    _replace_once(
        tree / SPLIT_PROFILE,
        rb"(?m)^version: '([^']+)'",
        lambda m: b"version: '" + m.group(1) + b"-changed'",
    )


def _edit_atomizer_prompt(tree):
    path = tree / "prompts" / "probe-atomizer.md"
    path.write_bytes(
        path.read_bytes()
        + b"\n<!-- plan7-06b labelled fixture: changed atomizer prompt bytes -->\n"
    )


def _edit_legacy_planner_prompt(tree):
    path = tree / "prompts" / "hypothesis-planner.md"
    path.write_bytes(
        path.read_bytes()
        + b"\n<!-- plan7-06b labelled fixture: changed legacy planner prompt -->\n"
    )


def _run_config(profile):
    original, configured = limits()
    original.metadata["success_criteria"] = ["Assess the original response"]
    configured = configured.with_overrides(specialist_profile_path=str(profile))
    config = AppConfig(
        engagement=EngagementConfig.model_validate(
            original.model_dump(exclude={"engagement_id", "created_at"})
        )
    )
    return config, configured


def _options(llm):
    tools = OriginalProtocolDouble(gateway())
    return {"llm": llm, "tool_gateway": tools, "tool_catalog": tools.catalog}


def _tree(path):
    return sorted(str(item.relative_to(path)) for item in path.rglob("*")) if path.exists() else []


async def _snapshot(session):
    status = await session.status()
    return {"tasks": status["tasks"], "executions": status["executions"], "usage": status["usage"]}


async def test_split_same_identity_resume_keeps_one_registration(tmp_path):
    config, configured = _run_config(_fixture_profile(tmp_path / "copy-a"))
    llm = ScriptedSplitDouble(plan=_plan_two_questions(), atoms=_atom_two_tasks())
    root = tmp_path / "run"
    async with open_central_run(
        config, directory=root, settings=configured, **_options(llm)
    ) as session:
        assert len(await SpecialistPlanner(session.deps).advance(session.runtime)) == 2
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        baseline = await _snapshot(session)
    async with open_central_run(
        config,
        directory=root,
        settings=configured,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        **_options(llm),
    ) as session:
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert await _snapshot(session) == baseline
    assert (llm.plan_calls, llm.atom_calls) == (1, 1)


@pytest.mark.parametrize(
    "edit",
    [_edit_profile_content, _edit_profile_version, _edit_atomizer_prompt],
    ids=["profile_content", "profile_version", "atomizer_prompt_bytes"],
)
async def test_split_changed_identity_refused_before_any_mutation(tmp_path, edit):
    base_profile = _fixture_profile(tmp_path / "base")
    changed_profile = _fixture_profile(tmp_path / "changed", edit)
    assert (
        load_specialist_profile(changed_profile).fingerprint
        != load_specialist_profile(base_profile).fingerprint
    )
    config, base = _run_config(base_profile)
    _, changed = _run_config(changed_profile)
    llm = ScriptedSplitDouble(plan=_plan_two_questions(), atoms=_atom_two_tasks())
    root = tmp_path / "run"
    async with open_central_run(config, directory=root, settings=base, **_options(llm)) as session:
        assert len(await SpecialistPlanner(session.deps).advance(session.runtime)) == 2
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        baseline = await _snapshot(session)
    approvals = _tree(root / "approvals")
    with pytest.raises(StateConflict, match=REFUSAL):
        async with open_central_run(
            config,
            directory=root,
            settings=changed,
            resume=True,
            run_id=run_id,
            engagement_id=engagement_id,
            **_options(llm),
        ):
            pytest.fail("changed profile identity must not open the original Run")
    assert _tree(root / "approvals") == approvals
    assert (llm.plan_calls, llm.atom_calls) == (1, 1)
    async with open_central_run(
        config,
        directory=root,
        settings=base,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        **_options(llm),
    ) as session:
        assert await _snapshot(session) == baseline
        assert len(baseline["tasks"]) == 2
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
    assert (llm.plan_calls, llm.atom_calls) == (1, 1)


async def test_legacy_single_stage_same_identity_resumes_and_changed_prompt_refused(tmp_path):
    base_profile = _fixture_profile(tmp_path / "legacy-base", profile=LEGACY_PROFILE)
    changed_profile = _fixture_profile(
        tmp_path / "legacy-changed", _edit_legacy_planner_prompt, profile=LEGACY_PROFILE
    )
    assert (
        load_specialist_profile(changed_profile).fingerprint
        != load_specialist_profile(base_profile).fingerprint
    )
    config, base = _run_config(base_profile)
    _, changed = _run_config(changed_profile)
    llm = FakeLLMClient(canned_structured={"tasks": [legacy_probe()], "reason": "No candidate yet"})
    root = tmp_path / "run"
    async with open_central_run(config, directory=root, settings=base, **_options(llm)) as session:
        assert session.deps.specialist_policy.probe_atomization is False
        assert len(await SpecialistPlanner(session.deps).advance(session.runtime)) == 1
        run_id, engagement_id = session.runtime.run_id, session.runtime.engagement_id
        baseline = await _snapshot(session)
    with pytest.raises(StateConflict, match=REFUSAL):
        async with open_central_run(
            config,
            directory=root,
            settings=changed,
            resume=True,
            run_id=run_id,
            engagement_id=engagement_id,
            **_options(llm),
        ):
            pytest.fail("changed legacy planner prompt must not open the original Run")
    async with open_central_run(
        config,
        directory=root,
        settings=base,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        **_options(llm),
    ) as session:
        assert session.deps.specialist_policy.probe_atomization is False
        assert await _snapshot(session) == baseline
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        assert len(await session.deps.central_state.list_tasks()) == 1
