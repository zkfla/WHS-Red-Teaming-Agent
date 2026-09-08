from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from redteam_core.extension.registry import (
    FINGERPRINT_PATTERN,
    CandidateDeriverRegistration,
    DomainStateCodecRegistration,
    ExperimentExecutorRegistration,
    PolicyConstraintEvaluatorRegistration,
    RelationTypeRegistration,
    RequirementTypeRegistration,
    SchemaCategory,
    SchemaRegistration,
    TypedRegistry,
    ValidatorRegistration,
)
from redteam_core.models.base import SchemaModel, canonical_json, sha256_text

BUNDLE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*$")


class BundleKind(StrEnum):
    DOMAIN = "domain"
    INTEGRATION = "integration"
    REFERENCE = "reference"


class BundleManifest(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    bundle_id: str = Field(min_length=1, max_length=120)
    namespace: str = Field(min_length=1, max_length=60)
    version: str = Field(min_length=1, max_length=40)
    bundle_kind: BundleKind = BundleKind.DOMAIN
    implementation_fingerprint: str
    requires_bundle_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> BundleManifest:
        if not BUNDLE_ID_RE.fullmatch(self.bundle_id):
            raise ValueError("invalid bundle_id")
        if not re.fullmatch(r"^[a-z][a-z0-9_-]*$", self.namespace):
            raise ValueError("invalid bundle namespace")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("invalid bundle implementation fingerprint")
        if self.bundle_id in self.requires_bundle_ids:
            raise ValueError("bundle cannot require itself")
        if len(set(self.requires_bundle_ids)) != len(self.requires_bundle_ids):
            raise ValueError("duplicate bundle dependencies are not allowed")
        return self


@dataclass(frozen=True)
class DomainBundle:
    manifest: BundleManifest
    schema_registrations: tuple[SchemaRegistration, ...] = ()
    relation_registrations: tuple[RelationTypeRegistration, ...] = ()
    requirement_registrations: tuple[RequirementTypeRegistration, ...] = ()
    candidate_deriver_registrations: tuple[CandidateDeriverRegistration, ...] = ()
    validator_registrations: tuple[ValidatorRegistration, ...] = ()
    experiment_executor_registrations: tuple[ExperimentExecutorRegistration, ...] = ()
    policy_constraint_evaluator_registrations: tuple[
        PolicyConstraintEvaluatorRegistration, ...
    ] = ()
    domain_state_codec_registrations: tuple[DomainStateCodecRegistration, ...] = ()


class ExtensionCatalog:
    def __init__(self) -> None:
        self.bundle_manifests: dict[str, BundleManifest] = {}
        self.relation_types = TypedRegistry[RelationTypeRegistration]("relation type")
        self.evidence_types = TypedRegistry[SchemaRegistration]("evidence type")
        self.observation_payloads = TypedRegistry[SchemaRegistration]("observation payload")
        self.requirement_types = TypedRegistry[RequirementTypeRegistration]("requirement type")
        self.expectation_types = TypedRegistry[SchemaRegistration]("expectation type")
        self.candidate_types = TypedRegistry[SchemaRegistration]("candidate type")
        self.candidate_derivers = TypedRegistry[CandidateDeriverRegistration](
            "candidate deriver", "deriver_id"
        )
        self.experiment_actions = TypedRegistry[SchemaRegistration]("experiment action")
        self.experiment_results = TypedRegistry[SchemaRegistration]("experiment result")
        self.validators = TypedRegistry[ValidatorRegistration]("validator", "validator_id")
        self.policy_constraints = TypedRegistry[SchemaRegistration]("policy constraint")
        self.experiment_executors = TypedRegistry[ExperimentExecutorRegistration](
            "experiment executor", "executor_id"
        )
        self.policy_constraint_evaluators = TypedRegistry[PolicyConstraintEvaluatorRegistration](
            "policy constraint evaluator", "constraint_type_id"
        )
        self.domain_state_codecs = TypedRegistry[DomainStateCodecRegistration](
            "domain state codec", "codec_id"
        )
        self._frozen = False
        self._semantic_manifest_hash: str | None = None

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def semantic_manifest_hash(self) -> str:
        if not self._frozen or self._semantic_manifest_hash is None:
            raise RuntimeError("catalog must be frozen before reading semantic manifest hash")
        return self._semantic_manifest_hash

    def _schema_registry(self, category: SchemaCategory) -> TypedRegistry[SchemaRegistration]:
        return {
            SchemaCategory.EVIDENCE: self.evidence_types,
            SchemaCategory.OBSERVATION_PAYLOAD: self.observation_payloads,
            SchemaCategory.EXPECTATION: self.expectation_types,
            SchemaCategory.CANDIDATE: self.candidate_types,
            SchemaCategory.EXPERIMENT_ACTION: self.experiment_actions,
            SchemaCategory.EXPERIMENT_RESULT: self.experiment_results,
            SchemaCategory.POLICY_CONSTRAINT: self.policy_constraints,
        }[category]

    def add_bundle(self, bundle: DomainBundle) -> None:
        if self._frozen:
            raise RuntimeError("cannot add bundle after catalog freeze")
        manifest = bundle.manifest
        if manifest.bundle_id in self.bundle_manifests:
            raise ValueError(f"duplicate bundle id: {manifest.bundle_id}")

        def validate_owner_and_namespace(type_id: str, owner_bundle_id: str) -> None:
            if owner_bundle_id != manifest.bundle_id:
                raise ValueError(f"registration owner mismatch for {type_id}")
            if not type_id.startswith(manifest.namespace + "."):
                raise ValueError(
                    f"registration {type_id} is outside bundle namespace {manifest.namespace}"
                )

        for item in bundle.schema_registrations:
            validate_owner_and_namespace(item.type_id, item.owner_bundle_id)
            self._schema_registry(item.category).register(item)
        for item in bundle.relation_registrations:
            validate_owner_and_namespace(item.type_id, item.owner_bundle_id)
            self.relation_types.register(item)
        for item in bundle.requirement_registrations:
            validate_owner_and_namespace(item.type_id, item.owner_bundle_id)
            self.requirement_types.register(item)
        for item in bundle.candidate_deriver_registrations:
            validate_owner_and_namespace(item.deriver_id, item.owner_bundle_id)
            self.candidate_derivers.register(item)
        for item in bundle.validator_registrations:
            validate_owner_and_namespace(item.validator_id, item.owner_bundle_id)
            self.validators.register(item)
        for item in bundle.experiment_executor_registrations:
            validate_owner_and_namespace(item.executor_id, item.owner_bundle_id)
            self.experiment_executors.register(item)
        for item in bundle.policy_constraint_evaluator_registrations:
            validate_owner_and_namespace(item.evaluator_id, item.owner_bundle_id)
            if not item.constraint_type_id.startswith(manifest.namespace + "."):
                raise ValueError(
                    f"registration {item.constraint_type_id} is outside bundle namespace "
                    f"{manifest.namespace}"
                )
            self.policy_constraint_evaluators.register(item)
        for item in bundle.domain_state_codec_registrations:
            validate_owner_and_namespace(item.codec_id, item.owner_bundle_id)
            self.domain_state_codecs.register(item)
        self.bundle_manifests[manifest.bundle_id] = manifest

    def validate_references(self) -> None:
        for registration in self.requirement_types.values():
            for payload_type_id in registration.allowed_observation_payload_type_ids:
                if self.observation_payloads.get(payload_type_id) is None:
                    raise ValueError(
                        f"requirement {registration.type_id} references unregistered payload "
                        f"{payload_type_id}"
                    )
        for registration in self.candidate_derivers.values():
            for candidate_type_id in registration.candidate_type_ids:
                candidate_type = self.candidate_types.get(candidate_type_id)
                if candidate_type is None:
                    raise ValueError(
                        f"candidate deriver {registration.deriver_id} references unregistered "
                        f"candidate type {candidate_type_id}"
                    )
                if candidate_type.owner_bundle_id != registration.owner_bundle_id:
                    raise ValueError(
                        "candidate deriver cannot emit another bundle's candidate type"
                    )
        for registration in self.validators.values():
            for candidate_type_id in registration.candidate_type_ids:
                if self.candidate_types.get(candidate_type_id) is None:
                    raise ValueError(
                        f"validator {registration.validator_id} references unregistered candidate "
                        f"type {candidate_type_id}"
                    )
        for registration in self.experiment_executors.values():
            for action_type_id in registration.action_type_ids:
                action_type = self.experiment_actions.get(action_type_id)
                if action_type is None:
                    raise ValueError(
                        f"experiment executor {registration.executor_id} references unregistered "
                        f"action type {action_type_id}"
                    )
                if action_type.owner_bundle_id != registration.owner_bundle_id:
                    raise ValueError(
                        "experiment executor cannot execute another bundle's action type"
                    )
            for result_type_id in registration.result_type_ids:
                result_type = self.experiment_results.get(result_type_id)
                if result_type is None:
                    raise ValueError(
                        f"experiment executor {registration.executor_id} references unregistered "
                        f"result type {result_type_id}"
                    )
                if result_type.owner_bundle_id != registration.owner_bundle_id:
                    raise ValueError(
                        "experiment executor cannot emit another bundle's result type"
                    )
        action_owners: dict[str, str] = {}
        for registration in self.experiment_executors.values():
            for action_type_id in registration.action_type_ids:
                existing = action_owners.get(action_type_id)
                if existing is not None:
                    raise ValueError(
                        f"experiment action {action_type_id} has multiple executors: "
                        f"{existing}, {registration.executor_id}"
                    )
                action_owners[action_type_id] = registration.executor_id
        for constraint in self.policy_constraints.values():
            if self.policy_constraint_evaluators.get(constraint.type_id) is None:
                raise ValueError(
                    f"policy constraint {constraint.type_id} has no registered evaluator"
                )
        for registration in self.policy_constraint_evaluators.values():
            constraint = self.policy_constraints.get(registration.constraint_type_id)
            if constraint is None:
                raise ValueError(
                    f"policy constraint evaluator {registration.evaluator_id} references "
                    f"unregistered constraint type {registration.constraint_type_id}"
                )
            if constraint.owner_bundle_id != registration.owner_bundle_id:
                raise ValueError(
                    "policy constraint evaluator cannot evaluate another bundle's constraint type"
                )

    def freeze(self) -> str:
        if self._frozen:
            return self.semantic_manifest_hash
        self.validate_references()
        registries = (
            self.relation_types,
            self.evidence_types,
            self.observation_payloads,
            self.requirement_types,
            self.expectation_types,
            self.candidate_types,
            self.candidate_derivers,
            self.experiment_actions,
            self.experiment_results,
            self.validators,
            self.policy_constraints,
            self.experiment_executors,
            self.policy_constraint_evaluators,
            self.domain_state_codecs,
        )
        for registry in registries:
            registry.freeze()
        manifest_payload = {
            "bundles": [
                manifest.model_dump(mode="json")
                for _, manifest in sorted(self.bundle_manifests.items())
            ],
            "registries": {
                registry.name: registry.manifest_rows()
                for registry in sorted(registries, key=lambda item: item.name)
            },
        }
        self._semantic_manifest_hash = "sha256:" + sha256_text(canonical_json(manifest_payload))
        self._frozen = True
        return self._semantic_manifest_hash


def _topological_order(bundles: tuple[DomainBundle, ...]) -> tuple[DomainBundle, ...]:
    by_id = {bundle.manifest.bundle_id: bundle for bundle in bundles}
    if len(by_id) != len(bundles):
        raise ValueError("duplicate bundle id in composition input")
    for bundle in bundles:
        missing = set(bundle.manifest.requires_bundle_ids) - set(by_id)
        if missing:
            raise ValueError(
                f"bundle {bundle.manifest.bundle_id} has missing dependencies: {sorted(missing)}"
            )
    temporary: set[str] = set()
    permanent: set[str] = set()
    ordered: list[DomainBundle] = []

    def visit(bundle_id: str) -> None:
        if bundle_id in permanent:
            return
        if bundle_id in temporary:
            raise ValueError("bundle dependency cycle detected")
        temporary.add(bundle_id)
        bundle = by_id[bundle_id]
        for dependency in sorted(bundle.manifest.requires_bundle_ids):
            visit(dependency)
        temporary.remove(bundle_id)
        permanent.add(bundle_id)
        ordered.append(bundle)

    for bundle_id in sorted(by_id):
        visit(bundle_id)
    return tuple(ordered)


def compose_bundles(bundles: tuple[DomainBundle, ...]) -> ExtensionCatalog:
    catalog = ExtensionCatalog()
    for bundle in _topological_order(bundles):
        catalog.add_bundle(bundle)
    catalog.freeze()
    return catalog
