from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hackingBuddyGPT.utils.console import Console
from hackingBuddyGPT.utils.limits import Limits
from hackingBuddyGPT.utils.llm import LiteLLM
from hackingBuddyGPT.utils.logging import JsonlLogger

from cicd_redteam_agent.domain.models import AnalysisState, ReportStatus
from cicd_redteam_agent.framework.auth import ModelProvider, resolve_litellm_credential
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.cicd_usecase import CICDUseCase
from cicd_redteam_agent.framework.runtime_support import (
    AgentRuntimeMetrics,
    CheckpointWriter,
    EventJournal,
    ResumeCheckpoint,
    RunLease,
    restore_limits,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FRAMEWORK_COMMIT = "b2653771aed383ffe92933f8f2f9b7684403af8a"


@dataclass
class AgentRunResult:
    status: ReportStatus
    reason: str
    rounds: int
    tokens: int
    cost: float
    warnings: list[str]
    trace_path: Path | None = None
    provider: str | None = None
    runtime_metrics: dict[str, object] | None = None
    checkpoint_path: Path | None = None
    attempt_id: int = 1
    duration_seconds: float = 0.0


async def run_agent_analysis(
    state: AnalysisState,
    boundary: RepositoryBoundary,
    config,
    run_dir: Path,
    *,
    resume_checkpoint: ResumeCheckpoint | None = None,
) -> AgentRunResult:
    with RunLease(run_dir / ".run.lock"):
        return await _run_agent_analysis_locked(
            state,
            boundary,
            config,
            run_dir,
            resume_checkpoint=resume_checkpoint,
        )


async def _run_agent_analysis_locked(
    state: AnalysisState,
    boundary: RepositoryBoundary,
    config,
    run_dir: Path,
    *,
    resume_checkpoint: ResumeCheckpoint | None,
) -> AgentRunResult:
    trace_dir = config.output_dir.resolve() / ".agent-traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    logger = JsonlLogger(console=Console(), log_dir=str(trace_dir), tag="cicd-redteam-agent")
    limits = Limits(
        max_rounds=config.max_rounds,
        max_tokens=config.max_tokens,
        max_cost=config.max_cost,
        max_duration=config.max_duration,
    )
    attempt_id = resume_checkpoint.attempt_id + 1 if resume_checkpoint else 1
    if resume_checkpoint is not None:
        restore_limits(limits, resume_checkpoint)
    event_journal = EventJournal(run_dir / "events.jsonl", attempt_id=attempt_id)
    event_journal.append(
        "RESUME_STARTED" if resume_checkpoint else "RUN_STARTED",
        round=limits.rounds,
        payload={
            "provider": resume_checkpoint.provider if resume_checkpoint else config.provider.value,
            "model": config.model,
            "checkpoint_event_seq": (
                resume_checkpoint.committed_event_seq if resume_checkpoint else 0
            ),
        },
    )
    if config.provider is ModelProvider.CODEX_CHATGPT:
        from cicd_redteam_agent.framework.codex_llm import CodexChatGPTLLM

        llm = CodexChatGPTLLM(
            model=config.model,
            context_size=min(config.max_tokens, 128_000),
            state=state,
        )
        provider = "codex-chatgpt"
    else:
        credential = resolve_litellm_credential(
            config.provider,
            config.model,
            env_file=config.env_file,
            untrusted_repository_root=boundary.root,
        )
        llm = LiteLLM(
            api_key=credential.api_key,
            model=config.model,
            context_size=min(config.max_tokens, 128_000),
            api_retries=0,
        )
        provider = credential.provider.value
    llm.init()
    try:
        context = CapabilityContext(
            state=state,
            boundary=boundary,
            analysis_mode=config.resolved_mode,
        )
        prompt_path = Path(__file__).parents[1] / "prompts" / "cicd_security.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        runtime_metrics = (
            resume_checkpoint.runtime_metrics if resume_checkpoint else AgentRuntimeMetrics()
        )
        agent = CICDChatAgent(
            log=logger,
            llm=llm,
            prompt=prompt,
            state=state,
            runtime_metrics=runtime_metrics,
            event_journal=event_journal,
        )
        agent._capabilities = build_capabilities(context)
        await agent.init()
        await logger.start_run(
            "cicd-redteam-analysis",
            (
                f"model={config.model}; framework_commit={FRAMEWORK_COMMIT}; "
                f"repository={state.repository.repository_id}"
            ),
        )
        checkpoint_path = run_dir / "checkpoint.json"
        checkpoint_writer = CheckpointWriter(
            path=checkpoint_path,
            state=state,
            provider=provider,
            model=config.model,
            metrics=runtime_metrics,
            journal=event_journal,
            attempt_id=attempt_id,
            config={
                "attacker_profile": config.attacker_profile.value,
                "analysis_mode": config.resolved_mode.value,
                "max_rounds": config.max_rounds,
                "max_tokens": config.max_tokens,
                "max_cost": config.max_cost,
                "max_duration": config.max_duration,
                "repository_commit": state.repository.git_commit,
                "repository_dirty": state.repository.dirty,
            },
        )
        usecase = CICDUseCase(
            agent=agent,
            context=context,
            limits=limits,
            checkpoint_writer=checkpoint_writer,
            event_journal=event_journal,
        )
        reason = await usecase.run()
        status = (
            ReportStatus.COMPLETE if reason == "proposal_validated" else ReportStatus.PARTIAL
        )
        # Avoid JsonlLogger.run_was_success(), whose generic state is "got root".
        logger._finish_run("OK" if status is ReportStatus.COMPLETE else "ERROR", reason)
        event_journal.append(
            "RUN_TERMINATED",
            round=limits.rounds,
            payload={"reason": reason, "status": status.value},
        )
        trace_path = Path(logger._writer.path) if logger._writer and logger._writer.path else None
        return AgentRunResult(
            status=status,
            reason=reason,
            rounds=limits.rounds,
            tokens=limits.tokens,
            cost=limits.cost,
            warnings=usecase.warnings,
            trace_path=trace_path,
            provider=provider,
            runtime_metrics=runtime_metrics.to_dict(),
            checkpoint_path=checkpoint_path,
            attempt_id=attempt_id,
            duration_seconds=(limits.duration.total_seconds() if limits.duration else 0.0),
        )
    finally:
        close = getattr(llm, "close", None)
        if close is not None:
            close()
