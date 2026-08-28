"""Built-in probes that adapt existing workers to the central dispatcher."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from whs_redteam.models import (
    ActionProposal,
    Capability,
    ResourceOwnershipRecord,
)
from whs_redteam.openapi import import_openapi_schemas
from whs_redteam.orchestration.adapters import ProbeContext
from whs_redteam.resource_model import model_resources
from whs_redteam.tools.actor_sessions import ActorSessionSummary


class DiscoveryProbe:
    capability = Capability.DISCOVERY

    def __init__(
        self,
        handler: Callable[[str], Awaitable[None]],
        exploration_run_id: str,
    ) -> None:
        self.handler = handler
        self.exploration_run_id = exploration_run_id

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]:
        await self.handler(self.exploration_run_id)
        return {
            "exploration_run_id": self.exploration_run_id,
            "target_url": proposal.target_url,
        }


class FindingValidationProbe:
    capability = Capability.FINDING_VALIDATION

    def __init__(self, handler: Callable[[], Awaitable[str]]) -> None:
        self.handler = handler

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]:
        validation_id = await self.handler()
        return {
            "validation_id": validation_id,
            "target_url": proposal.target_url,
        }


class ApiSchemaProbe:
    capability = Capability.API_SCHEMA

    def __init__(self, exploration_run_id: str) -> None:
        self.exploration_run_id = exploration_run_id

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]:
        schemas = context.inventory.list_api_request_schemas(
            self.exploration_run_id
        )
        openapi_imported = 0
        if context.config is not None:
            openapi_imported = import_openapi_schemas(
                context.config,
                context.inventory,
                self.exploration_run_id,
            )
        schemas = context.inventory.list_api_request_schemas(
            self.exploration_run_id
        )
        return {
            "exploration_run_id": self.exploration_run_id,
            "schemas_observed": len(schemas),
            "endpoints_with_query": sum(
                bool(schema.query_schema) for schema in schemas
            ),
            "endpoints_with_body": sum(
                schema.body_schema is not None for schema in schemas
            ),
            "openapi_imported": openapi_imported,
            "target_url": proposal.target_url,
        }


class ActorSessionProbe:
    capability = Capability.AUTH_SESSION

    def __init__(self, handler: Callable[[], ActorSessionSummary]) -> None:
        self.handler = handler

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]:
        summary = self.handler()
        return {
            **summary.model_dump(mode="json"),
            "target_url": proposal.target_url,
        }


class ResourceModelProbe:
    capability = Capability.RESOURCE_MODEL

    def __init__(
        self,
        exploration_run_id: str,
        ownership_records: tuple[ResourceOwnershipRecord, ...] = (),
    ) -> None:
        self.exploration_run_id = exploration_run_id
        self.ownership_records = ownership_records

    async def execute(
        self, proposal: ActionProposal, context: ProbeContext
    ) -> dict[str, Any]:
        resources = model_resources(
            context.inventory.list_endpoints(self.exploration_run_id),
            context.inventory.list_api_request_schemas(self.exploration_run_id),
        )
        context.inventory.replace_resources(self.exploration_run_id, resources)
        context.inventory.replace_resource_ownership(
            self.exploration_run_id, list(self.ownership_records)
        )
        return {
            "exploration_run_id": self.exploration_run_id,
            "resources_modeled": len(resources),
            "identifier_candidates": sum(
                len(resource.identifiers) for resource in resources
            ),
            "ownership_assertions": len(self.ownership_records),
            "target_url": proposal.target_url,
        }
