"""Build bounded authentication follow-ups from stored anonymous HTTP observations."""

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
    PlaybookCandidateProvider,
)
from redteam.orchestration.authentication import AuthenticationCheck, AuthSession, request_key
from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals


class WebCandidateProvider:
    async def propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
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
            latest = {}
            for item in facts.get("observations", []):
                if item.get("kind") != "http_response":
                    continue
                latest[request_key(item["method"], item["path"], item.get("session_ref"))] = item
            for item in latest.values():
                if item.get("session_ref") is not None or item["status_code"] != 401:
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
                    key = request_key("GET", item["path"], session.session_ref)
                    if key in latest:
                        continue
                    candidates.append(
                        CandidateDraft(
                            agent_id=spec.agent_id,
                            plan_step=PlanStep(
                                step_id=f"auth-check:{node.id}:{key}",
                                capability="web",
                                operation="http_request",
                                tool_id=ToolId("web.http_request"),
                                compatible_tool_ids=[ToolId("web.http_request")],
                                assessment_kind="observation",
                                authentication_check=AuthenticationCheck(
                                    baseline_evidence_id=EvidenceId(item["evidence_id"]),
                                    session_ref=session.session_ref,
                                    expected_access=session.expected_access,
                                ),
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
                                reason="Compare anonymous 401 with an operator-registered session",
                                hypothesis=HypothesisDraft(
                                    description=(
                                        f"Registered session {session.session_ref} may "
                                        f"access {item['path']} after anonymous 401"
                                    ),
                                    violation_if_confirmed=session.expected_access == "deny",
                                    metadata={
                                        "kind": "authentication",
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
        return CandidateBatch(candidates=candidates, diagnostics=diagnostics)
