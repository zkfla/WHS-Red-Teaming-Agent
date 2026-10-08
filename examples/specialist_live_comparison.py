"""Opt-in live-model smoke comparison, real MCP/Control and disposable local HTTP.

Not a vulnerability benchmark or proof of statistical quality/speed. Profiles
share target state/catalog/ROE/model; every trial owns fresh graph/stores/run.
Only the existing ChatGPT subscription route is permitted at a zero additional
API billing budget. No API-key fallback, model substitution, or purchases.
Account subscription quota still applies; monetary usage is not metered here.
"""

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from redteam.app.bootstrap import build_application, run_engagement
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.deployment import DeploymentSpec, connection_environment, provision
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    load_engagement_config,
)
from redteam.core.enums import RiskLevel
from redteam.core.ids import RunId
from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.settings import OrchestrationSettings


class MeteredModel:
    """Before-call reservation, including failures, shared across all trials."""

    def __init__(self, client, limit):
        self.client, self.limit = client, limit
        self.requests = 0
        self.trial_start = 0
        self.trial_limit = 30
        self.calls: list[dict] = []

    async def _call(self, method, kwargs):
        if self.requests >= self.limit or self.requests - self.trial_start >= self.trial_limit:
            raise RuntimeError("comparison_request_limit_reached")
        # No await between limit and reservation: parallel callers cannot overspend.
        self.requests += 1
        row = {"request": self.requests, "seconds": 0.0, "outcome": "started",
               "tokens": None}
        self.calls.append(row)
        started = time.monotonic()
        try:
            result = await method(**kwargs)
            row["outcome"] = "received"
            row["tokens"] = getattr(result, "total_tokens", None)
            return result
        except Exception as exc:
            row["outcome"] = type(exc).__name__  # No provider body or model content.
            raise
        finally:
            row["seconds"] = round(time.monotonic() - started, 3)

    async def complete_structured_with_usage(self, **kwargs):
        return await self._call(self.client.complete_structured_with_usage, kwargs)

    async def complete_structured(self, **kwargs):
        return await self._call(self.client.complete_structured, kwargs)

    async def complete(self, **kwargs):
        return await self._call(self.client.complete, kwargs)


@contextmanager
def synthetic_services():
    calls: list[dict] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                calls.append({"port": self.server.server_port, "path": self.path})
            # Safe, immutable observation benchmark. No target vulnerabilities/answers.
            self.send_response(200 if self.path == "/" else 404)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Synthetic observation service</title>")

        def log_message(self, *args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        yield [f"http://{host}:{server.server_port}"
               for host, server in zip(("127.0.0.1", "localhost"), servers, strict=True)], calls
    finally:
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(2)


def prior_usage(prior_results, max_calls):
    """Carry previous evaluation consumption forward before any model request."""
    previous_requests = 0
    if prior_results:
        previous = json.loads(Path(prior_results).read_text())
        previous_requests = previous.get("cumulative_requests_used", previous.get("requests_used"))
        if type(previous_requests) is not int or not 0 <= previous_requests <= 100:
            raise ValueError("comparison_prior_usage_unproven")
    if previous_requests + max_calls > 100:
        raise ValueError("comparison_cumulative_request_limit_exceeded")
    return previous_requests


async def compare(args):
    previous_requests = prior_usage(args.prior_results, args.max_calls)
    import litellm
    from litellm.llms.chatgpt.common_utils import CHATGPT_API_BASE

    environment = {**dotenv_values(args.env_file), **os.environ}
    config = LLMConfig.from_environment(environment)
    _, provider, _, _ = litellm.get_llm_provider(config.default_model)
    if provider != "chatgpt" or args.max_api_cost_usd != 0:
        raise ValueError("comparison_requires_existing_subscription_and_zero_api_budget")
    # Pin the installed SDK's subscription endpoint; no global API-base/key fallback.
    client = LiteLLMClient(config.default_model, num_retries=0, timeout=45,
                          api_base=CHATGPT_API_BASE)
    metered = MeteredModel(client, args.max_calls)
    metered.trial_limit = args.max_calls_per_trial
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": "1", "model": config.default_model,
              "max_calls": args.max_calls, "max_api_cost_usd": 0,
              "billing_route": "existing_chatgpt_subscription_no_api_fallback",
              "monetary_usage_verified": False, "quota_consumed": True,
              "prior_requests_used": previous_requests,
              "max_calls_per_trial": args.max_calls_per_trial,
              "scope": "local_read_only_smoke_not_quality_release_gate", "trials": []}
    with synthetic_services() as (origins, http_calls):
        spec = DeploymentSpec.model_validate({
            "policy": {"targets": {
                name: {"origin": origin, "allowed_paths": ["/"]}
                for name, origin in zip(("observe-a", "observe-b"), origins, strict=True)
            }, "allowed_networks": ["127.0.0.0/8", "::1/128"]},
            "host_bindings": {urlsplit(origin).hostname: name for name, origin in zip(
                ("observe-a", "observe-b"), origins, strict=True)},
        })
        engagement = EngagementConfig(
            name="Read-only specialist smoke comparison",
            scope={"allowed_hosts": [urlsplit(o).netloc for o in origins]},
            execution_budget={"max_actions": 6, "max_duration_seconds": 600},
            approval_policy={"require_approval_for": [], "risk_threshold": RiskLevel.CRITICAL},
            rate_limit_policy={"max_actions_per_minute": 30, "max_concurrent_actions": 2},
        )
        path = provision(spec, engagement, output / "deployment")
        loaded = load_engagement_config(path)
        binding = loaded.metadata["mcp_deployment"]
        os.environ.update(connection_environment(spec, binding["policy_path"],
                                                  binding["policy_sha256"]))
        objective = ("Observe whether each explicitly scoped HTTP service responds and compare "
                     "their status and visible security headers. Use only approved read-only "
                     "paths. Retain original evidence; do not claim vulnerabilities or goal "
                     "success without evidence. Unsupported checks must remain unresolved.")
        report["initial_conditions_hash"] = content_hash({
            "scope": loaded.scope.model_dump(mode="json"),
            "policy": loaded.approval_policy.model_dump(mode="json"),
            "budget": loaded.execution_budget.model_dump(mode="json"),
            "targets": spec.policy.model_dump(mode="json"),
            "objective": objective,
        })
        for trial in range(args.trials):
            for label, profile in zip(("P0", "P1", "P2"), args.profiles, strict=True):
                if metered.requests >= metered.limit:
                    break
                directory = output / f"{label}-{trial + 1}"
                app = AppConfig(engagement=loaded, specialist_profile=profile,
                                capability=CapabilityConfig(mcp_servers=list(spec.roles)))
                metered.trial_start = metered.requests
                start_http = len(http_calls)
                started = time.monotonic()
                row = {"profile": label, "trial": trial + 1, "requests": 0}
                report["trials"].append(row)
                async with AsyncExitStack() as stack:
                    eng, deps = await build_application(
                        app, llm=metered, mcp_stack=stack,
                        evidence_dir=directory / "evidence", audit_dir=directory / "audit",
                        approval_dir=directory / "approvals",
                        orchestration_settings=OrchestrationSettings(max_steps=12,
                            global_concurrency=2, per_target_concurrency=1),
                    )
                    row["catalog_hash"] = content_hash([
                        tool.model_dump(mode="json") for tool in deps.tool_catalog.all_descriptors()
                    ])
                    try:
                        result = await asyncio.wait_for(run_engagement(
                            eng, deps, objective=objective,
                            run_id=RunId(f"compare-{output.name}-{label}-{trial}"),
                        ), 600)
                        row.update(phase=result.phase, termination=result.termination_reason,
                                   actions=result.actions_executed, mission=result.mission_status,
                                   diagnostics=[d.model_dump(mode="json")
                                                for d in result.planning_diagnostic_history])
                        row["findings"] = len(await AttackGraphService(
                            deps.graph_store).list_findings())
                        paths = await deps.path_store.list_paths(str(result.run_id))
                        row["steps"] = [{"status": step.status, "outcome": step.outcome}
                                        for path in paths for step in path.steps]
                        tasks = await deps.task_graph_store.list_tasks(mission_id=result.mission_id)
                        row["tasks"] = [task.status for task in tasks]
                    except Exception as exc:
                        row["error_type"] = type(exc).__name__
                row.update(seconds=round(time.monotonic() - started, 3),
                           requests=metered.requests - metered.trial_start,
                           http_calls=http_calls[start_http:])
                report["requests_used"] = metered.requests
                report["cumulative_requests_used"] = previous_requests + metered.requests
                report["calls"] = metered.calls
                (output / "results.json").write_text(json.dumps(report, indent=2, default=str))
                print(json.dumps(row, default=str), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--max-api-cost-usd", type=float, required=True)
    parser.add_argument("--prior-results", help="Previous approved evaluation's results.json")
    parser.add_argument("--max-calls-per-trial", type=int, default=30)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--profiles", nargs=3, default=["config/agents/specialists.yaml",
        "config/agents/specialists-p1.yaml", "config/agents/specialists-p2.yaml"])
    args = parser.parse_args()
    if (not 1 <= args.max_calls <= 100 or not 1 <= args.trials <= 5
        or not 1 <= args.max_calls_per_trial <= args.max_calls):
        parser.error("maximum 100 calls, 1..5 trials and a bounded per-trial call limit")
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(compare(args))


if __name__ == "__main__":
    main()
