"""Opaque resource keys for the existing work ledger; no new lock manager."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from redteam.core.contracts.tool import ToolDescriptor, ToolInvocation
from redteam.core.convert import execution_input_digest
from redteam.core.tool_resources import resource_identities

INVOCATION_RESOURCE_PREFIX = "invocation:"


def execution_resource_keys(
    tool: ToolDescriptor, invocation: ToolInvocation, principal_ref: str | None,
) -> list[str]:
    """Serialize exact non-retry-safe physical calls, independently of actor IDs.

    Explicit session/account arguments are part of the normalized input digest.
    Provider identity is the Run-bound server, never an inferred session name.
    """
    keys = resource_conflict_keys(tool, invocation.arguments)
    if not tool.retry_safe:
        physical = [str(invocation.run_id), str(tool.server_id), str(tool.tool_id),
                    execution_input_digest(invocation), principal_ref]
        keys.append(INVOCATION_RESOURCE_PREFIX + hashlib.sha256(
            json.dumps(physical, ensure_ascii=False, separators=(",", ":")).encode(),
        ).hexdigest())
    return sorted(keys)


def resource_conflict_keys(tool: ToolDescriptor, arguments: dict[str, Any]) -> list[str]:
    # Include the namespace in the hash, not the server/Run/instance/URL.
    # Different providers can therefore contend for the same physical resource.
    # Never persist raw cookies, refs, serials, workspace paths or tokens as keys.
    return sorted(
        {
            "resource:"
            + hashlib.sha256(
                json.dumps([namespace, value], ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            for namespace, value in resource_identities(tool, arguments)
        }
    )
