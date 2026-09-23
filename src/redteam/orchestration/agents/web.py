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

from redteam.attack_graph.identity import endpoint_node_id, host_node_id
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.target import EndpointTarget
from redteam.core.enums import GraphNodeType
from redteam.core.ids import EvidenceId, HypothesisId, ToolId
from redteam.orchestration.agents.base import AgentSpec, HypothesisDraft, PlanStep
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    CandidateDiagnostic,
    DraftDrop,
    GraphCandidateContext,
    PlaybookCandidateProvider,
    endpoint_path_explicitly_disallowed,
)
from redteam.orchestration.authentication import AuthenticationCheck, AuthSession, request_key
from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals

WEB_HTTP_REQUEST = ToolId("web.http_request")
WEB_COMPARE_ANONYMOUS = ToolId("web.http_compare_anonymous")
#: Both of these only measure and record; the mappers label what they write
#: with verdict observation_only and the provider never promotes them.
WEB_COMPARE_SESSIONS = ToolId("web.http_compare_sessions")
WEB_SECURITY_POSTURE = ToolId("web.http_security_posture")

_HTTP_REQUEST_OPERATION = "http_request"
#: docs/contracts/actions.md requires a tool-independent intent. Comparing
#: anonymous against registered-session access is an authentication check.
_AUTH_ATTEMPT_OPERATION = "auth_attempt"
#: Comparing two responses, and reading a response's header/cookie flags,
#: are both analysis of an already-observed exchange rather than a new
#: request intent of their own.
_RESPONSE_ANALYSIS_OPERATION = "response_analysis"

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
        ("web", _RESPONSE_ANALYSIS_OPERATION),
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
        playbook = PlaybookCandidateProvider()
        drops: list[DraftDrop] = []
        if context is not None:
            inherited = await playbook.propose_graph(spec, context)
            candidates, drops = inherited.candidates, inherited.drops
        else:
            candidates = await playbook.propose(spec, view, observation, step=step)
        diagnostics: list[CandidateDiagnostic] = []
        nodes = await view.list_nodes()
        disallowed_ids = {
            str(node.id) for node in nodes if endpoint_path_explicitly_disallowed(node)
        }
        permitted: list[CandidateDraft] = []
        for candidate in candidates:
            if any(str(node_id) in disallowed_ids for node_id in candidate.target_node_ids):
                drops.append(DraftDrop(
                    step_id=candidate.plan_step.step_id,
                    target_node_id=str(candidate.target_node_ids[0]),
                    reason="path_not_allowlisted",
                    field="path",
                ))
            else:
                permitted.append(candidate)
        candidates = permitted
        validation_hypotheses = _access_validation_hypotheses(nodes)
        for node in nodes:
            if node.type is not GraphNodeType.ENDPOINT:
                continue
            if endpoint_path_explicitly_disallowed(node):
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
            candidates.extend(
                self._authentication_followups(
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    sessions=sessions,
                    latest=latest,
                    validation_hypothesis_id=validation_hypotheses.get(str(node.id)),
                    diagnostics=diagnostics,
                )
            )
            candidates.extend(
                self._session_baseline_followups(
                    context=context,
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    sessions=sessions,
                    latest=latest,
                    diagnostics=diagnostics,
                )
            )
            candidates.extend(
                self._authorization_followups(
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    sessions=sessions,
                    latest=latest,
                    diagnostics=diagnostics,
                )
            )
            candidates.extend(
                self._redirect_followups(
                    context=context,
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    latest=latest,
                )
            )
            candidates.extend(
                self._session_comparison_followups(
                    context=context,
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    sessions=sessions,
                    latest=latest,
                )
            )
            candidates.extend(
                self._posture_followups(
                    context=context,
                    spec=spec,
                    node=node,
                    facts=facts,
                    host=host,
                    host_facts=host_facts,
                    latest=latest,
                )
            )
        return CandidateBatch(
            candidates=candidates, diagnostics=diagnostics, drops=drops
        )

    def _redirect_followups(
        self,
        *,
        context: GraphCandidateContext | None,
        node: GraphNode,
        facts: dict,
        host: str,
        host_facts: dict,
        latest: dict[str, dict],
        spec: AgentSpec,
    ) -> list[CandidateDraft]:
        """Probe an observed same-origin redirect path once.

        The redirect target is only used when a ToolResult producer has
        already supplied a normalized path. A bare 3xx never creates a
        guessed login candidate, and query/fragment values are intentionally
        omitted by the metadata contract.
        """

        if context is not None and not context.can_execute("web", _HTTP_REQUEST_OPERATION):
            return []
        drafts: list[CandidateDraft] = []
        for item in latest.values():
            metadata = item.get("redirect_metadata")
            if not isinstance(metadata, dict) or metadata.get("detected") is not True:
                continue
            location = metadata.get("location_path")
            if (
                not isinstance(location, str)
                or not location.startswith("/")
                or location == item.get("path")
                or len(location) > 512
                or "\\" in location
                or any(ord(char) < 32 for char in location)
                or not item.get("evidence_id")
            ):
                continue
            session_ref = item.get("session_ref")
            if session_ref is not None and not any(
                entry.session_ref == session_ref for entry in self._sessions_for_host(host_facts)
            ):
                continue
            key = request_key("GET", location, session_ref)
            if key in latest:
                continue
            arguments = {
                "target_ref": host_facts.get("target_ref", host),
                "path": location,
                "method": "GET",
            }
            if isinstance(session_ref, str):
                arguments["session_ref"] = session_ref
            redirect_node_id = endpoint_node_id(
                "GET", location, host=host, port=facts.get("port")
            )
            drafts.append(
                CandidateDraft(
                    agent_id=spec.agent_id,
                    plan_step=PlanStep(
                        step_id=f"redirect-followup:{node.id}:{key}",
                        capability="web",
                        operation=_HTTP_REQUEST_OPERATION,
                        tool_id=WEB_HTTP_REQUEST,
                        compatible_tool_ids=[WEB_HTTP_REQUEST],
                        assessment_kind="observation",
                        target=EndpointTarget(
                            host=host,
                            path=location,
                            port=facts.get("port"),
                            graph_node_id=node.id,
                        ),
                        arguments=arguments,
                        reason="Probe an observed redirect target on the same host",
                        hypothesis=HypothesisDraft(
                            description=(
                                f"The observed redirect from {item['path']} leads to "
                                f"{location}"
                            ),
                            metadata={
                                "kind": "redirect_followup",
                                "source_evidence_id": item["evidence_id"],
                            },
                        ),
                    ),
                    target_node_ids=[redirect_node_id],
                    conflict_keys=[f"target:{redirect_node_id}"],
                    signals=PrioritySignals(source_refs=[f"evidence:{item['evidence_id']}"]),
                )
            )
        return drafts

    @staticmethod
    def _sessions_for_host(host_facts: dict) -> list[AuthSession]:
        return [
            AuthSession.model_validate(item)
            for item in host_facts.get("auth_session_config", {}).get("sessions", [])
        ]

    def _authentication_followups(
        self,
        *,
        node: GraphNode,
        facts: dict,
        host: str,
        host_facts: dict,
        sessions: list[AuthSession],
        latest: dict[str, dict],
        validation_hypothesis_id: HypothesisId | None,
        spec: AgentSpec,
        diagnostics: list[CandidateDiagnostic],
    ) -> list[CandidateDraft]:
        """Anonymous 401 -> ask the same question with a registered session.

        Only the status code and the operator-registered session list decide
        this; nothing is read from headers the tool does not return.
        """

        drafts: list[CandidateDraft] = []
        for item in latest.values():
            if (
                item.get("session_ref") is not None
                or item["status_code"] != _UNAUTHENTICATED_STATUS
            ):
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
                if session.expected_access == "deny" and validation_hypothesis_id is None:
                    diagnostics.append(
                        CandidateDiagnostic(
                            target_node_id=node.id,
                            reason="missing_access_validation_hypothesis",
                            evidence_id=item["evidence_id"],
                        )
                    )
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
                        validation_hypothesis_id=(
                            validation_hypothesis_id
                            if session.expected_access == "deny"
                            else None
                        ),
                    )
                )
        return drafts

    def _session_baseline_followups(
        self,
        *,
        context: GraphCandidateContext | None,
        node: GraphNode,
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
        node: GraphNode,
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
        node: GraphNode,
        facts: dict,
        host: str,
        host_facts: dict,
        session: AuthSession,
        baseline: dict,
        validation_hypothesis_id: HypothesisId | None = None,
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
                assessment_kind=(
                    "security_check" if validation_hypothesis_id is not None else "observation"
                ),
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
                existing_hypothesis_id=validation_hypothesis_id,
            ),
            target_node_ids=[node.id],
            conflict_keys=[f"target:{node.id}"],
            signals=PrioritySignals(source_refs=[f"evidence:{baseline['evidence_id']}"]),
        )

    def _session_comparison_followups(
        self,
        *,
        context: GraphCandidateContext | None,
        spec: AgentSpec,
        node: GraphNode,
        facts: dict,
        host: str,
        host_facts: dict,
        sessions: list[AuthSession],
        latest: dict[str, dict],
    ) -> list[CandidateDraft]:
        """Two identities that both answered this path -> compare them.

        The pair comes from observations, never from the registered session
        list: only identities that actually produced a response for this
        exact path are eligible. "Two sessions are configured" is not
        evidence that measuring them would tell us anything.

        The cross-check runs the other way too: an observed session_ref that
        the operator never registered is not an operator-managed identity,
        so it can never be half of the pair either. (Whether the two refs are
        genuinely distinct *principals* is not knowable here -- AuthSession
        carries no principal -- and remains the Capability tool's own check.)
        """

        if context is None or not context.can_execute(
            "web", _RESPONSE_ANALYSIS_OPERATION
        ):
            return []
        registered = {item.session_ref: set(item.paths) for item in sessions}
        observed: dict[str, set[str]] = {}
        evidences: dict[str, str] = {}
        for item in latest.values():
            session_ref = item.get("session_ref")
            path = item.get("path")
            evidence_id = item.get("evidence_id")
            if not isinstance(session_ref, str) or not session_ref:
                continue
            if not isinstance(path, str) or not path.startswith("/"):
                continue
            if not evidence_id:
                continue
            # Operator-managed only: registered, and registered for *this*
            # path. An arbitrary ref seen in a response is not an identity we
            # are allowed to treat as a test principal.
            if session_ref not in registered or path not in registered[session_ref]:
                continue
            observed.setdefault(path, set()).add(session_ref)
            evidences.setdefault(path, str(evidence_id))

        drafts: list[CandidateDraft] = []
        for path in sorted(observed):
            refs = sorted(observed[path])
            if len(refs) < 2:
                continue
            first, second = refs[0], refs[1]
            if _already_compared(facts, first, second):
                continue
            drafts.append(
                CandidateDraft(
                    agent_id=spec.agent_id,
                    plan_step=PlanStep(
                        step_id=f"compare-sessions:{node.id}:{first}:{second}",
                        capability="web",
                        operation=_RESPONSE_ANALYSIS_OPERATION,
                        tool_id=WEB_COMPARE_SESSIONS,
                        compatible_tool_ids=[WEB_COMPARE_SESSIONS],
                        assessment_kind="observation",
                        target=EndpointTarget(
                            host=host,
                            path=path,
                            port=facts.get("port"),
                            graph_node_id=node.id,
                        ),
                        arguments={
                            "target_ref": host_facts.get("target_ref", host),
                            "path": path,
                            "first_session_ref": first,
                            "second_session_ref": second,
                        },
                        reason=(
                            "Two operator-managed identities both answered this path, so "
                            "their statuses and bodies can be compared directly"
                        ),
                        hypothesis=HypothesisDraft(
                            description=(
                                f"{path} answers differently for {first} and {second}"
                            ),
                            metadata={
                                "kind": "session_comparison",
                                "first_session_ref": first,
                                "second_session_ref": second,
                            },
                        ),
                    ),
                    target_node_ids=[node.id],
                    conflict_keys=[f"target:{node.id}"],
                    signals=PrioritySignals(
                        expected_information_gain=0.7,
                        evidence_gap_reduction=0.6,
                        expected_cost=0.4,
                        source_refs=[f"evidence:{evidences[path]}"],
                    ),
                )
            )
        return drafts

    def _posture_followups(
        self,
        *,
        context: GraphCandidateContext | None,
        spec: AgentSpec,
        node: GraphNode,
        facts: dict,
        host: str,
        host_facts: dict,
        latest: dict[str, dict],
    ) -> list[CandidateDraft]:
        """Header and cookie-flag posture, once a real response exists.

        Skipped when this endpoint already has an anonymous posture
        observation: the tool would return the same flags again.
        """

        if context is None or not context.can_execute(
            "web", _RESPONSE_ANALYSIS_OPERATION
        ):
            return []
        if not latest:
            return []
        if _posture_observed(facts):
            return []
        first = sorted(latest.values(), key=lambda item: str(item.get("context_key")))[0]
        path = first.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            return []
        evidence_id = first.get("evidence_id")
        if not evidence_id:
            return []
        return [
            CandidateDraft(
                agent_id=spec.agent_id,
                plan_step=PlanStep(
                    step_id=f"security-posture:{node.id}",
                    capability="web",
                    operation=_RESPONSE_ANALYSIS_OPERATION,
                    tool_id=WEB_SECURITY_POSTURE,
                    compatible_tool_ids=[WEB_SECURITY_POSTURE],
                    assessment_kind="observation",
                    target=EndpointTarget(
                        host=host,
                        path=path,
                        port=facts.get("port"),
                        graph_node_id=node.id,
                    ),
                    arguments={
                        "target_ref": host_facts.get("target_ref", host),
                        "path": path,
                    },
                    reason=(
                        "This endpoint answered a real request, so its security-header "
                        "and cookie-flag posture can be recorded without guessing any value"
                    ),
                    hypothesis=HypothesisDraft(
                        description=(
                            f"{path} presents a header and cookie posture worth recording"
                        ),
                        confidence=0.3,
                        metadata={"kind": "security_posture"},
                    ),
                ),
                target_node_ids=[node.id],
                conflict_keys=[f"target:{node.id}"],
                signals=PrioritySignals(
                    expected_information_gain=0.4,
                    expected_cost=0.2,
                    source_refs=[f"evidence:{evidence_id}"],
                ),
            )
        ]


def _already_compared(facts: dict, first: str, second: str) -> bool:
    """Did this exact pair already produce a comparison for this endpoint?

    Read from the Mapper's own recorded fields rather than recomputing its
    context key, so the two cannot drift apart.
    """

    wanted = {first, second}
    for item in facts.get("observations", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "http_session_comparison":
            continue
        if {item.get("first_session_ref"), item.get("second_session_ref")} == wanted:
            return True
    return False


def _posture_observed(facts: dict) -> bool:
    """Is there already an anonymous posture observation for this endpoint?"""

    for item in facts.get("observations", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "http_security_posture" and item.get("session_ref") is None:
            return True
    return False


def _access_validation_hypotheses(nodes: list[GraphNode]) -> dict[str, HypothesisId]:
    """Return reusable observation questions keyed by endpoint node id.

    ``inconclusive`` means the observation did not decide the question. It is
    therefore eligible for one bounded, catalog-backed validation; confirmed
    and rejected hypotheses are deliberately ignored.
    """

    found: dict[str, HypothesisId] = {}
    for node in nodes:
        if node.type is not GraphNodeType.HYPOTHESIS:
            continue
        hypothesis = facts_of(node.properties)
        metadata = hypothesis.get("metadata")
        target_node_id = metadata.get("target_node_id") if isinstance(metadata, dict) else None
        hypothesis_id = hypothesis.get("hypothesis_id")
        if (
            attrs_of(node.properties).get("status") == "inconclusive"
            and isinstance(metadata, dict)
            and metadata.get("kind") == "web_access_validation"
            and isinstance(target_node_id, str)
            and target_node_id
            and isinstance(hypothesis_id, str)
            and hypothesis_id
        ):
            found.setdefault(target_node_id, HypothesisId(hypothesis_id))
    return found
