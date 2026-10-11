"""End to end: one HOST in scope -> explicit model runs Recon -> Attack Graph
gains SERVICE/ENDPOINT -> explicit model runs Web/API with real
``target_ref``/``path`` arguments -> real ``web.http_request``-shaped
tool is invoked.

Migration: retired the auto-planner assumption (no implicit frontier).
The plan is supplied by an explicit scripted model (harness route answers);
automatic rule/planner restoration and execution-guard monkeypatches are
not used. Preserved contract: graph-derived required fields, both roles
act, web call really happens.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import EngagementId, ToolId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.native_local_tools import (
    GOAL_SETTLE_TOOL_ID,
    TASK_SETTLE_TOOL_ID,
    goal_settle_descriptor,
    goal_settle_handler,
    task_settle_descriptor,
    task_settle_handler,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.target_binding import network_identity
from tests.fakes import DummyToolGateway, build_test_application
from tests.integration import _engine_llm_harness as h

pytestmark = pytest.mark.asyncio

_HTTP_PROBE_OUTPUT = {
    "target_ref": "a.local",
    "status_code": 200,
    "body_bytes": 10,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
    "scheme": "http",
    "port": 80,
}

# Verified directly against `redteam.capability.servers.web_http`'s
# MCP-declared input_schema for `http_request` -- not assumed.
_REAL_WEB_HTTP_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "target_ref": {"type": "string"},
        "path": {"type": "string"},
        "method": {"type": "string", "default": "GET"},
    },
    "required": ["target_ref", "path"],
}


def _native_tool_registry() -> LocalToolRegistry:
    registry = LocalToolRegistry()
    registry.register(task_settle_descriptor(), task_settle_handler)
    registry.register(goal_settle_descriptor(), goal_settle_handler)
    return registry


def _exposed_names(tools: list[Any]) -> set[str]:
    return {str(getattr(spec, "name", "")) for spec in tools}


def _remote_executed_evidence(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if message.get("role") != "tool" or not message.get("tool_call_id"):
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if not isinstance(body, dict) or body.get("ok") is not True:
            continue
        if body.get("executed") is not True:
            continue
        for identity in body.get("evidence_ids") or []:
            text = str(identity)
            if text and text not in found:
                found.append(text)
    return found


def _tool_result(messages: list[dict[str, Any]], call_id: str) -> dict[str, Any] | None:
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return None


async def _observed_endpoint_info(graph: Any) -> dict[str, Any]:
    """Real recon observation from the source-backed graph, not a guessed address.

    Reads ENDPOINT nodes written by ``recon.http_probe`` mapping and returns the
    exact execution binding the native layer expects: pinned ``target_ref``
    authority (``host:port`` when the observation carries a port), concrete
    ``path``, plus host/port/scheme for URL assembly. Uses the product's own
    ``EndpointTarget``/``network_identity`` so the derived ``target_ref`` matches
    ``target_aliases`` exactly.
    """
    assert graph is not None, "harness needs the source-backed graph to derive followup inputs"
    nodes = await graph.list_nodes()
    endpoints: list[Any] = []
    for node in nodes:
        if getattr(node, "type", None) != GraphNodeType.ENDPOINT:
            continue
        facts = facts_of(getattr(node, "properties", {}) or {})
        host = facts.get("host")
        if not isinstance(host, str) or not host:
            continue
        endpoints.append(node)
    assert len(endpoints) == 1, (
        "harness expects the single fixture endpoint from the recon probe, "
        f"found {len(endpoints)}"
    )
    facts = facts_of(getattr(endpoints[0], "properties", {}) or {})
    host = facts.get("host")
    assert isinstance(host, str) and host, "observed endpoint must carry a concrete host"
    raw_port = facts.get("port")
    assert type(raw_port) is int and 1 <= raw_port <= 65535, (
        "observed endpoint must carry a concrete port"
    )
    port = raw_port
    raw_path = facts.get("path")
    assert (
        isinstance(raw_path, str) and raw_path.startswith("/") and len(raw_path) <= 512
    ), "observed endpoint must carry a concrete absolute path"
    observed = facts.get("observed_target")
    assert isinstance(observed, str) and observed == raw_path, (
        "observed endpoint must carry the concrete observed path"
    )
    path = raw_path
    raw_scheme = facts.get("scheme")
    assert raw_scheme in {"http", "https"}, "observed endpoint must carry a concrete scheme"
    scheme = raw_scheme
    target = EndpointTarget(host=host, port=port, path=path)
    identity = network_identity(target)
    authority = identity.authority if identity is not None else host
    return {
        "target_ref": authority,
        "path": target.path,
        "port": target.port,
        "host": host,
        "scheme": scheme,
    }


def _endpoint_url(info: dict[str, Any]) -> str:
    scheme = info.get("scheme")
    host = info.get("host")
    port = info.get("port")
    path = info.get("path")
    assert scheme in {"http", "https"}, "endpoint info must carry a concrete scheme"
    assert isinstance(host, str) and host, "endpoint info must carry a concrete host"
    assert type(port) is int and 1 <= port <= 65535, "endpoint info must carry a concrete port"
    assert isinstance(path, str) and path.startswith("/"), (
        "endpoint info must carry a concrete absolute path"
    )
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"


def _bind_followup_args(tool_id: str, spec: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Keep operation params from the spec; take address fields from the observed endpoint."""
    if tool_id == "web.http_request":
        args: dict[str, Any] = {"target_ref": info["target_ref"], "path": info["path"]}
        if "method" in spec:
            args["method"] = spec["method"]
        return args
    if tool_id == "web.proxy_list_flows":
        return {"target_ref": info["target_ref"], "limit": spec.get("limit", 50)}
    if tool_id == "web.nuclei_response_check":
        return {
            "target_ref": info["target_ref"],
            "path": info["path"],
            "check_id": spec["check_id"],
        }
    if tool_id == "web.browser_navigate":
        return {"url": _endpoint_url(info)}
    return dict(spec)


class _HandoffNativeBase:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reviews: list[str] = []
        self.turns = 0
        self._task_call: str | None = None
        self._goal_call: str | None = None
        self._graph: Any = None
        self._evidence: Any = None

    @staticmethod
    def _route_key(schema: dict[str, Any]) -> str:
        props = schema.get("properties", {})
        if "criteria" in props:
            return "goal_completion_review"
        if "claims" in props:
            return "task_completion_review"
        if "claim_supported" in props:
            return "analysis_verify"
        return "unknown"

    async def complete(self, *, messages: Any, system: Any = None, **_: Any) -> str:
        raise AssertionError("native loop must not use free-text completion")

    async def complete_structured(
        self, *, messages: Any, response_schema: dict[str, Any], system: Any = None, **_: Any
    ) -> dict[str, Any]:
        route = self._route_key(response_schema)
        self.calls.append({"route": route})
        self.reviews.append(route)
        if route == "analysis_verify":
            return h.meaning("success", True)
        if route == "task_completion_review":
            payload = json.loads(messages[0]["content"])
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "task review needs the verified executed call evidence"
            return {
                "accepted": True,
                "reason": "scripted semantic accept",
                "claims": [
                    {
                        "description": "handoff tool result observed",
                        "kind": "observation",
                        "evidence_ids": list(receipts),
                        "target_node_ids": [],
                        "principal_refs": [],
                    }
                ],
            }
        if route == "goal_completion_review":
            payload = json.loads(messages[0]["content"])
            criteria = payload.get("mission", {}).get("success_criteria", [])
            receipts = list(payload.get("verified_source_ids", []))
            assert receipts, "goal review needs the verified executed call evidence"
            return h.goal_accept(
                [{"criterion": c, "met": True, "evidence_ids": list(receipts)} for c in criteria]
            )
        raise AssertionError(f"legacy strategy route is not part of the native loop: {route!r}")


class _HandoffLLM(_HandoffNativeBase):
    """Native model: recon then web, then task/goal settlement."""

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        recon = exposed_tool_name("recon.http_probe")
        web = exposed_tool_name("web.http_request")
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert recon in names, "native model needs the recon probe tool"
        assert web in names, "native model needs the web request tool"
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns == 1:
            self.calls.append({"turn": self.turns, "tool_calls": [recon]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-recon-1",
                        name=recon,
                        arguments={"target_ref": "a.local"},
                    ),
                ),
                text="",
            )
        if self.turns == 2:
            assert evidenced, "web followup needs the recon probe evidence"
            # Derive the real followup inputs from the source-backed graph instead
            # of repeating the scripted target string: the observed endpoint carries
            # the pinned authority the execution binding requires.
            info = await _observed_endpoint_info(self._graph)
            self.calls.append({"turn": self.turns, "tool_calls": [web]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id="call-web-1",
                        name=web,
                        arguments={"target_ref": info["target_ref"], "path": info["path"]},
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "recon and web evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-goal-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "handoff evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


def _engagement(*hosts: str) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_handoff"),
        name="handoff",
        scope=EngagementScope(allowed_hosts=list(hosts)),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=60),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


class _CapabilityFollowupLLM(_HandoffNativeBase):
    """Native fixture for additional tools; explicit calls, no rule execution."""

    def __init__(self, followups):
        super().__init__()
        self.followups = list(followups)

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any],
        system: str | None = None,
        **_: Any,
    ) -> NativeCompletion:
        names = _exposed_names(tools)
        task_tool = exposed_tool_name(TASK_SETTLE_TOOL_ID)
        goal_tool = exposed_tool_name(GOAL_SETTLE_TOOL_ID)
        assert task_tool in names, "native model needs the task settlement helper"
        assert goal_tool in names, "native model needs the goal settlement helper"
        plan: list[tuple[str, dict[str, Any]]] = [("recon.http_probe", {"target_ref": "a.local"})]
        plan.extend([(tool, dict(args)) for (tool, _op, args) in self.followups])
        for tool_id, _args in plan:
            assert exposed_tool_name(tool_id) in names, f"native model needs {tool_id}"
        self.turns += 1
        evidenced = _remote_executed_evidence(messages)
        if self.turns <= len(plan):
            tool_id, args = plan[self.turns - 1]
            if tool_id != "recon.http_probe":
                assert evidenced, f"{tool_id} followup needs the recon probe evidence"
                info = await _observed_endpoint_info(self._graph)
                args = _bind_followup_args(tool_id, args, info)
            exposed = exposed_tool_name(tool_id)
            self.calls.append({"turn": self.turns, "tool_calls": [exposed]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=f"call-followup-{self.turns}",
                        name=exposed,
                        arguments=dict(args),
                    ),
                ),
                text="",
            )
        if self._task_call is None:
            assert evidenced, "task settlement needs the executed call evidence"
            self._task_call = "call-task-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [task_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._task_call,
                        name=task_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "followup evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        seen = _tool_result(messages, self._task_call)
        if seen is None or seen.get("ok") is not True:
            return NativeCompletion(tool_calls=(), text="")
        if self._goal_call is None:
            self._goal_call = "call-goal-settle"
            self.calls.append({"turn": self.turns, "tool_calls": [goal_tool]})
            return NativeCompletion(
                tool_calls=(
                    NativeToolCall(
                        call_id=self._goal_call,
                        name=goal_tool,
                        arguments={
                            "outcome": "success",
                            "reason": "followup evidence ready",
                            "evidence_ids": evidenced,
                        },
                    ),
                ),
                text="",
            )
        return NativeCompletion(tool_calls=(), text="")


async def _run_capability_followups(gateway, followups):
    llm = _CapabilityFollowupLLM(followups)
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=30, decision_max_attempts=6),
    )
    deps.local_tools.register(task_settle_descriptor(), task_settle_handler)
    deps.local_tools.register(goal_settle_descriptor(), goal_settle_handler)
    llm._graph, llm._evidence = deps.graph_store, deps.evidence_store
    return await run_engagement(_engagement("a.local"), deps=deps)


@pytest.mark.parametrize("with_browser", [False, True])
async def test_recon_discovery_unlocks_a_real_web_http_request_call(with_browser: bool) -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="test",
        tool_name="http_probe",
        operations=["service_discovery", "endpoint_discovery"],
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    # Native binding resolves the execution target from arguments plus the
    # descriptor's declared kinds (the legacy path supplied explicit node
    # ids, so these doubles never needed kinds). The handoff inspects the
    # discovered endpoint, so the web double declares the endpoint kind.
    web_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="test",
        tool_name="http_request",
        operations=["http_request"],
        input_schema=_REAL_WEB_HTTP_REQUEST_SCHEMA,
        capability_tags=["web", "api"],
        target_types=["endpoint"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    tools = [recon_tool, web_tool]
    if with_browser:
        tools.append(
            ToolDescriptor(
                tool_id=ToolId("web.browser_navigate"),
                server_id="browser",
                tool_name="browser_navigate",
                operations=["http_request"],
                capability_tags=["web"],
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.HEALTHY,
            )
        )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=tools)
    llm = _HandoffLLM()
    deps = build_test_application(
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=llm,
        settings=OrchestrationSettings(max_steps=20, decision_max_attempts=6),
    )
    deps.local_tools.register(task_settle_descriptor(), task_settle_handler)
    deps.local_tools.register(goal_settle_descriptor(), goal_settle_handler)
    llm._graph = deps.graph_store
    llm._evidence = deps.evidence_store

    final = await run_engagement(_engagement("a.local"), deps=deps)

    web_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.http_request"
    ]
    assert web_invocations, "Web/API Agent never called a real web.http_request-shaped tool"

    # The structural point of this whole fix: real, graph-derived values,
    # not empty/guessed arguments -- this would have raised a schema
    # validation error at candidate-compile time before this fix, so
    # simply reaching here already proves required fields were filled.
    resolved = web_invocations[0].arguments
    # Graph-derived pinned authority from EndpointTarget(host, port, path): the
    # observed endpoint is endpoint:a.local:80 GET / so the execution binding
    # requires the pinned authority, not the bare scope host.
    assert resolved["target_ref"] == "a.local:80"
    assert resolved["path"] == "/"

    actions_by_agent = final.actions_by_agent
    assert actions_by_agent.get("recon-agent", 0) >= 1
    assert actions_by_agent.get("web-api-agent", 0) >= 1


async def test_browser_navigate_is_selected_when_http_mcp_is_unavailable() -> None:
    """A live Browser MCP can serve the same intent when HTTPX is absent.

    The URL is assembled only from Recon's observed scheme/host/path.  This
    proves the fallback is an Agent candidate, not a direct browser call.
    """

    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        operations=["service_discovery", "endpoint_discovery"],
        input_schema={
            "type": "object",
            "properties": {"target_ref": {"type": "string"}, "path": {"type": "string"}},
            "required": ["target_ref"],
        },
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    browser_tool = ToolDescriptor(
        tool_id=ToolId("web.browser_navigate"),
        server_id="browser",
        tool_name="browser_navigate",
        operations=["http_request"],
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, browser_tool])
    await _run_capability_followups(
        gateway,
        [
            # URL is derived inside the harness from the observed endpoint
            # (scheme/host/path); the spec carries intent only, no address guess.
            ("web.browser_navigate", "http_request", {}),
        ],
    )

    browser_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.browser_navigate"
    ]
    assert browser_invocations
    # Real observed URL from the recon endpoint (http + a.local + /).
    assert browser_invocations[0].arguments == {"url": "http://a.local/"}


async def test_proxy_history_is_selected_as_a_safe_observation_followup() -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        operations=["service_discovery", "endpoint_discovery"],
        input_schema={"type": "object", "properties": {"target_ref": {"type": "string"}}},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    proxy_tool = ToolDescriptor(
        tool_id=ToolId("web.proxy_list_flows"),
        server_id="proxy",
        tool_name="proxy_list_flows",
        operations=["response_analysis"],
        target_types=["endpoint"],
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["target_ref"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.MEDIUM,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(output=_HTTP_PROBE_OUTPUT, tools=[recon_tool, proxy_tool])
    await _run_capability_followups(
        gateway,
        [
            # target_ref is derived inside the harness from the observed endpoint;
            # the spec carries the operation param only, no address guess.
            ("web.proxy_list_flows", "response_analysis", {"limit": 50}),
        ],
    )

    proxy_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.proxy_list_flows"
    ]
    assert proxy_invocations
    assert proxy_invocations[0].arguments == {"target_ref": "a.local:80", "limit": 50}


async def test_nuclei_passive_check_is_selected_after_http_observation() -> None:
    recon_tool = ToolDescriptor(
        tool_id=ToolId("recon.http_probe"),
        server_id="recon",
        tool_name="http_probe",
        operations=["service_discovery", "endpoint_discovery"],
        input_schema={"type": "object", "properties": {"target_ref": {"type": "string"}}},
        capability_tags=["recon"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    http_tool = ToolDescriptor(
        tool_id=ToolId("web.http_request"),
        server_id="http",
        tool_name="http_request",
        operations=["http_request"],
        input_schema=_REAL_WEB_HTTP_REQUEST_SCHEMA,
        capability_tags=["web", "api"],
        target_types=["endpoint"],
        risk_level=RiskLevel.LOW,
        health_status=ToolHealthStatus.HEALTHY,
    )
    nuclei_tool = ToolDescriptor(
        tool_id=ToolId("web.nuclei_response_check"),
        server_id="nuclei",
        tool_name="nuclei_response_check",
        operations=["response_analysis"],
        target_types=["endpoint"],
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "path": {"type": "string"},
                "check_id": {"type": "string"},
            },
            "required": ["target_ref", "path", "check_id"],
        },
        capability_tags=["web", "api"],
        risk_level=RiskLevel.HIGH,
        health_status=ToolHealthStatus.HEALTHY,
    )
    gateway = DummyToolGateway(
        output=_HTTP_PROBE_OUTPUT,
        tools=[recon_tool, http_tool, nuclei_tool],
    )
    await _run_capability_followups(
        gateway,
        [
            # Address fields are derived inside the harness from the observed
            # endpoint; specs carry operation params only, no address guess.
            ("web.http_request", "http_request", {}),
            (
                "web.nuclei_response_check",
                "response_analysis",
                {"check_id": "missing-csp"},
            ),
        ],
    )

    nuclei_invocations = [
        invocation
        for invocation in gateway.invocations
        if str(invocation.tool_id) == "web.nuclei_response_check"
    ]
    assert nuclei_invocations
    assert nuclei_invocations[0].arguments["check_id"] == "missing-csp"
    assert nuclei_invocations[0].arguments["target_ref"] == "a.local:80"
    assert nuclei_invocations[0].arguments["path"] == "/"
