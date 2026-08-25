"""메인 순환 — 관측→재계획, 이게 없으면 그래프를 한 번 만들고 끝나는 정적 스크립트다.

순서(§13): BOOTSTRAP → RECON(passive) → ENTITLEMENT 확정 → BASELINE 학습 →
[전략 선택 → 후보 나열 → 실행 → 검증 → 학습(⑥) → 지식 갱신(②)] 반복.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from finredteam.agent import strategy as strategy_selector
from finredteam.agent import termination
from finredteam.agent.budget import Budget
from finredteam.knowledge.baseline import BaselineRegistry
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.llm.base import LLMClient, Usage
from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory
from finredteam.memory.working import WorkingMemory
from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import CandidateFinding, Finding
from finredteam.models.knowledge import Lesson
from finredteam.oracles import verifier as oracle_verifier
from finredteam.planning import candidates as candidates_module
from finredteam.planning import planner as planner_module
from finredteam.planning import prioritizer as prioritizer_module
from finredteam.planning.candidates import AttackTemplate
from finredteam.probing import executor as executor_module
from finredteam.probing.session import Session
from finredteam.recon import entitlement as entitlement_module
from finredteam.recon import mapper as mapper_module
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import TargetConfig

_TARGET_UNHEALTHY_THRESHOLD = 5
_SATURATED_ROUNDS_WITHOUT_GROWTH = 3


@dataclass
class AgentContext:
    target: TargetConfig
    session: Session
    llm: LLMClient
    store: RunStore
    budget: Budget

    principals: list[Principal] = field(default_factory=list)
    graph: KnowledgeGraph = field(default_factory=KnowledgeGraph)
    working: WorkingMemory = field(default_factory=WorkingMemory)
    semantic: SemanticMemory = field(default_factory=SemanticMemory)
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)
    baselines: BaselineRegistry = field(default_factory=BaselineRegistry)
    templates: dict[str, AttackTemplate] = field(default_factory=dict)

    round: int = 0
    findings: list[Finding] = field(default_factory=list)
    review_queue: list[Finding] = field(default_factory=list)
    strategy_history: list[str] = field(default_factory=list)

    _prev_edge_count: int = 0
    _rounds_without_growth: int = 0
    _consecutive_request_errors: int = 0

    # ── agent/strategy.py 가 요구하는 신호 ──────────────────────────
    def has_pending_race_trials(self) -> bool:
        # F4 후보 자동 enumerate 는 아직 없다(§17 우선순위: F4 는 빠져도 된다) —
        # RACE_RETRY 전환은 항상 스킵된다.
        return False

    def graph_grew_significantly(self) -> bool:
        return self.graph.grew_significantly(self._prev_edge_count)

    def recent_violations(self) -> bool:
        return any(f.verdict == Verdict.VIOLATION for f in self.findings[-5:])

    def successful_classes(self) -> list[str]:
        classes = sorted({f.candidate.attack_class for f in self.findings if f.verdict == Verdict.VIOLATION})
        return classes or list(self.templates.keys())

    def consecutive_failures(self) -> int:
        return self.episodic.consecutive_failures()

    def least_tried_classes(self) -> list[str]:
        tried = self.episodic.tried_attack_classes()
        untried = sorted(set(self.templates.keys()) - tried)
        return untried or sorted(self.templates.keys())

    def layer_untouched(self, layer: str) -> bool:
        return not any(ep.candidate.layer == layer for ep in self.episodic.all())

    # ── agent/termination.py 가 요구하는 신호 ───────────────────────
    @property
    def llm_usage(self) -> Usage:
        return self.llm.usage

    def target_unhealthy(self) -> bool:
        return self._consecutive_request_errors >= _TARGET_UNHEALTHY_THRESHOLD

    def saturated(self) -> bool:
        return self._rounds_without_growth >= _SATURATED_ROUNDS_WITHOUT_GROWTH

    def converged(self) -> bool:
        all_tried = self.templates and self.episodic.tried_attack_classes() >= set(self.templates.keys())
        return bool(all_tried) and self._rounds_without_growth >= 2


def bootstrap_principals(session: Session, target: TargetConfig) -> list[Principal]:
    run_tag = secrets.token_hex(3)
    principals: list[Principal] = []
    for principal_id in target.settings.principals:
        suffix = secrets.token_hex(3)
        username = f"{principal_id}_{run_tag}_{suffix}"
        password = f"Rt!{secrets.token_urlsafe(8)}"
        principals.append(session.bootstrap(principal_id, username, password))
    return principals


def _watch_accounts_for(candidate: CandidateFinding, principal: Principal) -> list[str]:
    accounts = list(candidate.watch_accounts)
    if candidate.subject_account:
        accounts.append(candidate.subject_account)
    if principal.account_number:
        accounts.append(principal.account_number)
    return list(dict.fromkeys(a for a in accounts if a))


def run(target: TargetConfig, session: Session, llm: LLMClient, store: RunStore, *, max_rounds: int | None = None) -> AgentContext:
    budget = Budget(max_rounds=max_rounds) if max_rounds else Budget()
    ctx = AgentContext(target=target, session=session, llm=llm, store=store, budget=budget)

    # Phase 1: BOOTSTRAP
    ctx.principals = bootstrap_principals(session, target)
    ctx.graph.add_entitlements(entitlement_module.from_self_created(ctx.principals))

    # Phase 2/3: RECON(passive) + ENTITLEMENT 확정 (정상 경로만)
    recon_result = mapper_module.run(session, target, ctx.principals, ctx.graph)
    ctx.semantic.add_facts(recon_result.facts)

    # Phase 4: BASELINE 학습 (정상 파라미터로 실측)
    principals_with_account = [p for p in ctx.principals if p.account_number]
    if len(principals_with_account) >= 2:
        payer, payee = principals_with_account[0], principals_with_account[1]
        for op_cfg in target.baseline_ops():
            ctx.baselines.learn(session, target, op_cfg, payer, counterparty=payee)

    ctx.templates = candidates_module.load_templates()
    ctx._prev_edge_count = ctx.graph.edge_count()

    # Phase 5~N: 순환
    while True:
        stop, reason = termination.should_stop(ctx)
        if stop:
            store.finalize(reason, usage=llm.usage)
            return ctx

        ctx.round += 1
        strat = strategy_selector.choose(ctx)
        ctx.strategy_history.append(strat.name)
        store.record_strategy(ctx.round, strat.name)

        edge_count_before_round = ctx.graph.edge_count()

        skeletons = candidates_module.enumerate_skeletons(
            ctx.principals, ctx.graph, ctx.target, ctx.templates, strat.attack_classes or None
        )
        candidates = planner_module.refine(
            skeletons, ctx.templates, ctx.semantic, ctx.episodic, ctx.llm, ctx.baselines
        )
        candidates = prioritizer_module.rank_and_dedup(candidates, ctx.episodic)

        for candidate in candidates[: strat.batch_size]:
            principal = next((p for p in ctx.principals if p.id == candidate.principal_id), None)
            if principal is None:
                continue

            watch_accounts = _watch_accounts_for(candidate, principal)
            result = executor_module.run(session, target, candidate, principal, watch_accounts, db=store.db)
            if all(code == 0 for code in result.status_codes):
                ctx._consecutive_request_errors += 1
            else:
                ctx._consecutive_request_errors = 0

            finding = oracle_verifier.verify(candidate, result, graph=ctx.graph)
            store.append("findings", finding.model_dump(mode="json"))

            for account in watch_accounts:
                ctx.graph.apply_observed(candidate.principal_id, f"account:{account}", "read")

            lesson = Lesson(
                candidate_summary=candidate.id,
                outcome=finding.verdict.value,
                insight=finding.reason,
                attack_class=candidate.attack_class,
            )
            ctx.episodic.record(candidate, finding, lesson)

            if finding.verdict == Verdict.VIOLATION:
                ctx.findings.append(finding)
            elif finding.verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
                ctx.review_queue.append(finding)

        if ctx.graph.edge_count() > edge_count_before_round:
            ctx._rounds_without_growth = 0
        else:
            ctx._rounds_without_growth += 1
        ctx._prev_edge_count = edge_count_before_round

        store.snapshot_round(ctx.round, {"strategy_history": ctx.strategy_history})
