"""Bounded provider-neutral decision step loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Protocol

from pydantic import Field, ValidationError

from redteam_core.diagnostics.performance import PerformanceStage, performance_span
from redteam_core.execution import plan_experiment
from redteam_core.knowledge.commit import _is_fresh, refresh_freshness
from redteam_core.knowledge.recompute import SemanticRecomputer
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.decision import (
    CompletionAssessment,
    CompletionAssessmentRecord,
    ContextRetrievalReceipt,
    DecisionAction,
    DecisionDriverDescriptor,
    DecisionDriverReply,
    DecisionPhase,
    DecisionProposal,
    DecisionResponseReceipt,
    DecisionStepOutcome,
    DecisionStepReceipt,
    ProgressPolicy,
    ProgressState,
    ScopeCompletion,
)
from redteam_core.models.execution import ExperimentDescriptor
from redteam_core.models.operational_budget import RequestReservation, ReservationStatus
from redteam_core.models.runtime import (
    RuntimeLifecycle,
    RuntimeState,
    TerminationDecision,
    TerminationKind,
)
from redteam_core.reasoning.focused_context import (
    FocusedContextPolicy,
    project_focused_context_retrieval,
    project_focused_decision_context,
)
from redteam_core.reasoning.working_memory import (
    apply_working_memory_patch,
    reconcile_working_memory,
)
from redteam_core.runtime.completion import assess_completion
from redteam_core.runtime.journal import RuntimeEventType
from redteam_core.runtime.operational_budget import (
    OperationalBudgetExceeded,
    reserve_request,
    synchronize_operational_usage,
    transition_request,
)
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.replan import evaluate_replan_events


class DecisionDriver(Protocol):
    descriptor: DecisionDriverDescriptor

    async def propose(
        self,
        context: str,
        *,
        repair_feedback: str | None = None,
    ) -> DecisionDriverReply | object: ...


class StepExecutionResult(SchemaModel):
    outcome: DecisionStepOutcome
    receipt: DecisionStepReceipt | None = None
    completion: CompletionAssessment | None = None
    termination: TerminationDecision | None = None
    detail: str = Field(min_length=1, max_length=500)


def semantic_progress_fingerprint(session: CoreSession) -> str:
    """Exclude selection counters and LLM prose; include truth-relevant state only."""
    payload = {
        "knowledge": {
            requirement_id: {
                "status": slot.status.value,
                "effective_payload_hash": slot.effective_payload_hash,
                "authoritative_payload_hashes": sorted(slot.authoritative_payload_hashes),
                "has_fresh_inferred": bool(slot.inferred_observation_ids),
                "has_stale": bool(slot.stale_observation_ids),
            }
            for requirement_id, slot in sorted(session.knowledge.slots.items())
        },
        "expectations": [
            {
                "id": item.expectation_id,
                "hash": item.payload.content_hash,
                "trust": item.provenance.trust_class.value,
                "evidence": sorted(item.evidence_ids),
            }
            for item in sorted(
                session.expectations.expectations.values(),
                key=lambda row: row.expectation_id,
            )
        ],
        "requirements": sorted(session.investigation.requirements),
        "observations": sorted(
            {
                (
                    item.requirement_id,
                    item.resolver_id,
                    item.payload.content_hash,
                    item.provenance.trust_class.value,
                    item.validity.snapshot_ref,
                    item.validity.not_after.isoformat() if item.validity.not_after else None,
                    tuple(sorted(item.evidence_ids)),
                )
                for item in session.investigation.observations.values()
            },
            key=repr,
        ),
        "failures": sorted(
            {
                (
                    item.requirement_id,
                    item.resolver_id,
                    item.kind.value,
                    item.retryable,
                )
                for item in session.investigation.resolution_failures.values()
            },
            key=repr,
        ),
        "semantic": session.semantic.model_dump(mode="json"),
        "candidates": [
            {
                "id": item.candidate_id,
                "claim": item.current_claim_status.value,
                "disposition": item.disposition.value,
                "support": sorted(item.supporting_relation_ids),
                "contradiction": sorted(item.contradicting_relation_ids),
                "guards": sorted(item.guard_relation_ids),
                "requirements": sorted(item.requirement_ids),
                "expectations": sorted(item.expectation_ids),
                "evidence": sorted(item.evidence_ids),
                "grounding": sorted(item.grounding_refs),
                "payload": item.candidate_payload.content_hash if item.candidate_payload else None,
            }
            for item in sorted(
                session.candidate_state.candidates.values(), key=lambda row: row.candidate_id
            )
        ],
        "validation": sorted(
            {
                (
                    item.validator_id,
                    item.invariant_id,
                    item.candidate_id,
                    item.experiment_result_id,
                    item.verdict.value,
                    tuple(sorted(item.evidence_ids)),
                )
                for item in session.validation.outcomes.values()
            },
            key=repr,
        ),
        "execution": {
            "attempts": sorted(
                {
                    (
                        item.experiment_id,
                        item.status.value,
                        item.result_id,
                        item.failure_reason_code,
                    )
                    for item in session.execution.attempts.values()
                },
                key=repr,
            ),
            "results": sorted(
                {
                    (
                        item.experiment_id,
                        item.succeeded,
                        item.result_payload.content_hash if item.result_payload else None,
                        tuple(sorted(item.evidence_ids)),
                    )
                    for item in session.execution.results.values()
                },
                key=repr,
            ),
            "cleanup": sorted(
                (item.cleanup_requirement_id, item.status.value)
                for item in session.execution.cleanup_outcomes.values()
            ),
            "stop_reason": session.execution.stop_reason,
        },
    }
    return "sha256:" + sha256_text(canonical_json(payload))


def _copy_runtime(target: RuntimeState, source: RuntimeState) -> None:
    validated = RuntimeState.model_validate(source.model_dump(mode="python"))
    target.__dict__.update(deepcopy(validated.__dict__))


def _copy_session(target: CoreSession, source: CoreSession) -> None:
    validated = CoreSession.model_validate(source.model_dump(mode="python"))
    target.__dict__.update(deepcopy(validated.__dict__))


class GenericStepEngine:
    def __init__(
        self,
        *,
        session: CoreSession,
        runtime: RuntimeState,
        orchestrator: RuntimeOrchestrator,
        driver: DecisionDriver,
        active_candidate_ids: tuple[str, ...] = (),
        scope_completion: ScopeCompletion | None = None,
        context_policy: FocusedContextPolicy | None = None,
        progress_policy: ProgressPolicy | None = None,
        instruction_epoch: str = "initial",
        authority_epoch: str = "initial",
        experiment_plans: dict[str, ExperimentDescriptor] | None = None,
        passive_scheduler: PassiveResolutionScheduler | None = None,
        recomputers: tuple[SemanticRecomputer, ...] = (),
        semantic_pipeline_hash: str | None = None,
        count_tokens: Callable[[str], int],
    ):
        self.session = session
        self.runtime = runtime
        self.orchestrator = orchestrator
        self.driver = driver
        self.active_candidate_ids = active_candidate_ids
        self.scope_completion = scope_completion or ScopeCompletion()
        self.context_policy = context_policy or FocusedContextPolicy()
        self.instruction_epoch = instruction_epoch
        self.authority_epoch = authority_epoch
        self.experiment_plans = {
            candidate_id: ExperimentDescriptor.model_validate(descriptor.model_dump(mode="python"))
            for candidate_id, descriptor in (experiment_plans or {}).items()
        }
        self.experiment_plan_catalog_hash = "sha256:" + sha256_text(
            canonical_json(
                {
                    candidate_id: descriptor.descriptor_hash
                    for candidate_id, descriptor in sorted(self.experiment_plans.items())
                }
            )
        )
        self.passive_scheduler = passive_scheduler
        self.recomputers = tuple(recomputers)
        self.semantic_pipeline_hash = semantic_pipeline_hash
        if self.recomputers and semantic_pipeline_hash is None:
            raise ValueError("configured recomputers require a versioned pipeline fingerprint")
        if len(self.recomputers) > 256 or len({r.recomputer_id for r in self.recomputers}) != len(
            self.recomputers
        ):
            raise ValueError("recomputers must be bounded and unique")
        self.count_tokens = count_tokens
        self._broken = False
        self._running = False
        if (
            not instruction_epoch
            or len(instruction_epoch) > 200
            or not authority_epoch
            or len(authority_epoch) > 200
        ):
            raise ValueError("instruction/authority epochs must be nonempty and bounded")
        if runtime.lifecycle is not RuntimeLifecycle.RUNNING:
            raise RuntimeError("step engine requires a running runtime")
        if runtime.operational_budget is None:
            raise RuntimeError("step engine requires the durable operational budget")
        if passive_scheduler is not None and (
            passive_scheduler.dispatcher.session is not session
            or passive_scheduler.dispatcher.runtime is not runtime
            or passive_scheduler.dispatcher.orchestrator is not orchestrator
        ):
            raise RuntimeError("passive scheduler is not bound to this step runtime")
        proposed_progress = runtime.progress
        if progress_policy is not None and runtime.progress.policy != progress_policy:
            pristine = (
                runtime.progress == ProgressState(policy=runtime.progress.policy)
                and not runtime.decision_responses
            )
            if not pristine:
                raise RuntimeError("progress policy differs from durable state")
            proposed_progress = ProgressState(policy=progress_policy)
        self._check_composition(progress_policy=proposed_progress.policy)
        if runtime.progress.active_attempt_id is not None:
            reservation = next(
                (
                    row
                    for row in runtime.operational_budget.reservations
                    if row.attempt_id == runtime.progress.active_attempt_id
                ),
                None,
            )
            if reservation is None or reservation.status not in {
                ReservationStatus.DISPATCHED,
                ReservationStatus.UNCERTAIN,
            }:
                raise RuntimeError("durable active decision attempt is inconsistent")
        if proposed_progress is not runtime.progress:
            runtime.progress = proposed_progress

    def _check_composition(self, *, progress_policy: ProgressPolicy | None = None) -> None:
        expected = self.orchestrator.composition_fingerprint
        context_hash = "sha256:" + sha256_text(
            canonical_json(self.context_policy.model_dump(mode="json"))
        )
        progress_hash = "sha256:" + sha256_text(
            canonical_json(
                (progress_policy or self.runtime.progress.policy).model_dump(mode="json")
            )
        )
        if (
            expected.decision_driver_hash != self.driver.descriptor.descriptor_hash
            or expected.semantic_pipeline_hash != self.semantic_pipeline_hash
            or expected.context_policy_hash != context_hash
            or expected.progress_policy_hash != progress_hash
        ):
            raise RuntimeError("decision driver/context/progress composition mismatch")

    def _count_input_tokens(self, content: str, *, repair_feedback: str | None = None) -> int:
        measured_content = self.count_tokens(
            canonical_json(
                {
                    "context": content,
                    "repair_feedback": repair_feedback,
                }
            )
        )
        if type(measured_content) is not int or measured_content < 0:
            raise ValueError("decision tokenizer adapter must return a nonnegative integer")
        measured = measured_content + self.driver.descriptor.structured_output_schema_tokens
        if measured > self.driver.descriptor.max_input_tokens:
            raise OperationalBudgetExceeded("decision context exceeds driver input budget")
        return measured

    def _check_global_projection(self, ledger) -> None:
        limits = self.orchestrator.budget_limits
        requests = (
            self.runtime.usage.requests
            + ledger.charged_requests
            - self.runtime.operational_accounted_requests
        )
        tokens = (
            self.runtime.usage.tokens
            + ledger.charged_tokens
            - self.runtime.operational_accounted_tokens
        )
        if limits.max_requests is not None and requests > limits.max_requests:
            raise OperationalBudgetExceeded("combined runtime request budget")
        if limits.max_tokens is not None:
            if any(
                not row.token_bound_guaranteed and row.status is not ReservationStatus.RELEASED
                for row in ledger.reservations
            ):
                raise OperationalBudgetExceeded("combined runtime hard token bound unsupported")
            if tokens > limits.max_tokens:
                raise OperationalBudgetExceeded("combined runtime token budget")

    def _persist(
        self,
        runtime: RuntimeState,
        *,
        reason: str,
        session: CoreSession | None = None,
    ) -> None:
        if self._broken:
            raise RuntimeError("decision engine storage failed; reload and recover")
        try:
            working_session = session or self.session
            runtime = RuntimeState.model_validate(runtime.model_dump(mode="python"))
            self.orchestrator.checkpoint(working_session, runtime, reason=reason)
            if session is not None:
                _copy_session(self.session, session)
            _copy_runtime(self.runtime, runtime)
        except BaseException:
            self._broken = True
            raise

    def _phase(
        self,
        phase: DecisionPhase,
        *,
        active_attempt_id: str | None = None,
    ) -> None:
        candidate = self.runtime.model_copy(deep=True)
        candidate.progress = candidate.progress.model_copy(
            update={"phase": phase, "active_attempt_id": active_attempt_id}
        )
        candidate.revision += 1
        self._persist(candidate, reason=f"decision-phase:{phase.value.lower()}")

    def _context(self) -> str:
        if self.active_candidate_ids:
            return project_focused_decision_context(
                self.session,
                active_candidate_ids=self.active_candidate_ids,
                policy=self.context_policy,
                count_tokens=self.count_tokens
                if self.context_policy.max_total_tokens is not None
                else None,
                allowed_action_ids=tuple(item.value for item in DecisionAction),
                prior_retrieval_contents=tuple(
                    item.content for item in self.runtime.progress.context_retrievals[-4:]
                ),
            ).content
        return canonical_json(
            {
                "channels": {
                    "CANONICAL_STATE": {
                        "active_candidate_ids": [],
                        "allowed_action_ids": [item.value for item in DecisionAction],
                        "revisions": {
                            "knowledge": self.session.knowledge.revision,
                            "candidate": self.session.candidate_state.revision,
                            "working_memory": self.session.working_memory.revision,
                            "expectation": self.session.expectations.revision,
                        },
                    }
                },
                "projection": {"contract": "EMPTY_PORTFOLIO_NOT_CLEAN_RESULT"},
            }
        )

    def _control_fingerprint(self) -> str:
        return "sha256:" + sha256_text(
            canonical_json(
                {
                    "instruction_epoch": self.instruction_epoch,
                    "authority_epoch": self.authority_epoch,
                    "experiment_plans": {
                        candidate_id: descriptor.descriptor_hash
                        for candidate_id, descriptor in sorted(self.experiment_plans.items())
                    },
                    "scope": self.scope_completion.model_dump(mode="json"),
                    "active_candidate_ids": sorted(self.active_candidate_ids),
                }
            )
        )

    def _input_fingerprint(self, context: str) -> str:
        return "sha256:" + sha256_text(
            canonical_json(
                {
                    "semantic": semantic_progress_fingerprint(self.session),
                    "context_hash": sha256_text(context),
                    "control_fingerprint": self._control_fingerprint(),
                    "instruction_epoch": self.instruction_epoch,
                    "authority_epoch": self.authority_epoch,
                    "experiment_plan_catalog_hash": self.experiment_plan_catalog_hash,
                    "scope": self.scope_completion.model_dump(mode="json"),
                }
            )
        )

    def _event(self, event_type: RuntimeEventType, reservation: RequestReservation) -> None:
        ledger = self.runtime.operational_budget
        assert ledger is not None
        try:
            self.orchestrator.journal.append(
                event_type,
                attributes={
                    "attempt_id": reservation.attempt_id,
                    "provider_id": reservation.provider_id,
                    "resolver_id": reservation.resolver_id,
                    "composition_hash": ledger.composition_hash,
                    "limits_hash": ledger.limits_hash,
                },
            )
        except BaseException:
            self._broken = True
            raise

    def _termination(self, kind: TerminationKind, reason: str) -> TerminationDecision:
        return TerminationDecision.create(
            kind=kind,
            reason=reason,
            evaluation_time=self.orchestrator.clock.now(),
        )

    async def _request(
        self,
        context: str,
        *,
        step_index: int,
        ordinal: int,
        repair_feedback: str | None,
        input_fingerprint: str,
        before_fingerprint: str,
    ) -> tuple[DecisionDriverReply | None, str]:
        descriptor = DecisionDriverDescriptor.model_validate(
            self.driver.descriptor.model_dump(mode="python")
        )
        self._check_composition()
        measured_input_tokens = self._count_input_tokens(context, repair_feedback=repair_feedback)
        control_fingerprint = self._control_fingerprint()
        attempt_id = f"decision:{step_index}:{ordinal}"
        reservation = RequestReservation(
            attempt_id=attempt_id,
            provider_id=descriptor.provider_id,
            resolver_id=descriptor.driver_id,
            reserved_tokens=descriptor.max_input_tokens + descriptor.max_output_tokens,
            token_bound_guaranteed=descriptor.hard_output_token_bound,
            reserved_receipt_bytes=descriptor.max_response_bytes,
        )
        if len(self.runtime.decision_responses) >= 512 or (
            sum(item.serialized_bytes for item in self.runtime.decision_responses)
            + descriptor.max_response_bytes
            > 67_108_864
        ):
            raise OperationalBudgetExceeded("durable decision response capacity exhausted")
        runtime = self.runtime.model_copy(deep=True)
        ledger = runtime.operational_budget
        assert ledger is not None
        runtime.operational_budget = reserve_request(
            ledger, reservation, expected_revision=ledger.revision
        )
        self._check_global_projection(runtime.operational_budget)
        runtime.progress = runtime.progress.model_copy(
            update={"phase": DecisionPhase.RESERVE, "active_attempt_id": attempt_id}
        )
        runtime.revision += 1
        self._persist(runtime, reason="decision-request-reserved")
        runtime = self.runtime.model_copy(deep=True)
        ledger = runtime.operational_budget
        assert ledger is not None
        runtime.operational_budget = transition_request(
            ledger,
            attempt_id,
            ReservationStatus.DISPATCHED,
            expected_revision=ledger.revision,
        )
        runtime.progress = runtime.progress.model_copy(update={"phase": DecisionPhase.DISPATCH})
        runtime = synchronize_operational_usage(runtime)
        runtime.revision += 1
        self._persist(runtime, reason="decision-request-dispatched")
        self._event(RuntimeEventType.OPERATIONAL_DISPATCH_INTENT, reservation)
        response_seen = False
        actual_tokens = None
        try:
            raw = await self.driver.propose(context, repair_feedback=repair_feedback)
            response_seen = True
            reply = DecisionDriverReply.model_validate(
                raw.model_dump(mode="python") if isinstance(raw, SchemaModel) else raw
            )
            # Preserve validated provider metering even if later byte/token checks reject it.
            actual_tokens = reply.input_tokens + reply.output_tokens
            encoded = canonical_json(reply.model_dump(mode="json")).encode("utf-8")
            measured_output_tokens = self.count_tokens(canonical_json(reply.payload))
            if type(measured_output_tokens) is not int or measured_output_tokens < 0:
                raise ValueError("decision tokenizer adapter must return a nonnegative integer")
            actual_tokens = max(reply.input_tokens, measured_input_tokens) + max(
                reply.output_tokens, measured_output_tokens
            )
            if len(encoded) > descriptor.max_response_bytes:
                raise ValueError("decision response exceeds byte reservation")
            if reply.input_tokens > descriptor.max_input_tokens:
                raise ValueError("decision input token report exceeds descriptor")
            if reply.output_tokens > descriptor.max_output_tokens:
                raise ValueError("decision output token report exceeds descriptor")
            response_receipt = DecisionResponseReceipt.create(
                run_id=self.runtime.run_id,
                attempt_id=attempt_id,
                step_index=step_index,
                ordinal=ordinal,
                composition_hash=ledger.composition_hash,
                limits_hash=ledger.limits_hash,
                driver_hash=descriptor.descriptor_hash,
                control_fingerprint=control_fingerprint,
                input_fingerprint=input_fingerprint,
                before_fingerprint=before_fingerprint,
                reply=reply,
            )
            if response_receipt.serialized_bytes > descriptor.max_response_bytes:
                raise ValueError("decision response receipt exceeds byte reservation")
        except (ValidationError, ValueError, TypeError):
            runtime = self.runtime.model_copy(deep=True)
            ledger = runtime.operational_budget
            assert ledger is not None
            runtime.progress = runtime.progress.model_copy(
                update={
                    "phase": DecisionPhase.VALIDATE_PROPOSAL
                    if response_seen
                    else DecisionPhase.DISPATCH,
                    "active_attempt_id": None if response_seen else attempt_id,
                }
            )
            runtime.operational_budget = transition_request(
                ledger,
                attempt_id,
                ReservationStatus.FAILED if response_seen else ReservationStatus.UNCERTAIN,
                expected_revision=ledger.revision,
                # Malformed envelopes retain their reservation; validated reports,
                # including overruns, remain charged even when the reply is rejected.
                actual_tokens=actual_tokens if response_seen else None,
            )
            runtime = synchronize_operational_usage(runtime)
            runtime.revision += 1
            self._persist(runtime, reason="decision-response-invalid")
            if response_seen:
                self._event(RuntimeEventType.OPERATIONAL_REQUEST_SETTLED, reservation)
                return None, attempt_id
            raise
        except BaseException:
            runtime = self.runtime.model_copy(deep=True)
            ledger = runtime.operational_budget
            assert ledger is not None
            runtime.operational_budget = transition_request(
                ledger,
                attempt_id,
                ReservationStatus.UNCERTAIN,
                expected_revision=ledger.revision,
            )
            runtime = synchronize_operational_usage(runtime)
            runtime.revision += 1
            self._persist(runtime, reason="decision-completion-uncertain")
            raise
        runtime = self.runtime.model_copy(deep=True)
        ledger = runtime.operational_budget
        assert ledger is not None
        runtime.progress = runtime.progress.model_copy(
            update={"phase": DecisionPhase.VALIDATE_PROPOSAL, "active_attempt_id": None}
        )
        runtime.operational_budget = transition_request(
            ledger,
            attempt_id,
            ReservationStatus.SUCCEEDED,
            expected_revision=ledger.revision,
            actual_tokens=actual_tokens,
        )
        runtime.decision_responses = (*runtime.decision_responses, response_receipt)
        runtime = synchronize_operational_usage(runtime)
        runtime.revision += 1
        self._persist(runtime, reason="decision-response-recorded")
        self._event(RuntimeEventType.OPERATIONAL_REQUEST_SETTLED, reservation)
        return reply, attempt_id

    def _validate_proposal(self, proposal: DecisionProposal) -> None:
        expected = (
            self.session.knowledge.revision,
            self.session.candidate_state.revision,
            self.session.working_memory.revision,
            self.session.expectations.revision,
        )
        actual = (
            proposal.expected_knowledge_revision,
            proposal.expected_candidate_revision,
            proposal.expected_working_memory_revision,
            proposal.expected_expectation_revision,
        )
        if actual != expected:
            raise ValueError("proposal revision snapshot is stale or forged")
        if proposal.candidate_id is not None and (
            proposal.candidate_id not in self.session.candidate_state.candidates
        ):
            raise ValueError("proposal references unknown candidate")
        if proposal.requirement_id is not None and (
            proposal.requirement_id not in self.session.investigation.requirements
        ):
            raise ValueError("proposal references unknown requirement")
        if proposal.action is DecisionAction.QUERY_CONTEXT:
            if not proposal.retrieval_ref.startswith(f"candidate:{proposal.candidate_id}:"):
                raise ValueError("query candidate and retrieval ref disagree")
            if len(self.runtime.progress.context_retrievals) >= 64 and all(
                item.retrieval_ref != proposal.retrieval_ref
                for item in self.runtime.progress.context_retrievals
            ):
                raise ValueError("context retrieval ledger capacity exhausted")
            permitted = {
                f"candidate:{candidate_id}:{kind}"
                for candidate_id in self.active_candidate_ids
                for kind in ("relations", "requirements", "observations", "expectations")
            }
            if proposal.retrieval_ref not in permitted:
                raise ValueError("proposal query ref is not in the focused retrieval contract")

    def _action_arguments(self, proposal: DecisionProposal) -> dict[str, object]:
        if proposal.action is DecisionAction.PATCH_WORKING_MEMORY:
            assert proposal.memory_patch is not None
            patch = proposal.memory_patch
            return {
                "action": proposal.action.value,
                "focus_hypothesis_id": patch.focus_hypothesis_id,
                "clear_focus": patch.clear_focus,
                "upserts": [
                    {
                        "hypothesis_id": item.hypothesis_id,
                        "semantic_key": item.semantic_key,
                        "candidate_id": item.candidate_id,
                        "status": item.status.value if item.status else None,
                        "supporting_relation_ids": item.supporting_relation_ids,
                        "contradicting_relation_ids": item.contradicting_relation_ids,
                        "next_check_target_ref": item.next_check.target_ref
                        if item.next_check
                        else None,
                        "clear_next_check": item.clear_next_check,
                    }
                    for item in patch.upserts
                ],
            }
        return proposal.model_dump(
            mode="json",
            exclude={
                "proposal_id",
                "expected_knowledge_revision",
                "expected_candidate_revision",
                "expected_working_memory_revision",
                "expected_expectation_revision",
            },
            exclude_none=True,
        )

    def _classify_progress(
        self,
        *,
        outcome: DecisionStepOutcome,
        action_key: str,
        changed: bool,
    ) -> DecisionStepOutcome:
        policy = self.runtime.progress.policy
        recent = self.runtime.progress.receipts[-policy.recent_window :]
        recent_keys = [row.action_key for row in recent if not row.changed]
        keys = recent_keys[-3:] + [action_key]
        ping_pong = len(keys) == 4 and keys[0] == keys[2] and keys[1] == keys[3]
        if outcome is DecisionStepOutcome.WAITING:
            repeated = sum(
                row.outcome is DecisionStepOutcome.WAITING and row.action_key == action_key
                for row in recent
            )
            if repeated + 1 >= policy.waiting_poll_limit:
                return DecisionStepOutcome.STOP_PARTIAL
            if ping_pong:
                if self.runtime.progress.replan_count < policy.max_replans:
                    return DecisionStepOutcome.REPLAN_REQUIRED
                return DecisionStepOutcome.STOP_PARTIAL
            return outcome
        if changed:
            return DecisionStepOutcome.PROGRESSED
        if outcome in {
            DecisionStepOutcome.FINISH_REQUESTED,
            DecisionStepOutcome.STOP_PARTIAL,
        }:
            return outcome
        no_progress = [row for row in recent if not row.changed]
        identical = sum(row.action_key == action_key for row in no_progress) + 1
        if identical >= policy.identical_noop_threshold or ping_pong:
            if self.runtime.progress.replan_count < policy.max_replans:
                return DecisionStepOutcome.REPLAN_REQUIRED
            return DecisionStepOutcome.STOP_PARTIAL
        return (
            outcome
            if outcome in {DecisionStepOutcome.REJECTED, DecisionStepOutcome.ERROR}
            else DecisionStepOutcome.NO_PROGRESS
        )

    def _record(
        self,
        *,
        proposal: DecisionProposal | None,
        input_fingerprint: str,
        normalized_outcome: str,
        outcome: DecisionStepOutcome,
        before: str,
        after: str,
        attempt_ids: tuple[str, ...],
        session: CoreSession | None = None,
        context_retrieval: ContextRetrievalReceipt | None = None,
        completion: CompletionAssessment | None = None,
    ) -> DecisionStepReceipt:
        action_args = self._action_arguments(proposal) if proposal else {}
        action_key = "sha256:" + sha256_text(
            canonical_json(
                {
                    "action": proposal.action.value if proposal else "INVALID_PROPOSAL",
                    "args": action_args,
                    # Response replay uses the full input fingerprint, but stuck
                    # detection must ignore cosmetic context/revision changes.
                    "semantic_input": before,
                    "controls": self._control_fingerprint(),
                    "outcome": normalized_outcome,
                }
            )
        )
        final_outcome = self._classify_progress(
            outcome=outcome, action_key=action_key, changed=before != after
        )
        progress = self.runtime.progress
        receipt = DecisionStepReceipt.create(
            step_index=progress.next_step_index,
            proposal_id=proposal.proposal_id if proposal else None,
            action=proposal.action if proposal else None,
            input_fingerprint=input_fingerprint,
            action_key=action_key,
            normalized_outcome=normalized_outcome,
            outcome=final_outcome,
            changed=before != after,
            request_attempt_ids=attempt_ids,
            before_fingerprint=before,
            after_fingerprint=after,
        )
        runtime = self.runtime.model_copy(deep=True)
        completion_record = (
            CompletionAssessmentRecord.create(
                step_index=receipt.step_index,
                input_fingerprint=input_fingerprint,
                before_fingerprint=before,
                scope_hash="sha256:"
                + sha256_text(canonical_json(self.scope_completion.model_dump(mode="json"))),
                assessment=completion,
            )
            if completion is not None
            else None
        )
        runtime.progress = runtime.progress.model_copy(
            update={
                "phase": DecisionPhase.IDLE,
                "active_attempt_id": None,
                "next_step_index": progress.next_step_index + 1,
                "replan_count": progress.replan_count
                + (1 if final_outcome is DecisionStepOutcome.REPLAN_REQUIRED else 0),
                "receipts": (*progress.receipts, receipt),
                "context_retrievals": (
                    *(
                        item
                        for item in progress.context_retrievals
                        if context_retrieval is None
                        or item.retrieval_ref != context_retrieval.retrieval_ref
                    ),
                    *((context_retrieval,) if context_retrieval is not None else ()),
                ),
            }
        )
        runtime.usage = runtime.usage.model_copy(update={"rounds": runtime.usage.rounds + 1})
        if completion_record is not None:
            runtime.completion_assessments = (
                *runtime.completion_assessments,
                completion_record,
            )
        runtime.revision += 1
        self._persist(runtime, reason="decision-step-recorded", session=session)
        return receipt

    @performance_span(
        PerformanceStage.DECISION, input_items=lambda self: len(self.active_candidate_ids)
    )
    async def run_step(self) -> StepExecutionResult:
        if self._broken:
            raise RuntimeError("decision engine storage failed; reload and recover")
        if self._running:
            raise RuntimeError("decision step already has a writer")
        if self.runtime.lifecycle is not RuntimeLifecycle.RUNNING:
            raise RuntimeError("step engine requires a running runtime")
        max_rounds = self.orchestrator.budget_limits.max_rounds
        if max_rounds is not None and self.runtime.usage.rounds >= max_rounds:
            return StepExecutionResult(
                outcome=DecisionStepOutcome.STOP_PARTIAL,
                termination=self._termination(
                    TerminationKind.BUDGET_STOP, "runtime round budget exhausted"
                ),
                detail="runtime round budget exhausted before decision dispatch",
            )
        if len(self.runtime.progress.receipts) >= 256:
            return StepExecutionResult(
                outcome=DecisionStepOutcome.STOP_PARTIAL,
                termination=self._termination(
                    TerminationKind.BUDGET_STOP, "durable step receipt capacity exhausted"
                ),
                detail="durable step receipt capacity exhausted before decision dispatch",
            )
        if self.runtime.progress.receipts and self.runtime.progress.receipts[-1].outcome in {
            DecisionStepOutcome.STOP_PARTIAL,
            DecisionStepOutcome.FINISH_REQUESTED,
        }:
            raise RuntimeError("decision loop stopped; finalize the run")
        if self.session.execution.stop_reason:
            return StepExecutionResult(
                outcome=DecisionStepOutcome.STOP_PARTIAL,
                termination=self._termination(
                    TerminationKind.POLICY_STOP, self.session.execution.stop_reason
                ),
                detail="execution policy stopped the run before decision dispatch",
            )
        self._running = True
        try:
            return await self._run_step()
        finally:
            self._running = False

    def _refresh_expired_knowledge(self) -> None:
        now = self.orchestrator.clock.now()
        expired = set()
        for requirement_id, slot in self.session.knowledge.slots.items():
            requirement = self.session.investigation.requirements.get(requirement_id)
            if requirement is None:
                raise ValueError("knowledge slot lacks canonical requirement")
            for observation_id in slot.observation_ids:
                observation = self.session.investigation.observations.get(observation_id)
                if observation is None:
                    raise ValueError("knowledge slot lacks canonical observation")
                if not _is_fresh(requirement, observation, now):
                    expired.add(requirement_id)
        if not expired:
            return
        producer_ids = {item.recomputer_id for item in self.recomputers}
        for relation in self.session.semantic.relations.values():
            if expired.intersection(relation.requirement_ids) and (
                self.session.semantic.relation_producers.get(relation.relation_id)
                not in producer_ids
            ):
                raise RuntimeError("expired relation requires its bound semantic recomputer")
        working = self.session.model_copy(deep=True)
        refresh_freshness(
            working,
            evaluation_time=now,
            expected_revision=working.knowledge.revision,
            catalog=self.orchestrator.catalog,
            recomputers=self.recomputers,
        )
        reconcile_working_memory(working)
        runtime = self.runtime.model_copy(deep=True)
        runtime.revision += 1
        self._persist(runtime, reason="decision-freshness-refreshed", session=working)

    async def _run_step(self) -> StepExecutionResult:
        if self.runtime.progress.active_attempt_id is not None:
            return StepExecutionResult(
                outcome=DecisionStepOutcome.WAITING,
                detail="prior decision dispatch is uncertain; automatic replay is forbidden",
            )
        self._check_composition()
        self._refresh_expired_knowledge()
        self._phase(DecisionPhase.SNAPSHOT)
        context = self._context()
        input_fingerprint = self._input_fingerprint(context)
        before = semantic_progress_fingerprint(self.session)
        try:
            self._count_input_tokens(context)
        except OperationalBudgetExceeded:
            receipt = self._record(
                proposal=None,
                input_fingerprint=input_fingerprint,
                normalized_outcome="DECISION_CONTEXT_BUDGET_EXHAUSTED",
                outcome=DecisionStepOutcome.STOP_PARTIAL,
                before=before,
                after=before,
                attempt_ids=(),
            )
            return StepExecutionResult(
                outcome=receipt.outcome,
                receipt=receipt,
                termination=self._termination(
                    TerminationKind.BUDGET_STOP, "decision context budget exhausted"
                ),
                detail="decision context exceeds the driver input budget",
            )
        self._phase(DecisionPhase.PROPOSE)
        consumed_attempt_ids = {
            attempt_id
            for receipt in self.runtime.progress.receipts
            for attempt_id in receipt.request_attempt_ids
        }
        pending_responses = [
            item
            for item in self.runtime.decision_responses
            if item.attempt_id not in consumed_attempt_ids
        ]
        if len(pending_responses) > self.runtime.progress.policy.malformed_repair_limit + 1:
            raise RuntimeError("too many unconsumed decision responses")
        historical_resolution = None
        for response in pending_responses:
            try:
                historical_proposal = DecisionProposal.model_validate(response.reply.payload)
            except (ValidationError, ValueError):
                continue
            if historical_proposal.action is not DecisionAction.RESOLVE_REQUIREMENT:
                continue
            batch_id = stable_id(
                "decision_batch",
                self.runtime.run_id,
                self.runtime.progress.next_step_index,
            )
            batch = next(
                (item for item in self.runtime.resolution_batches if item.batch_id == batch_id),
                None,
            )
            if batch is None or not batch.complete or len(batch.items) != 1:
                continue
            batch_item = batch.items[0]
            if (
                batch_item.requirement.requirement_id == historical_proposal.requirement_id
                and f"decision:{historical_proposal.proposal_id}" in batch_item.waiter_ids
            ):
                historical_resolution = (historical_proposal, response)
                break
        if any(
            item.driver_hash != self.driver.descriptor.descriptor_hash
            or item.control_fingerprint != self._control_fingerprint()
            or (
                historical_resolution is None
                and (
                    item.input_fingerprint != input_fingerprint or item.before_fingerprint != before
                )
            )
            for item in pending_responses
        ):
            raise RuntimeError("stored decision response no longer matches canonical input")
        attempt_ids = [item.attempt_id for item in pending_responses]
        proposal = historical_resolution[0] if historical_resolution else None
        if historical_resolution is not None:
            input_fingerprint = historical_resolution[1].input_fingerprint
            before = historical_resolution[1].before_fingerprint
        evaluation = next(
            (
                row
                for row in self.runtime.progress.replan_evaluations
                if row.step_index == self.runtime.progress.next_step_index
            ),
            None,
        )
        if evaluation is None:
            evaluation = evaluate_replan_events(
                self.session, self.runtime, self.orchestrator.budget_limits
            )
            updated = self.runtime.model_copy(deep=True)
            updated.progress = updated.progress.model_copy(
                update={
                    "replan_evaluations": (*updated.progress.replan_evaluations, evaluation),
                }
            )
            updated.revision += 1
            self._persist(updated, reason="decision-replan-evaluated")
        replan_feedback = (
            "Prior actions made no semantic progress. Select a materially different bounded action."
            if self.runtime.progress.receipts
            and self.runtime.progress.receipts[-1].outcome is DecisionStepOutcome.REPLAN_REQUIRED
            else None
        )
        if evaluation.reasons:
            replan_feedback = (
                (replan_feedback + " " if replan_feedback else "")
                + "Reassess bounded action priorities after canonical events: "
                + ",".join(reason.value for reason in evaluation.reasons)
                + ". This does not authorize effects or change truth."
            )
        ordinal_start = len(pending_responses)
        replies = [item.reply for item in pending_responses]
        for reply in () if historical_resolution else replies:
            try:
                proposal = DecisionProposal.model_validate(reply.payload)
                self._validate_proposal(proposal)
                break
            except (ValidationError, ValueError):
                proposal = None
        for ordinal in range(
            ordinal_start, self.runtime.progress.policy.malformed_repair_limit + 1
        ):
            if proposal is not None:
                break
            try:
                reply, attempt_id = await self._request(
                    context,
                    step_index=self.runtime.progress.next_step_index,
                    ordinal=ordinal,
                    repair_feedback=(
                        replan_feedback
                        if ordinal == 0
                        else "Return one schema-valid action bound to the supplied revisions."
                    ),
                    input_fingerprint=input_fingerprint,
                    before_fingerprint=before,
                )
            except OperationalBudgetExceeded:
                receipt = self._record(
                    proposal=None,
                    input_fingerprint=input_fingerprint,
                    normalized_outcome="DECISION_BUDGET_EXHAUSTED",
                    outcome=DecisionStepOutcome.STOP_PARTIAL,
                    before=before,
                    after=before,
                    attempt_ids=tuple(attempt_ids),
                )
                return StepExecutionResult(
                    outcome=receipt.outcome,
                    receipt=receipt,
                    termination=self._termination(
                        TerminationKind.BUDGET_STOP,
                        "operational decision request budget exhausted",
                    ),
                    detail="operational request budget exhausted before decision dispatch",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._broken or self.runtime.progress.active_attempt_id is None:
                    raise
                return StepExecutionResult(
                    outcome=DecisionStepOutcome.WAITING,
                    detail="decision completion is uncertain; no automatic replay",
                )
            attempt_ids.append(attempt_id)
            if reply is None:
                continue
            try:
                proposal = DecisionProposal.model_validate(reply.payload)
                self._validate_proposal(proposal)
                break
            except (ValidationError, ValueError):
                proposal = None
        if proposal is None:
            receipt = self._record(
                proposal=None,
                input_fingerprint=input_fingerprint,
                normalized_outcome="INVALID_PROPOSAL_AFTER_BOUNDED_REPAIR",
                outcome=DecisionStepOutcome.REJECTED,
                before=before,
                after=before,
                attempt_ids=tuple(attempt_ids),
            )
            return StepExecutionResult(
                outcome=receipt.outcome,
                receipt=receipt,
                detail="proposal rejected after bounded repair",
            )
        self._phase(DecisionPhase.RECORD_COMMIT)
        working = self.session.model_copy(deep=True)
        completion = None
        context_retrieval = None
        normalized = "NO_CANONICAL_CHANGE"
        outcome = DecisionStepOutcome.NO_PROGRESS
        if proposal.action is DecisionAction.PATCH_WORKING_MEMORY:
            assert proposal.memory_patch is not None
            update = apply_working_memory_patch(working, proposal.memory_patch)
            normalized = "MEMORY_PATCH_ACCEPTED" if update.accepted else "MEMORY_PATCH_REJECTED"
            # LLM prose and focus edits are not semantic progress.
        elif proposal.action is DecisionAction.RESOLVE_REQUIREMENT:
            if self.passive_scheduler is None:
                normalized = f"REQUIREMENT_WAITING:{proposal.requirement_id}"
                outcome = DecisionStepOutcome.WAITING
            else:
                batch_id = stable_id(
                    "decision_batch",
                    self.runtime.run_id,
                    self.runtime.progress.next_step_index,
                )
                existing_batch = next(
                    (item for item in self.runtime.resolution_batches if item.batch_id == batch_id),
                    None,
                )
                if existing_batch is None:
                    self.passive_scheduler.admit(
                        batch_id=batch_id,
                        requests=((f"decision:{proposal.proposal_id}", proposal.requirement_id),),
                    )
                    batch = await self.passive_scheduler.run(batch_id)
                elif existing_batch.complete:
                    batch = existing_batch
                else:
                    batch = await self.passive_scheduler.run(batch_id)
                item = batch.items[0]
                if item.receipt_hash is None:
                    normalized = f"REQUIREMENT_FAILED:{item.failure_kind.value}"
                else:
                    commit = self.passive_scheduler.commit(
                        batch_id,
                        expected_revision=self.session.knowledge.revision,
                        recomputers=self.recomputers,
                    )
                    normalized = "REQUIREMENT_COMMITTED:" + ",".join(
                        commit.recorded_observation_ids
                    )
                    working = self.session.model_copy(deep=True)
        elif proposal.action is DecisionAction.QUERY_CONTEXT:
            assert proposal.retrieval_ref is not None
            projected = project_focused_context_retrieval(
                working, retrieval_ref=proposal.retrieval_ref
            )
            context_retrieval = ContextRetrievalReceipt.create(
                step_index=self.runtime.progress.next_step_index,
                retrieval_ref=projected.retrieval_ref,
                complete=projected.complete,
                content=projected.content,
                utf8_bytes=projected.utf8_bytes,
            )
            normalized = "CONTEXT_RETRIEVED" if projected.complete else "CONTEXT_INCOMPLETE"
        elif proposal.action is DecisionAction.REQUEST_EXPERIMENT_PLAN:
            assert proposal.candidate_id is not None
            descriptor = self.experiment_plans.get(proposal.candidate_id)
            if descriptor is None:
                normalized = f"EXPERIMENT_PLAN_WAITING:{proposal.candidate_id}"
                outcome = DecisionStepOutcome.WAITING
            else:
                existing_attempts = sorted(
                    (
                        item
                        for item in working.execution.attempts.values()
                        if item.experiment_id == descriptor.experiment_id
                    ),
                    key=lambda item: item.sequence,
                )
                if existing_attempts:
                    attempt = existing_attempts[-1]
                    normalized = f"EXPERIMENT_ALREADY_PLANNED:{attempt.attempt_id}"
                else:
                    attempt = plan_experiment(
                        working,
                        descriptor=descriptor,
                        catalog=self.orchestrator.catalog,
                        clock=self.orchestrator.clock,
                    )
                    normalized = f"EXPERIMENT_PLANNED:{attempt.attempt_id}"
        elif proposal.action is DecisionAction.FINISH:
            completion = assess_completion(
                working,
                scope=self.scope_completion,
                priority=self.runtime.priority,
                evaluation_time=self.orchestrator.clock.now(),
            )
            normalized = "FINISH_" + completion.status.value
            outcome = DecisionStepOutcome.FINISH_REQUESTED
        self._phase(DecisionPhase.RECONCILE)
        reconcile_working_memory(working)
        after = semantic_progress_fingerprint(working)
        self._phase(DecisionPhase.ASSESS)
        receipt = self._record(
            proposal=proposal,
            input_fingerprint=input_fingerprint,
            normalized_outcome=normalized,
            outcome=outcome,
            before=before,
            after=after,
            attempt_ids=tuple(attempt_ids),
            session=working,
            context_retrieval=context_retrieval,
            completion=completion,
        )
        policy_stop_reason = self.session.execution.stop_reason
        return StepExecutionResult(
            outcome=receipt.outcome,
            receipt=receipt,
            completion=completion,
            termination=(
                self._termination(TerminationKind.POLICY_STOP, policy_stop_reason)
                if policy_stop_reason
                else self._termination(TerminationKind.LLM_FINISH, proposal.finish_reason)
                if proposal.action is DecisionAction.FINISH
                else self._termination(
                    TerminationKind.POLICY_STOP,
                    "semantic progress policy stopped the bounded loop",
                )
                if receipt.outcome is DecisionStepOutcome.STOP_PARTIAL
                else None
            ),
            detail=normalized,
        )
