"""Conservative P1 resource reservations; no tool/role-name routing.

Until providers expose finer read/write resources, calls sharing a backend,
network authority, or opaque resource reference serialize. A GET label or a
retry-safe declaration is not enough to prove concurrent use is safe.
"""

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.target_binding import network_identity


def execution_conflicts(target: AnyTarget, arguments: dict, tool: ToolDescriptor) -> list[str]:
    address = network_identity(target)
    authority = str(node_id_for_target(target))
    if address is not None:
        host = f"[{address.host}]" if ":" in address.host else address.host
        # Scope pinning is not execution identity. URL schemes already supply
        # an effective default port; never invent one for an unpinned host.
        authority = f"{host}:{address.port}" if address.port is not None else host
    keys = {
        f"server:{tool.server_id}",
        f"target:{authority}",
    }
    # Ref-valued inputs are opaque handles, not credentials. Equal handles
    # conflict even across different input names/backends. Binding metadata
    # supports provider-specific field names without per-tool branches.
    for name, value in arguments.items():
        binding = tool.argument_bindings.get(name, name)
        if binding.endswith("_ref") and value is not None:
            keys.add("resource:" + content_hash(value))
    return sorted(keys)
