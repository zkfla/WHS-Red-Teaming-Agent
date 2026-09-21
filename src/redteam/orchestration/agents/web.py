"""Graph-driven, bounded multi-step follow-ups for discovered HTTP surfaces.

Everything here is derived from facts the graph actually holds, which in
turn came from a real ToolResult. What the current web.http_request
contract exposes is exactly: status_code, body_bytes, body_sha256,
redirect, security_headers_present (four booleans), body_omitted,
target_ref and session_ref -- see
redteam.capability.servers.network.CapturedResponse.summary.

It does NOT expose Location, WWW-Authenticate, Set-Cookie or any raw
header or body value. So this provider never materializes a redirect
target, an auth challenge scheme/realm or a cookie name: those facts do
not exist here, and inventing them would put unobserved claims into the
Attack Graph. A redirect is recorded as "a 3xx was observed" and stops
there. Likewise the absence of a security header is an observation, not a
vulnerability, so no candidate and no finding is derived from it.
"""

from __future__ import annotations

from redteam.attack_graph.identity import host_node_id
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.target import EndpointTarget
from redteam.core.enums import GraphNodeType
from redteam.core.ids import EvidenceId, ToolId
from redteam.orchestration.agents.base import AgentSpec, HypothesisDraft, PlanStep
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    CandidateDiagnostic,
    GraphCandidateContext,
    PlaybookCandidateProvider,
)
from redteam.orchestration.authentication import AuthenticationCheck, AuthSession, request_key
from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals

WEB_HTTP_REQUEST = ToolId("web.http_request")
WEB_COMPARE_ANONYMOUS = ToolId("web.http_compare_anonymous")

_HTTP_REQUEST_OPERATION = "http_request"
#: docs/contracts/actions.md requires a tool-independent intent. Comparing
#: anonymous against registered-session access is an authentication check.
_AUTH_ATTEMPT_OPERATION = "auth_attempt"

#: HTTP status semantics, from RFC 9110. Deriving these from an *observed*
#: status code is reading the protocol definition of a measured value, not
#: inventing evidence; the raw header that would prove the mechanism was
#: never returned by the tool, so no mechanism is ever named.
_UNAUTHENTICATED_STATUS = 401
_FORBIDDEN_STATUS = 403


class WebCandidateProvider:
    #: Canonical actions this provider can propose beyond its static
    #: playbook. The runtime checks these against the live Tool Catalog and
    #: reports the result through GraphCandidateContext.can_execute, so a
    #: tool-specific follow-up is only offered when it can actually run.
    declared_operations = (
        ("web", _HTTP_REQUEST_OPERATION),
        ("web", _AUTH_ATTEMPT_OPERATION),
    )

    async def propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> CandidateBatch:
        return await self._build(spec, view, observation, step=step, context=None)

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        """The authoritative path: plan from the graph, not just the playbook."""

        return await self._build(
            spec,
            context.view,
            context.observation,
            step=context.step,
            context=context,
        )

    async def _build(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
        context: GraphCandidateContext | None,
    ) -> CandidateBatch:
        candidates = await PlaybookCandidateProvider().propose(spec, view, observation, step=step)
        diagnostics: list[CandidateDiagnostic] = []
        for node in await view.list_nodes():
            if node.type is not GraphNodeType.ENDPOINT:
                continue
            facts = facts_of(node.properties)
            host = facts.get("host")
            if not isinstance(host, str):
                continue
            host_node = await view.get_node(host_node_id(host))
            host_facts = facts_of(host_node.properties) if host_node else {}
            sessions = [
                AuthSession.model_validate(item)
                for item in host_facts.get("auth_session_config", {}).get("sessions", [])
            ]
            # A normalized endpoint may contain several concrete paths and sessions.
            latest: dict[str, dict] = {}
            for item in facts.get("observations", []):
                if item.get("kind") != "http_response":
                    continue
                latest[request_key(item["method"], item["path"], item.get("session_ref"))] = item
            shared = {
                "node": node,
                "facts": facts,
                "host": host,
                "host_facts": host_facts,
                "sessions": sessions,
                "latest": latest,
                "spec": spec,
            }
            candidates.extend(
                self._authentication_followups(diagnostics=diagnostics, **shared)
            )
            candidates.extend(
                self._session_baseline_followups(
                    context=context, diagnostics=diagnostics, **shared
                )
            )
            candidates.extend(
                self._authorization_followups(diagnostics=diagnostics, **shared)
            )
        return CandidateBatch(candidates=candidates, diagnostics=diagnostics)

    def _authentication_followups(
        self,
        *,
        node: object,
        facts: dict,
        host: str,
        host_facts: dict,
        sessions: list[AuthSession],
        latest: dict[str, dict],
        spec: AgentSpec,
        diagnostics: list[CandidateDiagnostic],
    ) -> list[CandidateDraft]:
        """Anonymous 401 -> ask the same question with a registered session.

        Only the status code and the operator-registered session list decide
        this; nothing is read from headers the tool does not return.
        """

        drafts: list[CandidateDraft] = []
        for item in latest.values():
            if item.get("session_ref") is not None or item["status_code"] != _UNAUTHENTICATED_STATUS:
                continue
            if item["method"] != "GET" or not item.get("evidence_id"):
                continue
            eligible = [session for session in sessions if item["path"] in session.paths]
            if not eligible:
                diagnostics.append(
                    CandidateDiagnostic(
                        target_node_id=node.id,
                        reason="no_registered_session_for_path",
                        evidence_id=item["evidence_id"],
                    )
                )
            for session in eligible:
                if request_key("GET", item["path"], session.session_ref) in latest:
                    continue
                drafts.append(
                    self._session_candidate(
                        spec=spec,
                        node=node,
                        facts=facts,
                        host=host,
                        host_facts=host_facts,
                        session=session,
                        baseline=item,
                    )
                )
        return drafts

    def _session_baseline_followups(
        self,
        *,
        context: GraphCandidateContext | None,
        node: object,
        facts: dict,
        host: str,
        host_facts: dict,
        sessions: list[AuthSession],
        latest: dict[str, dict],
        spec: AgentSpec,
        diagnostics: list[CandidateDiagnostic],
    ) -> list[CandidateDraft]:
        """Two bounded, evidence-first follow-ups the 401 case cannot cover.

        1. A session request that was refused (401/403) while the operator
           declared that session should be allowed is only interpretable
           against an anonymous baseline. If none was recorded, ask for it
           -- that is the missing half of an existing comparison, not a new
           guess about the target.
        2. When the catalog proves web.http_compare_anonymous is bound to
           the auth_attempt action, the anonymous/session pair can be
           measured in one real tool call instead of two probes.
        """

        drafts: list[CandidateDraft] = []
        for item in latest.values():
            session_ref = item.get("session_ref")
            if session_ref is None or not item.get("evidence_id"):
                continue
            if item["status_code"] not in {_UNAUTHENTICATED_STATUS, _FORBIDDEN_STATUS}:
                continue
            if item["method"] != "GET":
                continue
            session = next(
                (entry for entry in sessions if entry.session_ref == session_ref), None
            )
            if session is None or item["path"] not in session.paths:
                continue
            anonymous_key = request_key("GET", item["path"], None)
            if anonymous_key in latest:
                continue
            if context is not None and context.can_execute("web", _AUTH_ATTEMPT_OPERATION):
                drafts.append(
                    CandidateDraft(
                        agent_id=spec.agent_id,
                        plan_step=PlanStep(
                            step_id=f"compare-anonymous:{node.id}:{anonymous_key}",
                            capability="web",
                            operation=_AUTH_ATTEMPT_OPERATION,
                            tool_id=WEB_COMPARE_ANONYMOUS,
                            compatible_tool_ids=[WEB_COMPARE_ANONYMOUS],
                            assessment_kind="observation",
                            target=EndpointTarget(
                                host=host,
                                path=item["path"],
                                port=facts.get("port"),
                                graph_node_id=node.id,
                            ),
                            arguments={
                                "target_ref": host_facts.get("target_ref", host),
                                "path": item["path"],
                                "session_ref": session.session_ref,
                            },
                            reason=(
                                "Measure anonymous and registered-session access in one "
                                "catalog-bound comparison"
                            ),
                            hypothesis=HypothesisDraft(
                                description=(
                                    f"Anonymous and session {session.session_ref} access to "
                                    f"{item['path']} differ in a way worth recording"
                                ),
                                metadata={
                                    "kind": "authentication_comparison",
                                    "baseline_evidence_id": item["evidence_id"],
                                },
                            ),
                        ),
                        target_node_ids=[node.id],
                        conflict_keys=[f"target:{node.id}"],
                        signals=PrioritySignals(
                            source_refs=[f"evidence:{item['evidence_id']}"]
                        ),
                    )
                )
                continue
            drafts.append(
                CandidateDraft(
                    agent_id=spec.agent_id,
                    plan_step=PlanStep(
                        step_id=f"anonymous-baseline:{node.id}:{anonymous_key}",
                        capability="web",
                        operation=_HTTP_REQUEST_OPERATION,
                        tool_id=WEB_HTTP_REQUEST,
                        compatible_tool_ids=[WEB_HTTP_REQUEST],
                        assessment_kind="observation",
                        target=EndpointTarget(
                            host=host,
                            path=item["path"],
                            port=facts.get("port"),
                            graph_node_id=node.id,
                        ),
                        arguments={
                            "target_ref": host_facts.get("target_ref", host),
                            "path": item["path"],
                            "method": "GET",
                        },
                        reason=(
                            "Record the anonymous baseline a refused session request "
                            "has to be compared against"
                        ),
                        hypothesis=HypothesisDraft(
                            description=(
                                f"{item['path']} answers differently without a session "
                                f"than with {session.session_ref}"
                            ),
                            metadata={
                                "kind": "authentication_baseline",
                                "baseline_evidence_id": item["evidence_id"],
                            },
                        ),
                    ),
                    target_node_ids=[node.id],
                    conflict_keys=[f"target:{node.id}"],
                    signals=PrioritySignals(
                        source_refs=[f"evidence:{item['evidence_id']}"]
                    ),
                )
            )
        return drafts

    def _authorization_followups(
        self,
        *,
        node: object,
        facts: dict,
        host: str,
        host_facts: dict,
        sessions: list[AuthSession],
        latest: dict[str, dict],
        spec: AgentSpec,
        diagnostics: list[CandidateDiagnostic],
    ) -> list[CandidateDraft]:
        """A 403 for one registered identity -> ask the same path as another.

        A permission difference between two *operator-provisioned* test
        principals is worth measuring. It is still an observation: the
        status alone does not establish broken authorization, so the
        hypothesis is recorded without claiming a violation.

        Deliberately requires a *session-bound* 403. An anonymous 403 is
        fully explained by "no credentials were presented", so deriving a
        permission claim from it would be reading a meaning into the status
        that the observation does not carry; that case stays an observation
        and produces no follow-up.
        """

        drafts: list[CandidateDraft] = []
        for item in latest.values():
            if item["status_code"] != _FORBIDDEN_STATUS or item["method"] != "GET":
                continue
            if not item.get("evidence_id"):
                continue
            refused_ref = item.get("session_ref")
            if refused_ref is None:
                continue
            alternatives = [
                session
                for session in sessions
                if session.session_ref != refused_ref
                and item["path"] in session.paths
                and request_key("GET", item["path"], session.session_ref) not in latest
            ]
            if not alternatives:
                diagnostics.append(
                    CandidateDiagnostic(
                        target_node_id=node.id,
                        reason="no_alternate_registered_session_for_path",
                        evidence_id=item["evidence_id"],
                    )
                )
            for session in alternatives:
                drafts.append(
                    CandidateDraft(
                        agent_id=spec.agent_id,
                        plan_step=PlanStep(
                            step_id=(
                                f"authorization-check:{node.id}:"
                                f"{request_key('GET', item['path'], session.session_ref)}"
                            ),
                            capability="web",
                            operation=_HTTP_REQUEST_OPERATION,
                            tool_id=WEB_HTTP_REQUEST,
                            compatible_tool_ids=[WEB_HTTP_REQUEST],
                            assessment_kind="observation",
                            target=EndpointTarget(
                                host=host,
                                path=item["path"],
                                port=facts.get("port"),
                                graph_node_id=node.id,
                            ),
                            arguments={
                                "target_ref": host_facts.get("target_ref", host),
                                "path": item["path"],
                                "method": "GET",
                                "session_ref": session.session_ref,
                            },
                            reason=(
                                "Compare a 403 with a different operator-registered "
                                "identity"
                            ),
                            hypothesis=HypothesisDraft(
                                description=(
                                    f"{item['path']} is not uniformly refused across "
                                    f"registered identities"
                                ),
                                metadata={
                                    "kind": "authorization_comparison",
                                    "baseline_evidence_id": item["evidence_id"],
                                },
                            ),
                        ),
                        target_node_ids=[node.id],
                        conflict_keys=[f"target:{node.id}"],
                        signals=PrioritySignals(
                            source_refs=[f"evidence:{item['evidence_id']}"]
                        ),
                    )
                )
        return drafts

    def _session_candidate(
        self,
        *,
        spec: AgentSpec,
        node: object,
        facts: dict,
        host: str,
        host_facts: dict,
        session: AuthSession,
        baseline: dict,
    ) -> CandidateDraft:
        key = request_key("GET", baseline["path"], session.session_ref)
        return CandidateDraft(
            agent_id=spec.agent_id,
            plan_step=PlanStep(
                step_id=f"auth-check:{node.id}:{key}",
                capability="web",
                operation=_HTTP_REQUEST_OPERATION,
                tool_id=WEB_HTTP_REQUEST,
                compatible_tool_ids=[WEB_HTTP_REQUEST],
                assessment_kind="observation",
                authentication_check=AuthenticationCheck(
                    baseline_evidence_id=EvidenceId(baseline["evidence_id"]),
                    session_ref=session.session_ref,
                    expected_access=session.expected_access,
                ),
                target=EndpointTarget(
                    host=host,
                    path=baseline["path"],
                    port=facts.get("port"),
                    graph_node_id=node.id,
                ),
                arguments={
                    "target_ref": host_facts.get("target_ref", host),
                    "path": baseline["path"],
                    "method": "GET",
                    "session_ref": session.session_ref,
                },
                reason="Compare anonymous 401 with an operator-registered session",
                hypothesis=HypothesisDraft(
                    description=(
                        f"Registered session {session.session_ref} may "
                        f"access {baseline['path']} after anonymous 401"
                    ),
                    violation_if_confirmed=session.expected_access == "deny",
                    metadata={
                        "kind": "authentication",
                        "baseline_evidence_id": baseline["evidence_id"],
                    },
                ),
            ),
            target_node_ids=[node.id],
            conflict_keys=[f"target:{node.id}"],
            signals=PrioritySignals(source_refs=[f"evidence:{baseline['evidence_id']}"]),
        )
