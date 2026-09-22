"""Engagement Intake Assistant: natural-language config authoring.

This package prepares an Engagement (target/scope/ROE/budget) — it never
executes one. That is ``redteam.orchestration``'s job, unchanged.

    Intake Assistant  = Engagement 준비 (collect, structure, validate)
    Orchestrator      = Engagement 수행 (plan, act, verify)

Nothing here calls ``redteam.core.ports.tool_gateway.ToolGateway`` or
``redteam.core.ports.control_plane.ControlPlane`` — it only ever produces
a ``redteam.core.config.EngagementConfig``, which the existing loader
(``redteam.core.config.load_engagement_config``) and CLI (``redteam
run``) already know how to consume unchanged.
"""
