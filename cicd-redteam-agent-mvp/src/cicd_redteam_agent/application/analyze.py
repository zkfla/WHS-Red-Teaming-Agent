from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cicd_redteam_agent.application.report import write_report
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.attacker_control import assess_attacker_control, derive_attack_paths
from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.domain.identity_analysis import derive_identity_candidates
from cicd_redteam_agent.domain.models import (
    AnalysisReport,
    AttackerProfile,
    ReportStatus,
    Termination,
    TraceMetadata,
)
from cicd_redteam_agent.domain.reference_tracer import trace_local_references
from cicd_redteam_agent.domain.scenario_rules import derive_additional_scenario_results
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FRAMEWORK_COMMIT = "b2653771aed383ffe92933f8f2f9b7684403af8a"


@dataclass(frozen=True)
class AnalysisConfig:
    output_dir: Path
    attacker_profile: AttackerProfile = AttackerProfile.FORK_PR
    model: str | None = None
    max_rounds: int = 12
    max_tokens: int = 100_000
    max_cost: float = 2.0
    max_duration: int = 900


async def analyze_repository(
    repo_path: str | Path, config: AnalysisConfig
) -> tuple[AnalysisReport, Path]:
    started = time.monotonic()
    boundary = RepositoryBoundary(repo_path)
    state = create_initial_state(boundary)
    warnings: list[str] = []
    for workflow_path in discover_workflows(boundary):
        result = parse_workflow(boundary, workflow_path)
        state.workflows.append(result.workflow)
        state.evidence.update(result.evidence)
        state.secret_references.extend(result.secret_references)
        warnings.extend(result.workflow.parse_warnings)
    relations, reference_evidence, reference_warnings = trace_local_references(
        state.workflows, boundary
    )
    state.relations.extend(relations)
    state.evidence.update(reference_evidence)
    warnings.extend(reference_warnings)
    state.assessments = assess_attacker_control(state, config.attacker_profile)
    state.findings, state.paths = derive_attack_paths(state)
    additional_findings, additional_paths = derive_additional_scenario_results(state)
    state.findings.extend(additional_findings)
    state.paths.extend(additional_paths)
    state.identities = derive_identity_candidates(state)

    status = ReportStatus.COMPLETE
    reason = "deterministic_analysis_complete"
    if config.model:
        from cicd_redteam_agent.framework.hackingbuddy_adapter import run_agent_analysis

        agent_result = await run_agent_analysis(state, boundary, config)
        status = agent_result.status
        reason = agent_result.reason
        warnings.extend(agent_result.warnings)
        rounds, tokens, cost = agent_result.rounds, agent_result.tokens, agent_result.cost
    else:
        rounds, tokens, cost = 0, 0, 0.0
        warnings.append("No model configured; deterministic analysis completed without LLM review.")
    duration = time.monotonic() - started
    state.termination = Termination(
        reason=reason,
        rounds=rounds,
        tokens=tokens,
        cost=cost,
        duration_seconds=duration,
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = config.output_dir.resolve() / run_id
    report = AnalysisReport(
        status=status,
        termination_reason=reason,
        state=state,
        warnings=warnings,
        trace=TraceMetadata(
            provider=config.model.split("/", 1)[0]
            if config.model and "/" in config.model
            else None,
            model=config.model,
            framework_commit=FRAMEWORK_COMMIT,
            trace_path=str(run_dir / "trace.jsonl"),
        ),
        run_metadata={
            "attacker_profile": config.attacker_profile,
            "max_rounds": config.max_rounds,
            "max_tokens": config.max_tokens,
            "max_cost": config.max_cost,
            "max_duration": config.max_duration,
            "framework_commit": FRAMEWORK_COMMIT,
            "repository_commit": state.repository.git_commit,
        },
    )
    if config.model and agent_result.trace_path and agent_result.trace_path.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent_result.trace_path, run_dir / "trace.jsonl")
    write_report(report, run_dir)
    return report, run_dir
