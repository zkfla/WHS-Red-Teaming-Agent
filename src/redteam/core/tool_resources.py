"""Shared validation of explicit provider resource declarations."""

from __future__ import annotations

from typing import Any

from redteam.core.contracts.tool import ToolDescriptor


class ResourceBindingError(ValueError):
    """Fixed diagnostic; never includes a resource value or secret input."""

    def __init__(self, argument: str = "") -> None:
        self.argument = argument
        super().__init__("invalid declared resource binding")


def validate_resource_schema(tool: ToolDescriptor) -> None:
    properties = tool.input_schema.get("properties", {})
    for binding in tool.resource_bindings:
        if binding.argument is not None and (
            not isinstance(properties, dict) or binding.argument not in properties
        ):
            raise ResourceBindingError(binding.argument)


def resource_identities(tool: ToolDescriptor, arguments: dict[str, Any]) -> list[tuple[str, str]]:
    validate_resource_schema(tool)
    identities = []
    for binding in tool.resource_bindings:
        value: Any
        if binding.key is not None:
            value = binding.key
        else:
            value = arguments.get(binding.argument or "")
            if value is None and binding.optional:
                continue
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ResourceBindingError(binding.argument or "")
            if binding.value_map:
                if value not in binding.value_map:
                    raise ResourceBindingError(binding.argument or "")
                value = binding.value_map[value]
        identities.append((binding.namespace, value))
    return identities
