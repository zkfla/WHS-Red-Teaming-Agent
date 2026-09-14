"""Composition root and CLI entry point.

Every other subpackage is wired together here only. Nothing outside
``app`` should import more than one of {control, capability, evidence,
orchestration, event_bus, attack_graph, llm}'s concrete implementations
at once.
"""
