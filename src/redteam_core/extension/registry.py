from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from redteam_core.graph.semantics import RelationSemantics
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.provenance import TYPE_ID_PATTERN, TypedPayloadEnvelope

FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TYPE_ID_RE = re.compile(TYPE_ID_PATTERN)


class SchemaCategory(StrEnum):
    EVIDENCE = "evidence"
    OBSERVATION_PAYLOAD = "observation_payload"
    EXPECTATION = "expectation"
    CANDIDATE = "candidate"
    EXPERIMENT_ACTION = "experiment_action"
    EXPERIMENT_RESULT = "experiment_result"
    POLICY_CONSTRAINT = "policy_constraint"


def _validate_common(
    *,
    type_id: str,
    schema_version: str,
    owner_bundle_id: str,
    implementation_fingerprint: str,
) -> None:
    if not TYPE_ID_RE.fullmatch(type_id):
        raise ValueError(f"invalid namespaced type id: {type_id!r}")
    if not schema_version or len(schema_version) > 40:
        raise ValueError("schema_version must be non-empty and <=40 characters")
    if not owner_bundle_id or len(owner_bundle_id) > 120:
        raise ValueError("owner_bundle_id must be non-empty and <=120 characters")
    if not FINGERPRINT_PATTERN.fullmatch(implementation_fingerprint):
        raise ValueError("implementation_fingerprint must be sha256:<64 lowercase hex>")


def model_schema_hash(model: type[BaseModel]) -> str:
    return "sha256:" + sha256_text(canonical_json(model.model_json_schema()))


def fingerprint_source_files(paths: tuple[Path, ...] | list[Path]) -> str:
    """Build-time helper for deterministic implementation fingerprints.

    It hashes explicit file bytes in caller-provided order after canonical path sorting. Runtime
    composition does not introspect source code.
    """

    normalized = sorted(Path(path).resolve() for path in paths)
    digest = hashlib.sha256()
    for path in normalized:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def verify_source_fingerprint(
    expected_fingerprint: str,
    paths: tuple[Path, ...] | list[Path],
) -> None:
    """CI/build-time stale semantic fingerprint guard for explicit source files."""

    if not FINGERPRINT_PATTERN.fullmatch(expected_fingerprint):
        raise ValueError("expected implementation fingerprint is invalid")
    actual = fingerprint_source_files(paths)
    if actual != expected_fingerprint:
        raise ValueError(
            f"semantic implementation fingerprint mismatch: expected {expected_fingerprint}, "
            f"actual {actual}"
        )


@dataclass(frozen=True)
class SchemaRegistration:
    category: SchemaCategory
    type_id: str
    schema_version: str
    owner_bundle_id: str
    model: type[BaseModel]
    implementation_fingerprint: str

    def __post_init__(self) -> None:
        _validate_common(
            type_id=self.type_id,
            schema_version=self.schema_version,
            owner_bundle_id=self.owner_bundle_id,
            implementation_fingerprint=self.implementation_fingerprint,
        )
        if not issubclass(self.model, BaseModel):
            raise TypeError("registered schema model must subclass pydantic BaseModel")

    def manifest_record(self) -> dict[str, str]:
        return {
            "category": self.category.value,
            "type_id": self.type_id,
            "schema_version": self.schema_version,
            "owner_bundle_id": self.owner_bundle_id,
            "model_schema_hash": model_schema_hash(self.model),
            "implementation_fingerprint": self.implementation_fingerprint,
        }

    def envelope_from_model(self, value: BaseModel) -> TypedPayloadEnvelope:
        if not isinstance(value, self.model):
            raise TypeError(
                f"payload for {self.type_id} must be {self.model.__name__}, "
                f"got {type(value).__name__}"
            )
        payload_json = canonical_json(value)
        return TypedPayloadEnvelope(
            type_id=self.type_id,
            schema_version=self.schema_version,
            canonical_payload_json=payload_json,
            content_hash=sha256_text(payload_json),
        )

    def decode(self, envelope: TypedPayloadEnvelope) -> BaseModel:
        if envelope.type_id != self.type_id or envelope.schema_version != self.schema_version:
            raise ValueError("payload envelope does not match registered schema")
        return self.model.model_validate_json(envelope.canonical_payload_json)


@dataclass(frozen=True)
class RelationTypeRegistration:
    type_id: str
    schema_version: str
    owner_bundle_id: str
    implementation_fingerprint: str
    semantics: RelationSemantics = field(default_factory=RelationSemantics)

    def __post_init__(self) -> None:
        _validate_common(
            type_id=self.type_id,
            schema_version=self.schema_version,
            owner_bundle_id=self.owner_bundle_id,
            implementation_fingerprint=self.implementation_fingerprint,
        )

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "relation",
            "type_id": self.type_id,
            "schema_version": self.schema_version,
            "owner_bundle_id": self.owner_bundle_id,
            "implementation_fingerprint": self.implementation_fingerprint,
            "semantics": self.semantics.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class RequirementTypeRegistration:
    type_id: str
    schema_version: str
    owner_bundle_id: str
    allowed_observation_payload_type_ids: frozenset[str]
    implementation_fingerprint: str

    def __post_init__(self) -> None:
        _validate_common(
            type_id=self.type_id,
            schema_version=self.schema_version,
            owner_bundle_id=self.owner_bundle_id,
            implementation_fingerprint=self.implementation_fingerprint,
        )
        if not self.allowed_observation_payload_type_ids:
            raise ValueError("requirement type must allow at least one observation payload type")
        for payload_type_id in self.allowed_observation_payload_type_ids:
            if not TYPE_ID_RE.fullmatch(payload_type_id):
                raise ValueError(f"invalid observation payload type id: {payload_type_id!r}")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "requirement",
            "type_id": self.type_id,
            "schema_version": self.schema_version,
            "owner_bundle_id": self.owner_bundle_id,
            "allowed_observation_payload_type_ids": sorted(
                self.allowed_observation_payload_type_ids
            ),
            "implementation_fingerprint": self.implementation_fingerprint,
        }


@dataclass(frozen=True)
class CandidateDeriverRegistration:
    deriver_id: str
    owner_bundle_id: str
    candidate_type_ids: frozenset[str]
    implementation_fingerprint: str

    def __post_init__(self) -> None:
        if not TYPE_ID_RE.fullmatch(self.deriver_id):
            raise ValueError("candidate deriver id must be a namespaced versioned id")
        if not self.owner_bundle_id or len(self.owner_bundle_id) > 120:
            raise ValueError("candidate deriver owner is invalid")
        if not self.candidate_type_ids:
            raise ValueError("candidate deriver must declare at least one candidate type")
        for type_id in self.candidate_type_ids:
            if not TYPE_ID_RE.fullmatch(type_id):
                raise ValueError(f"invalid candidate type id: {type_id!r}")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("candidate deriver implementation fingerprint is invalid")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "candidate_deriver",
            "deriver_id": self.deriver_id,
            "owner_bundle_id": self.owner_bundle_id,
            "candidate_type_ids": sorted(self.candidate_type_ids),
            "implementation_fingerprint": self.implementation_fingerprint,
        }


@dataclass(frozen=True)
class ValidatorRegistration:
    validator_id: str
    owner_bundle_id: str
    implementation_fingerprint: str
    candidate_type_ids: frozenset[str] = frozenset()
    requires_experiment_result: bool = False

    def __post_init__(self) -> None:
        if not TYPE_ID_RE.fullmatch(self.validator_id):
            raise ValueError("validator_id must be a namespaced versioned id")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("validator implementation fingerprint is invalid")
        for candidate_type_id in self.candidate_type_ids:
            if not TYPE_ID_RE.fullmatch(candidate_type_id):
                raise ValueError(f"invalid validator candidate type id: {candidate_type_id!r}")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "validator",
            "validator_id": self.validator_id,
            "owner_bundle_id": self.owner_bundle_id,
            "implementation_fingerprint": self.implementation_fingerprint,
            "candidate_type_ids": sorted(self.candidate_type_ids),
            "requires_experiment_result": self.requires_experiment_result,
        }


@dataclass(frozen=True)
class ExperimentExecutorRegistration:
    executor_id: str
    owner_bundle_id: str
    action_type_ids: frozenset[str]
    implementation_fingerprint: str
    result_type_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not TYPE_ID_RE.fullmatch(self.executor_id):
            raise ValueError("experiment executor id must be a namespaced versioned id")
        if not self.owner_bundle_id or len(self.owner_bundle_id) > 120:
            raise ValueError("experiment executor owner is invalid")
        if not self.action_type_ids:
            raise ValueError("experiment executor must declare at least one action type")
        for action_type_id in self.action_type_ids:
            if not TYPE_ID_RE.fullmatch(action_type_id):
                raise ValueError(f"invalid experiment action type id: {action_type_id!r}")
        for result_type_id in self.result_type_ids:
            if not TYPE_ID_RE.fullmatch(result_type_id):
                raise ValueError(f"invalid experiment result type id: {result_type_id!r}")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("experiment executor implementation fingerprint is invalid")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "experiment_executor",
            "executor_id": self.executor_id,
            "owner_bundle_id": self.owner_bundle_id,
            "action_type_ids": sorted(self.action_type_ids),
            "result_type_ids": sorted(self.result_type_ids),
            "implementation_fingerprint": self.implementation_fingerprint,
        }


@dataclass(frozen=True)
class PolicyConstraintEvaluatorRegistration:
    constraint_type_id: str
    evaluator_id: str
    owner_bundle_id: str
    implementation_fingerprint: str

    def __post_init__(self) -> None:
        if not TYPE_ID_RE.fullmatch(self.constraint_type_id):
            raise ValueError("policy constraint type id must be namespaced and versioned")
        if not TYPE_ID_RE.fullmatch(self.evaluator_id):
            raise ValueError("policy constraint evaluator id must be namespaced and versioned")
        if not self.owner_bundle_id or len(self.owner_bundle_id) > 120:
            raise ValueError("policy constraint evaluator owner is invalid")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("policy constraint evaluator implementation fingerprint is invalid")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "policy_constraint_evaluator",
            "constraint_type_id": self.constraint_type_id,
            "evaluator_id": self.evaluator_id,
            "owner_bundle_id": self.owner_bundle_id,
            "implementation_fingerprint": self.implementation_fingerprint,
        }


@dataclass(frozen=True)
class DomainStateCodecRegistration:
    codec_id: str
    owner_bundle_id: str
    state_schema_version: str
    implementation_fingerprint: str

    def __post_init__(self) -> None:
        if not TYPE_ID_RE.fullmatch(self.codec_id):
            raise ValueError("domain state codec id must be namespaced and versioned")
        if not self.owner_bundle_id or len(self.owner_bundle_id) > 120:
            raise ValueError("domain state codec owner is invalid")
        if not self.state_schema_version or len(self.state_schema_version) > 40:
            raise ValueError("domain state schema version is invalid")
        if not FINGERPRINT_PATTERN.fullmatch(self.implementation_fingerprint):
            raise ValueError("domain state codec implementation fingerprint is invalid")

    def manifest_record(self) -> dict[str, object]:
        return {
            "category": "domain_state_codec",
            "codec_id": self.codec_id,
            "owner_bundle_id": self.owner_bundle_id,
            "state_schema_version": self.state_schema_version,
            "implementation_fingerprint": self.implementation_fingerprint,
        }


T = TypeVar("T")


class FrozenRegistryError(RuntimeError):
    pass


class TypedRegistry[T]:
    def __init__(self, name: str, key_attribute: str = "type_id") -> None:
        self.name = name
        self.key_attribute = key_attribute
        self._items: dict[str, T] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, item: T) -> None:
        if self._frozen:
            raise FrozenRegistryError(f"registry {self.name} is frozen")
        key = getattr(item, self.key_attribute)
        if key in self._items:
            raise ValueError(f"duplicate {self.name} registration: {key}")
        self._items[key] = item

    def get(self, key: str) -> T | None:
        return self._items.get(key)

    def require(self, key: str) -> T:
        item = self.get(key)
        if item is None:
            raise KeyError(f"unregistered {self.name}: {key}")
        return item

    def values(self) -> tuple[T, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def freeze(self) -> None:
        self._frozen = True

    def manifest_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for item in self.values():
            record = item.manifest_record()
            rows.append(record)
        return rows
