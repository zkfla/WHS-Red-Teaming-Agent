"""Single-writer durable priority publication, without model or resolver invocation."""

from collections.abc import Callable
from copy import deepcopy

from redteam_core.investigation.priority import (
    PriorityProvider,
    prepare_priority_selection,
    priority_input_fingerprint,
    root_evidence_fingerprint,
)
from redteam_core.investigation.resolution import ResolverRuntimeRegistry
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.priority import (
    PriorityDecisionRecord,
    PrioritySelection,
    PriorityState,
    PriorityTerminalBinding,
    RootPriorityState,
)
from redteam_core.models.runtime import RuntimeLifecycle, RuntimeState
from redteam_core.models.security import CandidateDisposition
from redteam_core.runtime.orchestrator import RuntimeOrchestrator


class PriorityCoordinator:
    def __init__(
        self,
        *,
        session: CoreSession,
        runtime: RuntimeState,
        orchestrator: RuntimeOrchestrator,
        registry: ResolverRuntimeRegistry,
        permissions: Callable[[], frozenset[str]],
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ):
        self.session, self.runtime, self.orchestrator = session, runtime, orchestrator
        self.registry, self.permissions, self.domain_states = registry, permissions, domain_states
        self._broken = False
        self._busy = False
        self._check_binding()

    def _check_binding(self):
        if self._broken:
            raise RuntimeError("priority storage failed; reload before continuing")
        if self._busy:
            raise RuntimeError("priority already has an active writer")
        if self.runtime.lifecycle is not RuntimeLifecycle.RUNNING:
            raise RuntimeError("priority requires running runtime")
        expected = self.orchestrator.composition_fingerprint
        descriptors = [row.model_dump(mode="python") for row in self.registry.descriptors()]
        actual = "sha256:" + sha256_text(canonical_json(descriptors))
        if not self.registry.frozen or actual != expected.resolver_routing_hash:
            raise RuntimeError("priority resolver composition mismatch")
        ledger = self.runtime.operational_budget
        if ledger is None or ledger.composition_hash != expected.composition_hash:
            raise RuntimeError("priority requires matching durable operational budget")
        if ledger.runtime_limits_hash != "sha256:" + sha256_text(
            canonical_json(self.orchestrator.budget_limits.model_dump(mode="json"))
        ):
            raise RuntimeError("priority runtime budget binding changed")
        RuntimeState.model_validate(self.runtime.model_dump(mode="python"))
        CoreSession.model_validate(self.session.model_dump(mode="python"))
        try:
            stored = self.orchestrator.checkpoint_store.load(
                catalog=self.orchestrator.catalog,
                composition_fingerprint=expected,
                journal=self.orchestrator.journal,
                expected_run_id=self.runtime.run_id,
            )
        except BaseException:
            self._broken = True
            raise
        if (
            stored.runtime_state.priority != self.runtime.priority
            or stored.runtime_state.operational_budget != ledger
            or not self.runtime.usage.dominates(stored.runtime_state.usage)
        ):
            self._broken = True
            raise RuntimeError(
                "durable priority/budget differs from memory; reload before continuing"
            )
        self._checkpoint_sequence = stored.checkpoint_sequence

    def _persist(self, session: CoreSession, runtime: RuntimeState, *, reason: str):
        session = CoreSession.model_validate(session.model_dump(mode="python"))
        runtime = RuntimeState.model_validate(runtime.model_dump(mode="python"))
        try:
            stored = self.orchestrator.checkpoint_store.load(
                catalog=self.orchestrator.catalog,
                composition_fingerprint=self.orchestrator.composition_fingerprint,
                journal=self.orchestrator.journal,
                expected_run_id=self.runtime.run_id,
            )
            if stored.checkpoint_sequence != self._checkpoint_sequence:
                raise RuntimeError("checkpoint changed during priority preparation; reload")
            self.orchestrator.checkpoint(
                session,
                runtime,
                reason=reason,
                domain_states=stored.domain_states
                if self.domain_states is None
                else self.domain_states,
            )
        except BaseException:
            self._broken = True
            raise
        self.session.__dict__.update(deepcopy(session.__dict__))
        self.runtime.__dict__.update(deepcopy(runtime.__dict__))

    def select(self, provider: PriorityProvider, *, limit: int = 6) -> PrioritySelection:
        self._check_binding()
        binding = provider.descriptor.binding_id
        if binding not in self.orchestrator.composition_fingerprint.priority_provider_ids:
            raise RuntimeError("priority provider/configuration is not explicitly composed")
        self._busy = True
        try:
            permissions = frozenset(self.permissions())
            now = self.orchestrator.clock.now()
            before = priority_input_fingerprint(
                self.session,
                self.runtime,
                self.registry,
                permissions,
                now,
                self.orchestrator.budget_limits,
            )
            working = self._reapply_bound_decisions(now)
            prepared_input = priority_input_fingerprint(
                working,
                self.runtime,
                self.registry,
                permissions,
                now,
                self.orchestrator.budget_limits,
            )
            selection, state = prepare_priority_selection(
                working,
                self.runtime,
                registry=self.registry,
                provider=provider,
                permissions=permissions,
                now=now,
                limits=self.orchestrator.budget_limits,
                limit=limit,
            )
            if (
                selection.input_hash != prepared_input
                or provider.descriptor.binding_id != binding
                or priority_input_fingerprint(
                    self.session,
                    self.runtime,
                    self.registry,
                    frozenset(self.permissions()),
                    self.orchestrator.clock.now(),
                    self.orchestrator.budget_limits,
                )
                != before
            ):
                raise RuntimeError("priority inputs changed during preparation")
            updated = self.runtime.model_copy(deep=True)
            updated.priority = state
            updated.revision += 1
            self._persist(working, updated, reason="priority-selection")
            return selection
        finally:
            self._busy = False

    def _reapply_bound_decisions(self, now) -> CoreSession:
        """Reconcile existing host decisions, never invent a new judgment from a score."""
        working = self.session.model_copy(deep=True)
        latest = {row.root_key: row for row in self.runtime.priority.decisions}
        groups = {}
        for candidate in working.candidate_state.candidates.values():
            groups.setdefault(candidate.root_cause.qualified_key, []).append(candidate)
        changed = False
        for root_key, group in groups.items():
            record = latest.get(root_key)
            if record is None or record.binding.evidence_hash != root_evidence_fingerprint(
                working, group, now=now
            ):
                continue
            for candidate in group:
                if candidate.disposition not in {
                    CandidateDisposition.UNREVIEWED,
                    CandidateDisposition.INVESTIGATING,
                }:
                    continue
                changed = True
                working.candidate_state.candidates[candidate.candidate_id] = candidate.model_copy(
                    update={
                        "disposition": record.binding.disposition,
                        "disposition_reason": record.reason,
                    }
                )
                entry = working.candidate_state.coverage_ledger[candidate.candidate_id]
                working.candidate_state.coverage_ledger[candidate.candidate_id] = entry.model_copy(
                    update={"disposition": record.binding.disposition, "reason": record.reason}
                )
        if changed:
            working.candidate_state.revision += 1
        return working

    def record_decision(
        self,
        *,
        root_key: str,
        expected_evidence_hash: str,
        disposition: CandidateDisposition,
        reason: str,
        user_final: bool = False,
    ) -> PriorityDecisionRecord:
        """Trusted host API; not a DecisionProposal action and never materializes Findings."""
        self._check_binding()
        self._busy = True
        try:
            state = self.runtime.priority
            if not state.selections:
                raise ValueError("priority decision requires a selected root")
            latest = state.selections[-1]
            selected_roots = {
                row.facts.root_key
                for row in latest.assessments
                if row.facts.candidate_id in latest.selected_candidate_ids
            }
            root = state.roots.get(root_key)
            if root is None or root_key not in selected_roots:
                raise ValueError("priority decision root was not selected in the latest batch")
            record = PriorityDecisionRecord(
                root_key=root_key,
                batch_index=state.batch_sequence,
                reason=reason,
                binding=PriorityTerminalBinding(
                    disposition=disposition,
                    evidence_hash=expected_evidence_hash,
                    user_final=user_final,
                ),
            )
            prior = next(
                (
                    row
                    for row in state.decisions
                    if row.root_key == root_key and row.batch_index == state.batch_sequence
                ),
                None,
            )
            if prior is not None:
                if prior == record:
                    return prior
                raise ValueError("priority root already decided in this batch")
            if root.terminal is not None and root.terminal.locked:
                raise ValueError("locked priority terminal decision cannot be overwritten")
            candidates = [
                row
                for row in self.session.candidate_state.candidates.values()
                if row.root_cause.qualified_key == root_key
            ]
            evidence = root_evidence_fingerprint(
                self.session, candidates, now=self.orchestrator.clock.now()
            )
            if (
                not candidates
                or evidence != expected_evidence_hash
                or root.evidence_hash != evidence
            ):
                raise ValueError("priority decision evidence binding is stale")
            session = self.session.model_copy(deep=True)
            for candidate in candidates:
                session.candidate_state.candidates[candidate.candidate_id] = candidate.model_copy(
                    update={"disposition": disposition, "disposition_reason": reason}
                )
                entry = session.candidate_state.coverage_ledger[candidate.candidate_id]
                session.candidate_state.coverage_ledger[candidate.candidate_id] = entry.model_copy(
                    update={"disposition": disposition, "reason": reason}
                )
            session.candidate_state.revision += 1
            payload = state.model_dump(mode="python")
            payload["revision"] += 1
            payload["decisions"] = (*state.decisions, record)
            payload["roots"][root_key] = RootPriorityState.model_validate(
                {
                    **root.model_dump(mode="python"),
                    "terminal": record.binding,
                    "reevaluation_required": False,
                }
            )
            runtime = self.runtime.model_copy(deep=True)
            runtime.priority = PriorityState.model_validate(payload)
            runtime.revision += 1
            self._persist(session, runtime, reason="priority-terminal-decision")
            return record
        finally:
            self._busy = False
