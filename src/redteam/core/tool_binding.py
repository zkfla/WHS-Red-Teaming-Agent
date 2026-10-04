# Shared direct/legacy binding helpers owned by Core.
# Dependency-free: plain strings and mappings only.
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

BindingMode = Literal["legacy_action", "selected_tool"]

LEGACY_ACTION: BindingMode = "legacy_action"
SELECTED_TOOL: BindingMode = "selected_tool"


def is_direct(binding_mode, tool_id):
    return binding_mode == SELECTED_TOOL and bool(tool_id)


def execution_display_key(proposal):
    mode = getattr(proposal, "binding_mode", "legacy_action")
    tool_id = getattr(proposal, "tool_id", None)
    if mode == SELECTED_TOOL and tool_id:
        return str(tool_id)
    capability = getattr(proposal, "capability", "")
    operation = getattr(proposal, "operation", "")
    if capability and operation:
        return capability + "." + operation
    return operation or capability or ""


def normalize_json(value):
    # Strict JSON round-trip: non-JSON values raise instead of being
    # laundered into strings that would fake an approval fingerprint.
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def normalize_arguments(arguments):
    return normalize_json(dict(arguments))


def execution_fingerprint(tool_id, target, arguments):
    payload = {"tool_id": str(tool_id)}
    dump = getattr(target, "model_dump", None)
    if callable(dump):
        try:
            payload["target"] = dump(mode="json")
        except TypeError:
            payload["target"] = dump()
    else:
        payload["target"] = dict(target) if isinstance(target, Mapping) else str(target)
    payload["arguments"] = normalize_arguments(arguments)
    blob = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
