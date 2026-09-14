"""Core: Source of Truth for domain contracts and port interfaces.

Dependency rule: ``core`` MUST NOT import from any other ``redteam.*``
subpackage (orchestration, attack_graph, event_bus, control, evidence,
capability, llm, app) and MUST NOT import framework-specific libraries
(langgraph, litellm, mcp client libs, docker SDKs, ...). Every other
module imports from here, never the other way around.
"""
