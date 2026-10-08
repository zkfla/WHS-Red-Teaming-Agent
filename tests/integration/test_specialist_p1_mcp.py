"""Fixture reasoning + real Control/stdio MCP/HTTP; no Finance Lab or live LLM."""

import asyncio
import json
import threading
from contextlib import AsyncExitStack, aclosing
from copy import deepcopy
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from redteam.app.bootstrap import build_application, run_engagement
from redteam.app.report_store import load_terminal_report, save_terminal_report
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.core.config import AppConfig, CapabilityConfig, load_engagement_config
from redteam.core.enums import (
    EvidenceSourceType,
    MissionStatus,
    PolicyVerdict,
    RiskLevel,
    TaskStatus,
)
from redteam.core.ids import RunId
from redteam.orchestration.dispatch import merge_worker_outcomes
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.graph import build_graph
from redteam.orchestration.nodes.aggregate import aggregate_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import TerminationReason, initial_graph_state
from redteam.orchestration.work.models import WorkStatus
from tests.integration import _engine_llm_harness as h
from tests.unit.test_mcp_deployment import engagement as engagement_fixture
from tests.unit.test_specialist_profile import WEB_ROLE, SpecialistLLM, _atomized, _strategy

P1_PROFILE = "config/agents/specialists-p1.yaml"


@pytest.fixture
def independent_services():
    """A barrier proves overlap, not a timing-based speed assumption."""
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    calls = []
    concurrency = {"active": 0, "maximum": 0, "barrier_passed": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                calls.append((self.server.server_port, self.path))
                concurrency["active"] += 1
                concurrency["maximum"] = max(concurrency["maximum"], concurrency["active"])
            try:
                barrier.wait()
                with lock:
                    concurrency["barrier_passed"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Synthetic independent observation")
            except threading.BrokenBarrierError:
                self.send_error(503, "Parallel barrier was not reached")
            finally:
                with lock:
                    concurrency["active"] -= 1

        def log_message(self, *args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    origins = [
        f"http://{host}:{s.server_port}"
        for host, s in zip(("127.0.0.1", "localhost"), servers, strict=True)
    ]
    try:
        yield origins, calls, concurrency
    finally:
        barrier.abort()
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@pytest.mark.parametrize("require_approval", [False, True])
@pytest.mark.parametrize("dispatch_mode", ["isolated", "langgraph"])
@pytest.mark.parametrize("profile", [P1_PROFILE, "config/agents/specialists-p2.yaml"])
async def test_p1_independent_workers_use_real_mcp_and_keep_control_gate(
    tmp_path,
    monkeypatch,
    independent_services,
    require_approval,
    dispatch_mode,
    profile,
):
    origins, calls, concurrency = independent_services
    refs = ["service-a", "service-b"]
    spec = DeploymentSpec.model_validate(
        {
            "policy": {
                "targets": {
                    ref: {"origin": origin, "allowed_paths": ["/"]}
                    for ref, origin in zip(refs, origins, strict=True)
                },
                "allowed_networks": ["127.0.0.0/8", "::1/128"],
            },
            "host_bindings": {
                urlsplit(origin).hostname: ref for ref, origin in zip(refs, origins, strict=True)
            },
        }
    )
    config = engagement_fixture()
    config.scope.allowed_hosts = [urlsplit(origin).netloc for origin in origins]
    config.approval_policy.require_approval_for = []
    config.approval_policy.risk_threshold = (
        RiskLevel.LOW if require_approval else RiskLevel.CRITICAL
    )
    config.rate_limit_policy.max_concurrent_actions = 2
    path = provision(spec, config, tmp_path / "approved")
    loaded = load_engagement_config(path)
    binding = loaded.metadata["mcp_deployment"]
    for key, value in connection_environment(
        spec,
        binding["policy_path"],
        binding["policy_sha256"],
    ).items():
        monkeypatch.setenv(key, value)
    app = AppConfig(
        engagement=loaded,
        specialist_profile=profile,
        capability=CapabilityConfig(mcp_servers=list(spec.roles)),
    )
    llm = SpecialistLLM()
    async with AsyncExitStack() as stack:
        eng, deps = await build_application(
            app,
            llm=llm,
            mcp_stack=stack,
            evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
            orchestration_settings=OrchestrationSettings(
                max_steps=12,
                global_concurrency=2,
                per_target_concurrency=1,
                retry_retryable_tool_failures=False,
            ),
        )
        # Profile selection also reaches the CLI/default one-item settings.
        assert deps.settings.max_actions_per_turn == 2
        runtime = h.make_runtime("run-independent", str(eng.engagement_id))
        runtime.remaining_budget = 10
        nodes = await seed_from_engagement(
            AttackGraphService(deps.graph_store),
            eng,
            run_id=RunId(runtime.run_id),
            step=0,
        )
        targets = ["host:" + urlsplit(origin).netloc for origin in origins]
        assert set(targets) <= {str(n.id) for n in nodes}
        strategy, atomized = _strategy(targets[0]), _atomized(targets[0])
        question = deepcopy(strategy["questions"][0])
        question.update(question_id="q2", target_node_ids=[targets[1]])
        strategy["questions"].append(question)
        task = deepcopy(atomized["path"]["steps"][0])
        task.update(step_id="s2", target_node_ids=[targets[1]])
        atomized["path"]["steps"].append(task)
        atomized["origins"]["s2"] = "q2"
        llm.add("attack_path_proposal", strategy)
        llm.add("task_atomization", atomized)
        llm.add("path_review", h.review_accept())
        llm.add(
            "next_action",
            {
                "mode": "delegate",
                "reason": "Two independent scoped observations",
                "delegations": [
                    {"path_step_id": identity, "agent_id": WEB_ROLE, "budget": {"max_actions": 1}}
                    for identity in ("s1", "s2")
                ],
            },
        )
        engine = InvestigationEngine(deps)
        current, actions, note = await engine.decide(runtime, GraphObservation())
        assert current and not actions and note == "continue_planning"
        assert not calls
        for alias, target, ref in zip(
            ("recon.http_probe", "web.http_request"),
            targets,
            refs,
            strict=True,
        ):
            tool = deps.tool_catalog.get(alias)
            assert tool is not None
            llm.add(
                "worker_next_action",
                {
                    "decision": "propose_actions",
                    "reason": "One bounded GET observation",
                    "actions": [
                        {
                            "target_node_id": target,
                            "tool_id": str(tool.tool_id),
                            "arguments": {"target_ref": ref, "path": "/"},
                            "reason": "Observe the authorized service",
                        }
                    ],
                },
            )
        planned = await plan_node(
            {**initial_graph_state(engine.updated_runtime), "observation": GraphObservation()},
            deps=deps,
        )
        assert len(planned["dispatch_items"]) == 2, planned["runtime"].planning_feedback
        if dispatch_mode == "langgraph":
            outcomes = {}
            aggregate = None
            # Verify the actual Send/reducer path through the first batch only.
            # No follow-up model completion or approval-resume claim is implied.
            updates = build_graph(deps).astream(
                planned, config={"recursion_limit": 12}, stream_mode="updates"
            )
            async with aclosing(updates):
                async for update in updates:
                    if "worker" in update:
                        outcomes = merge_worker_outcomes(
                            outcomes, update["worker"]["worker_outcomes"]
                        )
                    if "aggregate" in update:
                        aggregate = update["aggregate"]
                        break
            assert aggregate is not None
            assert len(outcomes) == 2
        else:
            results = await asyncio.gather(
                *[
                    worker_node(
                        {"runtime": planned["runtime"], "dispatch_item": item}, deps=deps
                    )
                    for item in planned["dispatch_items"]
                ]
            )
            outcomes = merge_worker_outcomes(
                results[0]["worker_outcomes"], results[1]["worker_outcomes"]
            )
            aggregate = await aggregate_node({**planned, "worker_outcomes": outcomes}, deps=deps)
        if require_approval:
            assert calls == []
            assert aggregate["runtime"].actions_executed == 0
            assert aggregate["runtime"].pending_approval_id
            assert (
                sum(o.policy_verdict is PolicyVerdict.REQUIRE_APPROVAL for o in outcomes.values())
                == 1
            )
            assert any(o.error == "deferred_for_approval" for o in outcomes.values())
            for item in planned["dispatch_items"]:
                assert (await deps.work_store.get(item.work_id)).status is WorkStatus.PENDING
        else:
            assert all(o.executed and o.succeeded for o in outcomes.values())
            assert len(calls) == 2 and all(path == "/" for _, path in calls)
            assert concurrency["maximum"] == 2 and concurrency["barrier_passed"] == 2
            assert aggregate["runtime"].actions_executed == 2
            assert aggregate["runtime"].remaining_budget == 8
            records = await deps.evidence_store.list_for_run(runtime.run_id)
            assert len(records) >= 2
            graph = AttackGraphService(deps.graph_store)
            assert len(await graph.list_nodes()) > 2
            assert await graph.list_findings() == []
            replay = await aggregate_node(
                {**planned, "runtime": aggregate["runtime"], "worker_outcomes": outcomes},
                deps=deps,
            )
            assert replay["runtime"].actions_executed == 2
            assert replay["runtime"].remaining_budget == 8


class CompletingSpecialists(SpecialistLLM):
    """Fixture model only: real loop owns admission, evidence and settlement."""

    def __init__(self, targets):
        super().__init__()
        self.targets = targets
        self.worker_calls = {}

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        payload, _ = json.JSONDecoder().raw_decode(messages[-1]["content"])
        route = self._route_key(response_schema)
        self.calls.append({"route": route, "messages": messages, "system": system})
        if route == "attack_path_proposal":
            strategy = _strategy(self.targets[0])
            other = deepcopy(strategy["questions"][0])
            other.update(question_id="q2", target_node_ids=[self.targets[1]])
            strategy["questions"].append(other)
            return strategy
        if route == "task_atomization":
            atomized = _atomized(self.targets[0])
            other = deepcopy(atomized["path"]["steps"][0])
            other.update(step_id="s2", target_node_ids=[self.targets[1]])
            atomized["path"]["steps"].append(other)
            atomized["origins"]["s2"] = "q2"
            return atomized
        if route == "path_review":
            return h.review_accept()
        if route == "analysis_verify":
            return h.meaning("success", False)  # observation, never a vulnerability
        if route == "worker_next_action":
            identity = payload["path_step_id"]
            count = self.worker_calls.get(identity, 0) + 1
            self.worker_calls[identity] = count
            if count == 1:
                index = 0 if identity == "s1" else 1
                name = "http_probe" if index == 0 else "http_request"
                tool = next(row for row in payload["tools"] if row["tool_name"] == name)
                return {"decision": "propose_actions", "reason": "Observe scoped response",
                        "actions": [{"target_node_id": self.targets[index],
                                     "tool_id": tool["tool_id"],
                                     "arguments": {"target_ref": f"service-{index}", "path": "/"},
                                     "reason": "Bounded approved read"}]}
            # Completion responses cross, while application remains serialized.
            if identity == "s1":
                await asyncio.sleep(0.02)
            ids = payload["evidence_ids"]
            assert ids
            return {"decision": "finish", "reason": "Original response stored",
                    "evidence_ids": ids, "completion": {"outcome": "success",
                    "reason": "Assigned response observed", "evidence_ids": ids}}
        if route == "completion_review":
            return h.completion_accept()
        if route == "task_completion_review":
            return h.task_accept(payload["verified_source_ids"])
        if route == "goal_completion_review":
            return h.goal_accept([{"criterion": criterion, "met": True,
                                  "evidence_ids": payload["verified_source_ids"]}
                                 for criterion in payload["mission"]["success_criteria"]])
        if route == "next_action":
            if payload.get("active_task") is None:
                return h.turn_complete_goal()
            if all(s["status"] == "completed" for s in payload["active_path"]["steps"]):
                return {"mode": "complete_task", "reason": "Original observations ready"}
            return {"mode": "delegate", "reason": "Independent observations",
                    "delegations": [{"path_step_id": identity, "agent_id": WEB_ROLE,
                                     "budget": {"max_actions": 2}}
                                    for identity in ("s1", "s2")]}
        raise AssertionError(f"Unexpected completion route: {route}")


@pytest.mark.parametrize("profile", [P1_PROFILE, "config/agents/specialists-p2.yaml"])
async def test_specialists_real_mcp_full_loop_settles_steps_task_and_goal(
    tmp_path, monkeypatch, independent_services, profile,
):
    origins, calls, concurrency = independent_services
    spec = DeploymentSpec.model_validate({
        "policy": {"targets": {f"service-{index}": {"origin": origin, "allowed_paths": ["/"]}
                               for index, origin in enumerate(origins)},
                   "allowed_networks": ["127.0.0.0/8", "::1/128"]},
        "host_bindings": {urlsplit(origin).hostname: f"service-{index}"
                          for index, origin in enumerate(origins)},
    })
    config = engagement_fixture()
    config.scope.allowed_hosts = [urlsplit(origin).netloc for origin in origins]
    config.approval_policy.require_approval_for = []
    config.approval_policy.risk_threshold = RiskLevel.CRITICAL
    config.rate_limit_policy.max_concurrent_actions = 2
    loaded = load_engagement_config(provision(spec, config, tmp_path / "approved"))
    binding = loaded.metadata["mcp_deployment"]
    for key, value in connection_environment(
        spec, binding["policy_path"], binding["policy_sha256"],
    ).items():
        monkeypatch.setenv(key, value)
    targets = ["host:" + urlsplit(origin).netloc for origin in origins]
    llm = CompletingSpecialists(targets)
    monkeypatch.setattr("redteam.app.bootstrap.save_terminal_report",
                        partial(save_terminal_report, reports_dir=tmp_path / "reports"))
    async with AsyncExitStack() as stack:
        eng, deps = await build_application(
            AppConfig(engagement=loaded, specialist_profile=profile,
                      capability=CapabilityConfig(mcp_servers=list(spec.roles))),
            llm=llm, mcp_stack=stack, evidence_dir=tmp_path / "evidence",
            audit_dir=tmp_path / "audit", approval_dir=tmp_path / "approvals",
            orchestration_settings=OrchestrationSettings(
                max_steps=20, global_concurrency=2, per_target_concurrency=1,
                retry_retryable_tool_failures=False,
            ),
        )
        result = await run_engagement(
            eng, deps, objective="Observe original responses from both declared services",
            run_id=RunId("run-full-specialists"),
        )
        if result.termination_reason != TerminationReason.GOAL_ACHIEVED:
            raise AssertionError(json.dumps({
                "termination": result.termination_reason,
                "http": calls, "concurrency": concurrency,
                "delegations": [d.model_dump(mode="json") for d in result.delegations.values()],
                "tool_errors": [e.metadata["tool_result"].get("error") for e in
                                await deps.evidence_store.list_for_run(result.run_id)
                                if "tool_result" in e.metadata],
                "diagnostics": [d.model_dump() for d in result.planning_diagnostic_history],
                "calls": [(c["route"], json.JSONDecoder().raw_decode(
                    c["messages"][-1]["content"])[0].get("feedback")) for c in llm.calls],
            }))
        assert result.actions_executed == 2 and len(calls) == 2
        assert concurrency["maximum"] == 2
        assert llm.worker_calls == {"s1": 2, "s2": 2}
        mission = await deps.task_graph_store.get_mission(result.mission_id)
        assert mission.status is MissionStatus.COMPLETED
        tasks = await deps.task_graph_store.list_tasks(mission_id=result.mission_id)
        assert len(tasks) == 1 and tasks[0].status is TaskStatus.COMPLETED
        paths = await deps.path_store.list_paths(str(result.run_id))
        assert all(s.status == "completed" for p in paths for s in p.steps)
        evidence = await deps.evidence_store.list_for_run(result.run_id)
        originals = [e for e in evidence if e.source_type is EvidenceSourceType.TOOL]
        assert len(originals) == 2 and all(e.metadata["tool_result"]["success"] for e in originals)
        receipts = [e for e in evidence
                    if e.metadata.get("record_kind") == "task_completion_receipt"]
        assert len(receipts) == 1
        assert set(receipts[0].metadata["source_evidence_ids"]) == {
            str(e.evidence_id) for e in originals
        }
        assert await AttackGraphService(deps.graph_store).list_findings() == []
        saved_report = load_terminal_report(result.run_id, reports_dir=tmp_path / "reports")
        assert saved_report is not None and saved_report.ready_for_release
        assert saved_report.run_outcome == TerminationReason.GOAL_ACHIEVED
