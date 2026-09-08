from __future__ import annotations

from typing import Protocol

from pydantic import ConfigDict, Field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.models.base import SchemaModel
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.provenance import TYPE_ID_PATTERN


class DomainStateCodecDescriptor(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    codec_id: str = Field(pattern=TYPE_ID_PATTERN)
    owner_bundle_id: str = Field(min_length=1, max_length=120)
    state_schema_version: str = Field(min_length=1, max_length=40)
    implementation_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class DomainStateCodec(Protocol):
    descriptor: DomainStateCodecDescriptor

    def encode(self, state: object) -> str: ...

    def validate(self, canonical_payload_json: str) -> None: ...

    def decode(self, canonical_payload_json: str) -> object: ...


class DomainStateCodecRegistry:
    def __init__(self) -> None:
        self._items: dict[str, DomainStateCodec] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, codec: DomainStateCodec) -> None:
        if self._frozen:
            raise RuntimeError("domain state codec registry is frozen")
        codec_id = codec.descriptor.codec_id
        if codec_id in self._items:
            raise ValueError(f"duplicate domain state codec runtime: {codec_id}")
        self._items[codec_id] = codec

    def freeze(self) -> None:
        self._frozen = True

    def require(self, codec_id: str) -> DomainStateCodec:
        try:
            return self._items[codec_id]
        except KeyError as exc:
            raise KeyError(f"unregistered domain state codec runtime: {codec_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))


def _validate_runtime_registry(
    catalog: ExtensionCatalog,
    registry: DomainStateCodecRegistry,
) -> None:
    if not catalog.frozen or not registry.frozen:
        raise RuntimeError("catalog and domain state codec registry must be frozen")
    expected = tuple(item.codec_id for item in catalog.domain_state_codecs.values())
    if registry.ids() != expected:
        raise ValueError("runtime domain state codec set does not match semantic manifest")
    for registration in catalog.domain_state_codecs.values():
        descriptor = registry.require(registration.codec_id).descriptor
        if descriptor.owner_bundle_id != registration.owner_bundle_id:
            raise ValueError("runtime domain state codec owner differs from semantic manifest")
        if descriptor.state_schema_version != registration.state_schema_version:
            raise ValueError("runtime domain state codec schema differs from semantic manifest")
        if descriptor.implementation_fingerprint != registration.implementation_fingerprint:
            raise ValueError(
                "runtime domain state codec implementation differs from semantic manifest"
            )


def validate_domain_state_envelopes(
    catalog: ExtensionCatalog,
    envelopes: tuple[DomainStateEnvelope, ...],
) -> tuple[DomainStateEnvelope, ...]:
    if not catalog.frozen:
        raise RuntimeError("catalog must be frozen before validating domain state")
    ordered = tuple(sorted(envelopes, key=lambda item: item.bundle_id))
    if len({item.bundle_id for item in ordered}) != len(ordered):
        raise ValueError("checkpoint may contain at most one domain state per bundle")
    for envelope in ordered:
        registration = catalog.domain_state_codecs.get(envelope.codec_id)
        if registration is None:
            raise ValueError("domain state references an unregistered codec")
        if registration.owner_bundle_id != envelope.bundle_id:
            raise ValueError("domain state bundle does not own its codec")
        if registration.state_schema_version != envelope.schema_version:
            raise ValueError("domain state schema version differs from codec registration")
    return ordered


def encode_domain_state(
    *,
    bundle_id: str,
    state: object,
    catalog: ExtensionCatalog,
    registry: DomainStateCodecRegistry,
) -> DomainStateEnvelope:
    _validate_runtime_registry(catalog, registry)
    registrations = [
        item for item in catalog.domain_state_codecs.values() if item.owner_bundle_id == bundle_id
    ]
    if len(registrations) != 1:
        raise ValueError("bundle must register exactly one domain state codec to encode state")
    registration = registrations[0]
    codec = registry.require(registration.codec_id)
    encoded = codec.encode(state)
    if not isinstance(encoded, str):
        raise TypeError("domain state codec encode() must return JSON text")
    envelope = DomainStateEnvelope.create(
        bundle_id=bundle_id,
        schema_version=registration.state_schema_version,
        codec_id=registration.codec_id,
        payload_json=encoded,
    )
    codec.validate(envelope.canonical_payload_json)
    return envelope


def decode_domain_state(
    envelope: DomainStateEnvelope,
    *,
    catalog: ExtensionCatalog,
    registry: DomainStateCodecRegistry,
) -> object:
    _validate_runtime_registry(catalog, registry)
    validate_domain_state_envelopes(catalog, (envelope,))
    codec = registry.require(envelope.codec_id)
    codec.validate(envelope.canonical_payload_json)
    return codec.decode(envelope.canonical_payload_json)
