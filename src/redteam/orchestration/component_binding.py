"""Stable component-binding record for execution composition (A2).

A run records which implementations it was built with as plain metadata:
a profile id, a version, and one stable id per component slot. Python
objects, factories, secrets, and evidence never enter the checkpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BUILTIN_PROFILE_ID = "builtin"
BUILTIN_PROFILE_VERSION = "1"

AGENTS_COMPONENT = "agents"
CANDIDATE_PROVIDERS_COMPONENT = "candidate_providers"
VERIFIER_COMPONENT = "verifier"
RESULT_MAPPERS_COMPONENT = "result_mappers"

COMPONENT_SLOTS: tuple[str, ...] = (
    AGENTS_COMPONENT,
    CANDIDATE_PROVIDERS_COMPONENT,
    VERIFIER_COMPONENT,
    RESULT_MAPPERS_COMPONENT,
)

BUILTIN_COMPONENT_IDS: dict[str, str] = {
    AGENTS_COMPONENT: "builtin-agents-v1",
    CANDIDATE_PROVIDERS_COMPONENT: "builtin-providers-v1",
    VERIFIER_COMPONENT: "builtin-verifier-v1",
    RESULT_MAPPERS_COMPONENT: "builtin-mappers-v1",
}


class ComponentBindingError(ValueError):
    """Base error for profile registration and resolution failures."""


class DuplicateComponentProfileError(ComponentBindingError):
    """A profile id plus version was registered twice."""


class UnknownComponentProfileError(ComponentBindingError):
    """No profile is registered for the requested id plus version."""


class ComponentMismatchError(ComponentBindingError):
    """Stored component ids differ from the registered profile ids."""


class NonRebuildableComponentsError(ComponentBindingError):
    """Explicit objects were passed where only a profile can rebuild."""


class ComponentProfile(BaseModel):
    """Selection handle: which registered profile to build a run from."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = BUILTIN_PROFILE_ID
    version: str = BUILTIN_PROFILE_VERSION


class RuntimeComponentBinding(BaseModel):
    """Durable per-run record of the resolved component set."""

    model_config = ConfigDict(frozen=True)

    profile_id: str = BUILTIN_PROFILE_ID
    version: str = BUILTIN_PROFILE_VERSION
    component_ids: dict[str, str] = Field(default_factory=dict)
    extension_versions: dict[str, str] = Field(default_factory=dict)
    extension_entry_points: tuple[str, ...] = ()
    planning_provider_id: str | None = None
    role_tools_fingerprint: str | None = None
    specialist_profile_path: str | None = None
    specialist_profile_fingerprint: str | None = None
    prompt_bindings: dict[str, dict[str, str]] = Field(default_factory=dict)

    def component_id(self, slot: str) -> str | None:
        """Return the stable id recorded for one component slot."""
        return self.component_ids.get(slot)


def builtin_component_binding() -> RuntimeComponentBinding:
    """Return the canonical builtin record."""
    return RuntimeComponentBinding(
        profile_id=BUILTIN_PROFILE_ID,
        version=BUILTIN_PROFILE_VERSION,
        component_ids=dict(BUILTIN_COMPONENT_IDS),
    )


def is_builtin_binding(binding: RuntimeComponentBinding | None) -> bool:
    """Return true only for the full canonical builtin record."""
    return binding is not None and binding == builtin_component_binding()


def selection_of(binding: RuntimeComponentBinding) -> ComponentProfile:
    """Return the lookup key a durable record was resolved from."""
    return ComponentProfile(profile_id=binding.profile_id, version=binding.version)


ComponentFactory = Callable[[Any], Any]


@dataclass(frozen=True)
class ComponentRegistration:
    """One registered profile: lookup key, declared ids, factory."""

    profile: ComponentProfile
    component_ids: dict[str, str]
    factory: ComponentFactory


class ComponentRegistry:
    """Explicit profile map. No plugin auto discovery."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], ComponentRegistration] = {}

    def register(
        self,
        profile: ComponentProfile,
        *,
        component_ids: dict[str, str],
        factory: ComponentFactory,
    ) -> ComponentProfile:
        """Register one profile. Duplicate id plus version is an error."""
        missing = [slot for slot in COMPONENT_SLOTS if slot not in component_ids]
        if missing:
            raise ComponentBindingError(
                f"profile {profile.profile_id!r} version {profile.version!r} "
                f"is missing ids for {missing}"
            )
        extra = [key for key in component_ids if key not in COMPONENT_SLOTS]
        if extra:
            raise ComponentBindingError(
                f"profile {profile.profile_id!r} version {profile.version!r} "
                f"lists unknown slots {extra}"
            )
        key = (profile.profile_id, profile.version)
        if key in self._entries:
            raise DuplicateComponentProfileError(
                f"component profile {profile.profile_id!r} "
                f"version {profile.version!r} is already registered"
            )
        self._entries[key] = ComponentRegistration(
            profile=profile,
            component_ids=dict(component_ids),
            factory=factory,
        )
        return profile

    @property
    def profiles(self) -> list[ComponentProfile]:
        """Return every registered selection key."""
        return [entry.profile for entry in self._entries.values()]

    def resolve(self, selection: ComponentProfile | None = None) -> ComponentRegistration:
        """Resolve a selection. None means builtin. Unknown is an error."""
        wanted = selection if selection is not None else ComponentProfile()
        try:
            return self._entries[(wanted.profile_id, wanted.version)]
        except KeyError:
            raise UnknownComponentProfileError(
                f"no component profile {wanted.profile_id!r} "
                f"version {wanted.version!r} is registered"
            ) from None

    def resolve_binding(self, binding: RuntimeComponentBinding) -> ComponentRegistration:
        """Resolve a durable record. Unknown or changed ids are errors."""
        try:
            registration = self._entries[(binding.profile_id, binding.version)]
        except KeyError:
            raise UnknownComponentProfileError(
                f"no component profile {binding.profile_id!r} "
                f"version {binding.version!r} is registered"
            ) from None
        if registration.component_ids != binding.component_ids:
            raise ComponentMismatchError(
                f"stored components for profile {binding.profile_id!r} "
                f"version {binding.version!r} differ from the registered set"
            )
        return registration


def binding_for_registration(registration: ComponentRegistration) -> RuntimeComponentBinding:
    """Return the durable record for a resolved registration."""
    return RuntimeComponentBinding(
        profile_id=registration.profile.profile_id,
        version=registration.profile.version,
        component_ids=dict(registration.component_ids),
    )
