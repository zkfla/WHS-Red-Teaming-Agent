"""Data crossing capability and orchestration; implementations stay with providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationTargetBinding:
    operation: str
    binds_target_arguments: bool = True
    binds_known_path_from_url: bool = False
    requires_url_target: bool = False
    requires_exact_url_match: bool = False


LEGACY_OPERATION_BINDINGS = (
    OperationTargetBinding("default"),
    OperationTargetBinding("web_search", binds_target_arguments=False),
    OperationTargetBinding("endpoint_discovery", binds_known_path_from_url=True),
    OperationTargetBinding("page_read", requires_url_target=True, requires_exact_url_match=True),
)


def operation_target_binding(
    operation: str,
    bindings: Mapping[str, OperationTargetBinding] | None = None,
) -> OperationTargetBinding:
    table = (
        {item.operation: item for item in LEGACY_OPERATION_BINDINGS}
        if bindings is None
        else bindings
    )
    return table.get(operation, table.get("default", OperationTargetBinding("default")))


TARGET_ARGUMENT_VALUES = frozenset(
    {
        "host",
        "domain",
        "hostname",
        "url",
        "base_url",
        "target_url",
        "port",
        "path",
        "target_ref",
        "service",
        "asset_ref",
        "apk_ref",
        "artifact_ref",
        "device",
        "device_ref",
        "packageName",
        "package_name",
    }
)


RetryPredicate = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class RetryPolicy:
    safe_actions: frozenset[str] = frozenset()
    http_safe_methods: frozenset[str] = frozenset()
    http_mutating_argument_keys: frozenset[str] = frozenset()
    conditional_predicates: tuple[tuple[str, RetryPredicate], ...] = ()

    def permits(self, capability: str, operation: str, arguments: Mapping[str, Any]) -> bool:
        action = f"{capability}.{operation}"
        if action in self.safe_actions:
            return True
        for key, predicate in self.conditional_predicates:
            if key == action:
                return bool(predicate(arguments))
        return False


@dataclass(frozen=True)
class ExecutionRecipeSpec:
    name: str
    capability: str
    operation: str
    tool_name: str
    executable: str
    executable_args: tuple[str, ...]
    timeout_seconds: float

    def build_arguments(
        self, *, asset_ref: str, path: str, engagement_id: str, run_id: str
    ) -> dict[str, Any]:
        return {
            "asset_ref": asset_ref,
            "executable": self.executable,
            "args": [*self.executable_args, path],
            "timeout_seconds": self.timeout_seconds,
            "engagement_id": engagement_id,
            "run_id": run_id,
        }
