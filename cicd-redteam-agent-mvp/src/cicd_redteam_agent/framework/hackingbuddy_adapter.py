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


async def run_agent_analysis(
    state: AnalysisState, boundary: RepositoryBoundary, config
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
    if config.provider is ModelProvider.CODEX_CHATGPT:
        from cicd_redteam_agent.framework.codex_llm import CodexChatGPTLLM

        llm = CodexChatGPTLLM(
            model=config.model,
            context_size=min(config.max_tokens, 128_000),
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
        )
        provider = credential.provider.value
    llm.init()
    try:
        context = CapabilityContext(state=state, boundary=boundary)
        prompt_path = Path(__file__).parents[1] / "prompts" / "cicd_security.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        agent = CICDChatAgent(log=logger, llm=llm, prompt=prompt)
        agent._capabilities = build_capabilities(context)
        await agent.init()
        await logger.start_run(
            "cicd-redteam-analysis",
            (
                f"model={config.model}; framework_commit={FRAMEWORK_COMMIT}; "
                f"repository={state.repository.repository_id}"
            ),
        )
        usecase = CICDUseCase(agent=agent, context=context, limits=limits)
        reason = await usecase.run()
        status = (
            ReportStatus.COMPLETE if reason == "proposal_validated" else ReportStatus.PARTIAL
        )
        # Avoid JsonlLogger.run_was_success(), whose generic state is "got root".
        logger._finish_run("OK" if status is ReportStatus.COMPLETE else "ERROR", reason)
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
        )
    finally:
        close = getattr(llm, "close", None)
        if close is not None:
            close()
