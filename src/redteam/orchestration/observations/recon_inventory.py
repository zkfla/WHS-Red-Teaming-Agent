"""Record DNS, advertised routes and TCP observations without granting scope."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Any

from redteam.attack_graph.identity import node_id_for_target, service_node_id
from redteam.core.contracts.target import AnyTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.observations.mapper import EdgeMutation, GraphMutation, NodeMutation


def map_recon_inventory(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    if not isinstance(target, HostTarget):
        return GraphMutation()
    output = result.output
    if "host" in output and output["host"] != target.host:
        return GraphMutation()
    host_id = node_id_for_target(target)
    facts: dict[str, Any] = {}
    nodes: list[NodeMutation] = []
    edges: list[EdgeMutation] = []
    if result.tool_id == "recon.dns_resolve":
        addresses = output.get("addresses")
        if not isinstance(addresses, list):
            return GraphMutation()
        valid = []
        for address in addresses[:256]:
            if not isinstance(address, str):
                continue
            try:
                valid.append(str(ip_address(address)))
            except ValueError:
                continue
        if valid:
            facts["resolved_addresses"] = sorted(set(valid))
    elif result.tool_id in ("recon.openapi_inventory", "recon.html_inventory"):
        key = "operations" if result.tool_id == "recon.openapi_inventory" else "references"
        entries = output.get(key)
        if not isinstance(entries, list):
            return GraphMutation()
        candidates = []
        for entry in entries[:200]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if (
                not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//") or len(path) > 512
                or any(char in path for char in ("?", "#", "\\"))
                or any(ord(char) < 32 for char in path)
            ):
                continue
            candidate = {"path": path}
            if key == "operations":
                method = entry.get("method")
                if not isinstance(method, str) or method not in (
                    "GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE",
                ):
                    continue
                candidate["method"] = method
            candidates.append(candidate)
        if candidates:
            facts[f"{key}_candidates"] = candidates
            facts["inventory_observation_only"] = True
    elif result.tool_id == "recon.port_scan":
        if output.get("engine_exit_success") is not True:
            return GraphMutation()
        ports = output.get("ports")
        if not isinstance(ports, list):
            return GraphMutation()
        observations = []
        for item in ports[:16]:
            if not isinstance(item, dict):
                continue
            port, protocol, state = item.get("port"), item.get("protocol"), item.get("state")
            if (
                type(port) is not int or not 1 <= port <= 65535
                or protocol != "tcp" or state not in ("open", "closed", "filtered", "unknown")
            ):
                continue
            observations.append({"port": port, "protocol": protocol, "state": state})
            if state == "open":
                service_id = service_node_id(f"tcp:{port}", host=target.host)
                nodes.append(NodeMutation(
                    GraphNodeType.SERVICE, service_id, attrs={"port_open": True},
                    facts={"host": target.host, "port": port, "protocol": protocol},
                    labels=["discovered-by:recon.port_scan"],
                ))
                edges.append(EdgeMutation(GraphEdgeType.HOSTS, host_id, service_id))
        if observations:
            facts["port_observations"] = observations
    if facts:
        nodes.insert(0, NodeMutation(
            GraphNodeType.HOST, host_id, facts=facts,
            labels=[f"observed-by:{result.tool_id}"],
        ))
    return GraphMutation(nodes=nodes, edges=edges)
