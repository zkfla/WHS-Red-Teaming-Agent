"""One registration unit per specialist role (M02/M07).

A role bundle keeps together what used to be edited in several places:
the scheduler-facing definition, the executable playbook, the link to
the candidate provider that proposes work for the role, and whether the
role ships in the default profile.

Capability values live in exactly one place: the definition. The
playbook side is derived and checked at registration time by the
ordered role holder, never silently merged here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from redteam.core.ids import AgentId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import (
    AgentDefinition,
    builtin_agent_definitions,
)

if TYPE_CHECKING:
    pass

#: Contribution kinds this package speaks. The composition root owns
#: collection; the names below are the draft so both sides can meet.
AGENT_ROLE_KIND = "agent_role"
CANDIDATE_PROVIDER_KIND = "candidate_provider"


def _recon_provider():  # lazy: recon.py reads providers.py at import time
    from redteam.orchestration.agents.recon import ReconCandidateProvider

    return ReconCandidateProvider()


def _web_provider():
    from redteam.orchestration.agents.web import WebCandidateProvider

    return WebCandidateProvider()


def _playbook_provider():
    from redteam.orchestration.agents.providers import PlaybookCandidateProvider

    return PlaybookCandidateProvider()


@dataclass(frozen=True)
class RolePackage:
    """One role's definition, playbook, provider link and default flag."""

    agent_id: AgentId
    definition: AgentDefinition
    #: ``None`` for seam roles (reporting): scheduled never, projected
    #: after the run drains. Everything else must carry a playbook.
    spec: AgentSpec | None = None
    provider_id: str = ""
    provider_factory: Callable[[], Any] = field(default=_playbook_provider)
    required_providers: tuple[str, ...] = ()
    optional_providers: tuple[str, ...] = ()
    default_include: bool = True


def builtin_role_packages() -> tuple[RolePackage, ...]:
    """The five agreed roles as bundles, in canonical schedule order."""
    from redteam.orchestration.agents.builtin import (
        ANALYSIS_AGENT,
        MOBILE_AGENT,
        WEB_API_AGENT,
    )
    from redteam.orchestration.agents.registry import RECON_AGENT

    definitions = {item.agent_id: item for item in builtin_agent_definitions()}
    specs: dict[AgentId, AgentSpec | None] = {
        RECON_AGENT.agent_id: RECON_AGENT,
        WEB_API_AGENT.agent_id: WEB_API_AGENT,
        MOBILE_AGENT.agent_id: MOBILE_AGENT,
        ANALYSIS_AGENT.agent_id: ANALYSIS_AGENT,
    }
    factories: dict[str, Callable[[], Any]] = {
        "playbook:recon": _recon_provider,
        "playbook:web_api": _web_provider,
    }
    order = [
        RECON_AGENT.agent_id,
        WEB_API_AGENT.agent_id,
        MOBILE_AGENT.agent_id,
        ANALYSIS_AGENT.agent_id,
        AgentId("reporting-agent"),
    ]
    packages = []
    for agent_id in order:
        definition = definitions[agent_id]
        packages.append(
            RolePackage(
                agent_id=agent_id,
                definition=definition,
                spec=specs.get(agent_id),
                provider_id=definition.provider_id,
                provider_factory=factories.get(definition.provider_id, _playbook_provider),
                default_include=True,
            )
        )
    return tuple(packages)


def register_role_package(agents, providers, package: RolePackage) -> RolePackage:
    """Register one bundle on the purpose-specific holders.

    Capability agreement stays enforced by the ordered holder itself;
    this only keeps definition-first ordering so a drift always raises
    from the same check, whichever side was registered first.
    """
    if package.agent_id != package.definition.agent_id or (
        package.spec is not None and package.agent_id != package.spec.agent_id
    ):
        raise ValueError("role package definition and executable identity must agree")
    if providers.get(package.provider_id) is None:
        providers.register(package.provider_id, package.provider_factory())
    agents.register_definition(package.definition)
    if package.spec is not None:
        agents.register(package.spec)
    return package


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    resolved: bool
    required: bool


@dataclass(frozen=True)
class RoleBindingReport:
    """How one role's provider references resolved (M07 start-up check)."""

    agent_id: AgentId
    bindings: tuple[ProviderBinding, ...] = ()

    @property
    def missing_required(self) -> tuple[str, ...]:
        return tuple(
            item.provider_id for item in self.bindings if item.required and not item.resolved
        )

    @property
    def missing_optional(self) -> tuple[str, ...]:
        return tuple(
            item.provider_id for item in self.bindings if not item.required and not item.resolved
        )


def resolve_role_package(
    package: RolePackage,
    providers,
    *,
    known_provider_ids: Iterable[str] | None = None,
) -> RoleBindingReport:
    """Resolve a role's provider references without creating anything.

    The role's own provider plus its required/optional references are
    looked up. Nothing is skipped silently: missing entries come back
    in the report so the assembler can fail (required) or show
    isolation (optional) before the run starts.
    """
    if known_provider_ids is not None:
        known = set(known_provider_ids)
    else:
        known = set(providers.provider_ids)
    refs = [
        ProviderBinding(
            provider_id=package.provider_id,
            resolved=package.provider_id in known,
            required=True,
        ),
        *[
            ProviderBinding(provider_id=ref, resolved=ref in known, required=True)
            for ref in package.required_providers
        ],
        *[
            ProviderBinding(provider_id=ref, resolved=ref in known, required=False)
            for ref in package.optional_providers
        ],
    ]
    return RoleBindingReport(agent_id=package.agent_id, bindings=tuple(refs))


def check_role_package_bindings(
    packages: Iterable[RolePackage],
    providers,
    *,
    known_provider_ids: Iterable[str] | None = None,
) -> tuple[RoleBindingReport, ...]:
    """Resolve every bundle; the caller decides fail vs. isolate."""
    return tuple(
        resolve_role_package(package, providers, known_provider_ids=known_provider_ids)
        for package in packages
    )


def role_package_contributions(package: RolePackage) -> tuple[Any, ...]:
    """Describe this bundle in the composition root's declaration shape.

    One ``agent_role`` contribution keyed by role id whose factory
    ignores its context argument and returns the bundle, and one
    ``candidate_provider`` contribution keyed by provider id whose
    factory ignores its context argument and builds a fresh provider.
    ``requires`` carries the required/optional provider references.
    """
    from redteam.core.extensions import Contribution, ContributionReference, value_factory

    provider_factory = package.provider_factory

    def _build_provider(_context: Any) -> Any:
        return provider_factory()

    role = Contribution(
        kind=AGENT_ROLE_KIND,
        contribution_id=str(package.agent_id),
        version=package.definition.contract_version,
        factory=value_factory(package),
        requires=(
            ContributionReference(
                kind=CANDIDATE_PROVIDER_KIND, contribution_id=package.provider_id
            ),
            *[
                ContributionReference(kind=CANDIDATE_PROVIDER_KIND, contribution_id=ref)
                for ref in package.required_providers
            ],
            *[
                ContributionReference(
                    kind=CANDIDATE_PROVIDER_KIND, contribution_id=ref, optional=True
                )
                for ref in package.optional_providers
            ],
        ),
    )
    provider = Contribution(
        kind=CANDIDATE_PROVIDER_KIND,
        contribution_id=package.provider_id,
        version=package.definition.contract_version,
        factory=_build_provider,
    )
    return (role, provider)


def builtin_extension_declaration():
    """All built-in bundles as one declaration for the composition root."""
    from redteam.core.extensions import ExtensionDeclaration

    contributions: list[Any] = []
    for package in builtin_role_packages():
        contributions.extend(role_package_contributions(package))
    return ExtensionDeclaration(
        extension_id="builtin.roles",
        version="1",
        contributions=tuple(contributions),
    )
