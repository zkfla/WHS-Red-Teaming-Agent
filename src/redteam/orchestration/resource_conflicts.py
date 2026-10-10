"""Opaque resource keys for the existing work ledger; no new lock manager."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.tool_resources import resource_identities


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
