"""Plan 4e module-boundary contracts: stages, schemas, registry, fixture factory.

No central state, Docker, target, or model calls. Handler flows against a
live central store live in tests/integration/test_plan4e_code_artifacts.py.
"""

import json

import pytest

from redteam.capability.code_fixtures import build_code_fixture_runner
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.orchestration import code_artifacts
from redteam.orchestration.code_artifacts import (
    CodeProgram,
    CodeReview,
    InspectInput,
    PrepareInput,
    register_code_artifacts,
    validate_stages,
)
from redteam.orchestration.cognition import DecisionValidationError
from redteam.orchestration.local_tools import LocalToolRegistry

IMAGE = "sha256:" + "a" * 64

SOURCE = "def evaluate(value):\n    return {\"observed\": bool(value.get(\"n\", 0))}"
CASES = [{"name": "one", "input": {"n": 1}, "expected": {"observed": True}}]


def program(**overrides):
    material = {"language": "python", "source": SOURCE, "cases": list(CASES)}
    material.update(overrides)
    return CodeProgram.model_validate(material)


def test_stage_pair_selection_uses_registered_ids_not_fixed_roles():
    assert validate_stages({}) is False
    assert validate_stages({"code_generation": "writer-a", "code_review": "reviewer-b"}) is True
    with pytest.raises(ValueError, match="code_independent_stage_bindings_required"):
        validate_stages({"code_generation": "only-writer"})
    with pytest.raises(ValueError, match="code_independent_stage_bindings_required"):
        validate_stages({"code_review": "only-reviewer"})
    with pytest.raises(ValueError, match="code_independent_stage_bindings_required"):
        validate_stages({"code_generation": "same", "code_review": "same"})


def test_prepare_and_inspect_input_bounds():
    PrepareInput(objective="why", source_evidence_ids=["e1"])
    with pytest.raises(ValueError):
        PrepareInput(objective="", source_evidence_ids=["e1"])
    with pytest.raises(ValueError):
        PrepareInput(objective="why", source_evidence_ids=[])
    with pytest.raises(ValueError):
        PrepareInput(objective="why", source_evidence_ids=[f"e{i}" for i in range(9)])
    InspectInput(artifact_evidence_id="code_artifact_x")
    with pytest.raises(ValueError):
        InspectInput(artifact_evidence_id="")


def test_program_schema_bounds():
    program()
    with pytest.raises(ValueError):
        program(source="")
    with pytest.raises(ValueError):
        program(language="ruby")
    with pytest.raises(ValueError):
        program(cases=[])
    with pytest.raises(ValueError):
        program(cases=[{"name": f"c{i}", "input": {}, "expected": {}} for i in range(17)])
    with pytest.raises(ValueError):
        program(source="#" * 32769)


def test_program_rejects_bad_syntax_secrets_and_oversize_json():
    with pytest.raises((ValueError, SyntaxError)):
        code_artifacts._program({"language": "python", "source": "def broken(:",
                                 "cases": CASES})
    leaked = "def evaluate(value):\n    password = \"hunter2\"\n    return {\"observed\": True}"
    with pytest.raises(ValueError, match="code_secret_material_detected"):
        code_artifacts._program({"language": "python", "source": leaked, "cases": CASES})
    wide = {"language": "python", "source": "def evaluate(value):\n    return value",
            "cases": [{"name": "wide", "input": {}, "expected": {"blob": "y" * 70000}}]}
    with pytest.raises(ValueError, match="code_program_oversized"):
        code_artifacts._program(wide)
    assert code_artifacts._program(program().model_dump(mode="json")).source == SOURCE


def test_review_digest_and_bundle_markers():
    CodeReview(accepted=True, artifact_digest="d" * 64, reason="relevant")
    with pytest.raises(ValueError):
        CodeReview(accepted=True, artifact_digest="short", reason="relevant")
    with pytest.raises(ValueError):
        CodeReview(accepted="yes", artifact_digest="d" * 64, reason="relevant")
    with pytest.raises(ValueError):
        CodeReview(accepted=True, artifact_digest="d" * 64, reason="")
    rejected = code_artifacts._rejection("code_review_digest_mismatch", "code_review", "Echo it.")
    assert isinstance(rejected, DecisionValidationError)
    assert rejected.diagnostic.code == "code_review_digest_mismatch"
    assert rejected.diagnostic.stage == "code_review"
    assert rejected.diagnostic.retryable is True


async def test_registered_helpers_are_discoverable_without_central_branches():
    registry = LocalToolRegistry()
    register_code_artifacts(registry)
    assert sorted(descriptor.tool_id for descriptor in registry.descriptors()) == [
        "code.inspect", "code.prepare", "code.validate",
    ]
    for descriptor in registry.descriptors():
        assert descriptor.contribution_id.startswith("code-artifacts:")
        assert isinstance(descriptor.input_schema, dict)
        assert "target" not in json.dumps(descriptor.input_schema).lower()
        assert "shell" not in json.dumps(descriptor.input_schema).lower()
    with pytest.raises(ValueError, match="already registered"):
        register_code_artifacts(registry)


async def test_registry_dispatch_rejects_unknown_tool_without_a_handler():
    registry = LocalToolRegistry()
    register_code_artifacts(registry)
    result = await registry.call("code.missing", {}, None)
    assert result.ok is False and result.error == "unknown local tool"


def test_fixture_factory_selection_contract(tmp_path):
    assert build_code_fixture_runner(None) is None
    with pytest.raises(ValueError, match="fixture_operator_policy_invalid"):
        build_code_fixture_runner(str(tmp_path / "absent.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="fixture_operator_policy_invalid"):
        build_code_fixture_runner(str(bad))
    loose = tmp_path / "loose.json"
    loose.write_text(ShellWorkroomPolicy(
        image="tag", allow_unpinned_image=True).model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="fixture_operator_policy_invalid"):
        build_code_fixture_runner(str(loose))
    policy = tmp_path / "policy.json"
    policy.write_text(ShellWorkroomPolicy(image=IMAGE).model_dump_json(), encoding="utf-8")
    runner = build_code_fixture_runner(str(policy))
    assert runner is not None and len(runner.fingerprint) == 64
