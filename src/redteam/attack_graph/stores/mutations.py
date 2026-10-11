"""Optional store-owned atomic graph mutation/receipt contract.

Receipts acknowledge storage only. They neither validate findings nor authorize
replay. The namespace wrapper prepares physical IDs before the caller fences
the immutable request; the owning store commits the change and receipt together.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from redteam.core.contracts.graph import GraphEdge, GraphNode

GraphOperation = Literal["add_node", "add_edge", "remove_node", "remove_edge"]


class GraphMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    namespace: str = ""
    operation: GraphOperation
    node: GraphNode | None = None
    edge: GraphEdge | None = None
    identity: str | None = None
    references: tuple[str, ...]

    @model_validator(mode="after")
    def check_payload(self) -> GraphMutation:
        present = (self.node is not None, self.edge is not None, bool(self.identity))
        required = {
            "add_node": (True, False, False), "add_edge": (False, True, False),
            "remove_node": (False, False, True), "remove_edge": (False, False, True),
        }
        if present != required[self.operation] or self.identity == "":
            raise ValueError("graph_mutation_payload_invalid")
        if (self.references != tuple(sorted(set(self.references)))
                or "graph" not in self.references or any(not ref for ref in self.references)):
            raise ValueError("graph_mutation_references_invalid")
        return self


def mutation_hash(mutation: GraphMutation) -> str:
    payload = json.dumps(mutation.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class GraphMutationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    mutation_id: str = Field(min_length=1)
    namespace: str = ""
    operation: GraphOperation
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    references: tuple[str, ...]
    result: StrictBool | None = None

    @model_validator(mode="after")
    def check_references(self) -> GraphMutationReceipt:
        if (self.references != tuple(sorted(set(self.references)))
                or "graph" not in self.references or any(not ref for ref in self.references)):
            raise ValueError("graph_mutation_references_invalid")
        if self.operation.startswith("remove_") != isinstance(self.result, bool):
            raise ValueError("graph_mutation_result_invalid")
        return self


class GraphMutationConflict(RuntimeError):
    """A receipt or its index does not match the original request."""


def checked_receipt(
    receipt: GraphMutationReceipt, mutation: GraphMutation,
) -> GraphMutationReceipt:
    if (receipt.run_id != mutation.run_id or receipt.mutation_id != mutation.mutation_id
            or receipt.namespace != mutation.namespace
            or receipt.operation != mutation.operation
            or receipt.request_hash != mutation_hash(mutation)
            or receipt.references != mutation.references):
        raise GraphMutationConflict("graph_mutation_receipt_conflict")
    return receipt


def make_receipt(mutation: GraphMutation, result: bool | None) -> GraphMutationReceipt:
    return GraphMutationReceipt(
        run_id=mutation.run_id, mutation_id=mutation.mutation_id, namespace=mutation.namespace,
        operation=mutation.operation,
        request_hash=mutation_hash(mutation), references=mutation.references, result=result,
    )


@runtime_checkable
class ReceiptedAttackGraphStore(Protocol):
    async def prepare_graph_mutation(self, mutation: GraphMutation) -> GraphMutation: ...

    async def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt: ...

    async def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None: ...
