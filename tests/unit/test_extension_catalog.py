from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from redteam_core.extension import (
    BundleManifest,
    DomainBundle,
    ExperimentExecutorRegistration,
    FrozenRegistryError,
    PolicyConstraintEvaluatorRegistration,
    RelationTypeRegistration,
    RequirementTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
    ValidatorRegistration,
    compose_bundles,
    fingerprint_source_files,
    verify_source_fingerprint,
)
from redteam_core.graph.semantics import RelationSemantics
from tests.helpers import FP_A, FP_B, SamplePayload, reference_bundle


def test_catalog_freezes_all_registries_and_hash_is_order_independent():
    first = compose_bundles((reference_bundle(reverse_schema_order=False),))
    second = compose_bundles((reference_bundle(reverse_schema_order=True),))
    assert first.semantic_manifest_hash == second.semantic_manifest_hash
    assert first.frozen
    assert first.observation_payloads.frozen


def test_duplicate_registration_fails_closed():
    bundle = reference_bundle()
    duplicate = DomainBundle(
        manifest=bundle.manifest,
        schema_registrations=(bundle.schema_registrations[0], bundle.schema_registrations[0]),
    )
    with pytest.raises(ValueError, match="duplicate"):
        compose_bundles((duplicate,))


def test_registry_mutation_after_freeze_is_rejected():
    catalog = compose_bundles((reference_bundle(),))
    with pytest.raises(FrozenRegistryError):
        catalog.observation_payloads.register(reference_bundle().schema_registrations[0])


def test_requirement_cannot_reference_unregistered_payload():
    bundle = DomainBundle(
        manifest=BundleManifest(
            bundle_id="broken",
            namespace="broken",
            version="1",
            implementation_fingerprint=FP_A,
        ),
        requirement_registrations=(
            RequirementTypeRegistration(
                type_id="broken.requirement.value@1",
                schema_version="1",
                owner_bundle_id="broken",
                allowed_observation_payload_type_ids=frozenset({"broken.payload.value@1"}),
                implementation_fingerprint=FP_A,
            ),
        ),
    )
    with pytest.raises(ValueError, match="unregistered payload"):
        compose_bundles((bundle,))


def test_registration_must_stay_inside_bundle_namespace():
    bundle = DomainBundle(
        manifest=BundleManifest(
            bundle_id="owner",
            namespace="owner",
            version="1",
            implementation_fingerprint=FP_A,
        ),
        schema_registrations=(
            SchemaRegistration(
                category=SchemaCategory.OBSERVATION_PAYLOAD,
                type_id="other.payload.value@1",
                schema_version="1",
                owner_bundle_id="owner",
                model=SamplePayload,
                implementation_fingerprint=FP_A,
            ),
        ),
    )
    with pytest.raises(ValueError, match="outside bundle namespace"):
        compose_bundles((bundle,))


def test_bundle_dependency_cycle_is_rejected():
    left = DomainBundle(
        manifest=BundleManifest(
            bundle_id="left",
            namespace="left",
            version="1",
            implementation_fingerprint=FP_A,
            requires_bundle_ids=("right",),
        )
    )
    right = DomainBundle(
        manifest=BundleManifest(
            bundle_id="right",
            namespace="right",
            version="1",
            implementation_fingerprint=FP_B,
            requires_bundle_ids=("left",),
        )
    )
    with pytest.raises(ValueError, match="cycle"):
        compose_bundles((left, right))


def test_build_time_source_fingerprint_changes_with_source(tmp_path: Path):
    source = tmp_path / "semantic.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = fingerprint_source_files([source])
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = fingerprint_source_files([source])
    assert first != second


def test_build_time_source_fingerprint_guard_rejects_stale_expected_hash(tmp_path: Path):
    source = tmp_path / "semantic.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    expected = fingerprint_source_files([source])
    verify_source_fingerprint(expected, [source])
    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        verify_source_fingerprint(expected, [source])


def test_semantic_manifest_hash_binds_bundle_implementation_fingerprint():
    original = reference_bundle()
    changed = DomainBundle(
        manifest=original.manifest.model_copy(update={"implementation_fingerprint": FP_B}),
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_semantic_manifest_hash_binds_relation_semantics():
    original = reference_bundle()
    relation = original.relation_registrations[0]
    changed = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=(
            RelationTypeRegistration(
                type_id=relation.type_id,
                schema_version=relation.schema_version,
                owner_bundle_id=relation.owner_bundle_id,
                implementation_fingerprint=relation.implementation_fingerprint,
                semantics=RelationSemantics(
                    traversable=True,
                    source_tags=("reference-source",),
                ),
            ),
        ),
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_relation_semantics_are_immutable_after_registration():
    registration = reference_bundle().relation_registrations[0]
    with pytest.raises(ValidationError):
        registration.semantics.traversable = True


def test_semantic_manifest_hash_binds_candidate_deriver_implementation():
    original = reference_bundle()
    deriver = original.candidate_deriver_registrations[0]
    changed = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=(
            type(deriver)(
                deriver_id=deriver.deriver_id,
                owner_bundle_id=deriver.owner_bundle_id,
                candidate_type_ids=deriver.candidate_type_ids,
                implementation_fingerprint=FP_B,
            ),
        ),
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_semantic_manifest_hash_binds_validator_applicability():
    original = reference_bundle()
    validator = original.validator_registrations[0]
    changed = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=(
            ValidatorRegistration(
                validator_id=validator.validator_id,
                owner_bundle_id=validator.owner_bundle_id,
                implementation_fingerprint=validator.implementation_fingerprint,
                candidate_type_ids=frozenset({"reference.candidate.value@1"}),
                requires_experiment_result=True,
            ),
        ),
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_semantic_manifest_hash_binds_experiment_executor_implementation():
    original = reference_bundle()
    executor = original.experiment_executor_registrations[0]
    changed = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=(
            ExperimentExecutorRegistration(
                executor_id=executor.executor_id,
                owner_bundle_id=executor.owner_bundle_id,
                action_type_ids=executor.action_type_ids,
                implementation_fingerprint=FP_B,
                result_type_ids=executor.result_type_ids,
            ),
        ),
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_semantic_manifest_hash_binds_policy_constraint_evaluator_implementation():
    original = reference_bundle()
    evaluator = original.policy_constraint_evaluator_registrations[0]
    changed = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            PolicyConstraintEvaluatorRegistration(
                constraint_type_id=evaluator.constraint_type_id,
                evaluator_id=evaluator.evaluator_id,
                owner_bundle_id=evaluator.owner_bundle_id,
                implementation_fingerprint=FP_B,
            ),
        ),
    )
    assert compose_bundles((original,)).semantic_manifest_hash != compose_bundles(
        (changed,)
    ).semantic_manifest_hash


def test_policy_constraint_without_evaluator_is_rejected():
    original = reference_bundle()
    broken = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
    )
    with pytest.raises(ValueError, match="no registered evaluator"):
        compose_bundles((broken,))


def test_experiment_action_cannot_have_multiple_executors():
    original = reference_bundle()
    executor = original.experiment_executor_registrations[0]
    duplicate = ExperimentExecutorRegistration(
        executor_id="reference.executor.second@1",
        owner_bundle_id="reference",
        action_type_ids=executor.action_type_ids,
        implementation_fingerprint=FP_B,
        result_type_ids=executor.result_type_ids,
    )
    broken = DomainBundle(
        manifest=original.manifest,
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=(executor, duplicate),
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    with pytest.raises(ValueError, match="multiple executors"):
        compose_bundles((broken,))
