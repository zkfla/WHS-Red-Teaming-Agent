from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cicd_redteam_agent.application.report import write_report
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.attack_graph import compose_global_attack_paths
from cicd_redteam_agent.domain.attacker_control import assess_attacker_control, derive_attack_paths
from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.domain.identity_analysis import derive_identity_candidates
from cicd_redteam_agent.domain.models import (
    AnalysisMode,
    AnalysisReport,
    AttackerProfile,
    ReportStatus,
    Termination,
    TraceMetadata,
)
from cicd_redteam_agent.domain.reference_tracer import trace_local_references
from cicd_redteam_agent.domain.scenario_rules import derive_additional_scenario_results
from cicd_redteam_agent.domain.security_facts import derive_security_facts
from cicd_redteam_agent.framework.auth import ModelProvider
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FRAMEWORK_COMMIT = "b2653771aed383ffe92933f8f2f9b7684403af8a"


@dataclass(frozen=True)
class AnalysisConfig:
    output_dir: Path
    attacker_profile: AttackerProfile = AttackerProfile.FORK_PR
    model: str | None = None
    provider: ModelProvider = ModelProvider.AUTO
    analysis_mode: AnalysisMode | None = None
    env_file: Path | None = None
    max_rounds: int = 12
    max_tokens: int = 100_000
    max_cost: float = 2.0
    max_duration: int = 900

    @property
    def resolved_mode(self) -> AnalysisMode:
        if self.analysis_mode is not None:
            return self.analysis_mode
        if self.model or self.provider is ModelProvider.CODEX_CHATGPT:
            return AnalysisMode.REASONING
        return AnalysisMode.DETERMINISTIC


async def analyze_repository(
    repo_path: str | Path, config: AnalysisConfig
) -> tuple[AnalysisReport, Path]:
    started = time.monotonic()
    mode = config.resolved_mode
    if (
        mode is AnalysisMode.REASONING
        and not config.model
        and config.provider is not ModelProvider.CODEX_CHATGPT
    ):
        raise ValueError(
            "reasoning analysis requires --model or --provider codex-chatgpt"
        )
    boundary = RepositoryBoundary(repo_path)
    state = create_initial_state(boundary)
    warnings: list[str] = []
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = config.output_dir.resolve() / run_id

    def finalize(
        status: ReportStatus,
        reason: str,
        *,
        rounds: int = 0,
        tokens: int = 0,
        cost: float = 0.0,
        detail: str | None = None,
        source_trace: Path | None = None,
        provider: str | None = None,
    ) -> tuple[AnalysisReport, Path]:
        state.termination = Termination(
            reason=reason,
            detail=detail,
            rounds=rounds,
            tokens=tokens,
            cost=cost,
            duration_seconds=time.monotonic() - started,
        )
        report = AnalysisReport(
            status=status,
            termination_reason=reason,
            state=state,
            warnings=warnings,
            trace=TraceMetadata(
                provider=provider,
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
                "provider": provider or config.provider,
                "analysis_mode": mode,
                "framework_commit": FRAMEWORK_COMMIT,
                "repository_commit": state.repository.git_commit,
            },
        )
        if source_trace and source_trace.exists():
            run_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_trace, run_dir / "trace.jsonl")
        write_report(report, run_dir)
        return report, run_dir

    try:
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
        security_facts = derive_security_facts(state, config.attacker_profile)
        state.relations.extend(security_facts)
        state.pending_questions = sorted(
            {
                item
                for relation in security_facts
                for item in relation.missing_evidence
            }
        )
        state.identities = derive_identity_candidates(state)
        if mode is AnalysisMode.DETERMINISTIC:
            state.findings, state.paths = derive_attack_paths(state)
            additional_findings, additional_paths = derive_additional_scenario_results(state)
            state.findings.extend(additional_findings)
            state.paths.extend(additional_paths)
            graph_relations, global_paths = compose_global_attack_paths(
                state, config.attacker_profile
            )
            state.relations.extend(graph_relations)
            state.paths.extend(global_paths)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {redact(str(exc))}"
        warnings.append(f"Deterministic analysis failed: {detail}")
        return finalize(
            ReportStatus.FAILED,
            "deterministic_analysis_failed",
            detail=detail,
        )

    if mode is AnalysisMode.REASONING:
        from cicd_redteam_agent.framework.hackingbuddy_adapter import run_agent_analysis

        agent_result = await run_agent_analysis(state, boundary, config)
        warnings.extend(agent_result.warnings)
        return finalize(
            agent_result.status,
            agent_result.reason,
            rounds=agent_result.rounds,
            tokens=agent_result.tokens,
            cost=agent_result.cost,
            source_trace=agent_result.trace_path,
            provider=agent_result.provider,
        )

    if config.model or config.provider is ModelProvider.CODEX_CHATGPT:
        warnings.append("Deterministic mode selected; configured model was not used.")
    else:
        warnings.append("No model configured; deterministic analysis completed without LLM review.")
    return finalize(ReportStatus.COMPLETE, "deterministic_analysis_complete")
