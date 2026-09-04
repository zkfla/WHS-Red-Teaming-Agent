from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from collections import Counter
from pathlib import Path

from cicd_redteam_agent.application.analyze import AnalysisConfig, analyze_repository
from cicd_redteam_agent.domain.graph_frontier import build_attack_frontiers
from cicd_redteam_agent.domain.investigation_policy import (
    decide_investigation_policy,
)
from cicd_redteam_agent.domain.models import (
    AnalysisMode,
    CandidateDisposition,
    CandidateKind,
)
from cicd_redteam_agent.domain.relation_semantics import relation_semantics
from cicd_redteam_agent.domain.security_candidate import (
    derive_security_candidates,
    reconcile_candidate_state,
)
from cicd_redteam_agent.framework.capabilities import CapabilityContext, SubmitAnalysis
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

GOAT_URL = "https://github.com/step-security/github-actions-goat.git"
GOAT_COMMIT = "4da23c8602b4589bacdf7de77240113e02cdb884"
DEFAULT_GOAT_ROOT = (
    Path(__file__).parents[2] / "output" / "benchmark-target" / "github-actions-goat"
)
_TERMINAL_DISPOSITIONS = {
    CandidateDisposition.ACCEPTED.value,
    CandidateDisposition.REJECTED.value,
    CandidateDisposition.BLOCKED_EXTERNAL.value,
    CandidateDisposition.MERGED.value,
}


def _git_value(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot verify benchmark repository with git: {exc}") from exc
    return result.stdout.strip()


def verify_pinned_goat(root: Path) -> str:
    root = root.resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise RuntimeError(f"pinned Goat checkout is missing: {root}")
    origin = _git_value(root, "config", "--get", "remote.origin.url")
    if origin != GOAT_URL:
        raise RuntimeError(f"unexpected Goat origin: {origin!r}")
    commit = _git_value(root, "rev-parse", "HEAD")
    if commit != GOAT_COMMIT:
        raise RuntimeError(f"Goat checkout is not pinned to {GOAT_COMMIT}: {commit}")
    if _git_value(root, "status", "--porcelain"):
        raise RuntimeError("pinned Goat checkout is dirty")
    return commit


def _candidate_projection(candidates) -> list[tuple[object, ...]]:
    return [
        (
            candidate.candidate_id,
            candidate.candidate_kind.value,
            candidate.root_cause_family,
            candidate.root_cause_key,
            candidate.proposal_status_ceiling.value,
            candidate.status.value,
            tuple(candidate.supporting_relation_ids),
        )
        for candidate in candidates
    ]


def _entry_relation_ids(entry, relation_by_ref, relation_by_id) -> set[str]:
    if entry.candidate is not None:
        relation_ids = set(entry.candidate.supporting_relation_ids)
        relation_ids.update(entry.candidate.contradicting_relation_ids)
        relation_ids.update(
            edge.relation_id
            for variant in entry.candidate.path_variants
            for edge in variant.edges
        )
        return {relation_id for relation_id in relation_ids if relation_id in relation_by_id}
    if entry.frontier is not None:
        return {
            relation_by_ref[reference].relation_id
            for reference in entry.frontier.relation_refs
            if reference in relation_by_ref
        }
    return set()


def _portfolio_metrics(policy, state) -> dict[str, object]:
    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    relation_by_ref = {
        reference: relation for reference, relation in _indexed_relations(state)
    }
    relation_sets = [
        _entry_relation_ids(entry, relation_by_ref, relation_by_id) for entry in policy.portfolio
    ]
    mechanisms: set[str] = set()
    sinks: set[str] = set()
    for relation_ids in relation_sets:
        for relation_id in relation_ids:
            semantics = relation_semantics(relation_by_id[relation_id].relation_type)
            mechanisms.update(semantics.mechanism_tags)
            if semantics.sink_tag is not None:
                sinks.add(semantics.sink_tag)
    overlaps = [
        len(left & right)
        for index, left in enumerate(relation_sets)
        for right in relation_sets[index + 1 :]
    ]
    return {
        "size": len(policy.portfolio),
        "contents": [entry.as_dict() for entry in policy.portfolio],
        "unique_root_causes": sorted(
            {
                entry.candidate.root_cause_key
                for entry in policy.portfolio
                if entry.candidate is not None
            }
        ),
        "unique_root_cause_families": sorted(
            {
                entry.candidate.root_cause_family or entry.candidate.root_cause_key
                for entry in policy.portfolio
                if entry.candidate is not None
            }
        ),
        "unique_root_cause_instances": sorted(
            {
                entry.candidate.root_cause_key
                for entry in policy.portfolio
                if entry.candidate is not None
            }
        ),
        "unique_candidate_kinds": sorted(
            {
                entry.candidate.candidate_kind.value
                for entry in policy.portfolio
                if entry.candidate is not None
            }
        ),
        "unique_mechanism_families": sorted(mechanisms),
        "unique_sink_families": sorted(sinks),
        "unique_contexts": sorted(
            {
                entry.candidate.context_id or "<none>"
                for entry in policy.portfolio
                if entry.candidate is not None
            }
            | {
                entry.frontier.job_context_id or "<none>"
                for entry in policy.portfolio
                if entry.frontier is not None
            }
        ),
        "relation_overlap_pairs": overlaps,
        "max_relation_overlap": max(overlaps, default=0),
    }


def _indexed_relations(state):
    return [
        (f"R{index:04d}", relation)
        for index, relation in enumerate(state.relations, start=1)
    ]


async def run_p0_goat_benchmark(
    goat_root: Path = DEFAULT_GOAT_ROOT,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Run the pinned, read-only Goat benchmark and a provider-free reasoning smoke.

    The reasoning portion intentionally does not call an external model. It reconstructs the
    reasoning-mode initial state from the same extracted graph, runs the real portfolio and
    candidate-backed SubmitAnalysis capability, and records the smoke boundary explicitly.
    """

    goat_root = goat_root.resolve()
    commit = verify_pinned_goat(goat_root)
    destination = (output_dir or goat_root.parent.parent / "p0-goat-benchmark").resolve()
    destination.mkdir(parents=True, exist_ok=True)

    deterministic_report, deterministic_run = await analyze_repository(
        goat_root,
        AnalysisConfig(
            output_dir=destination / "deterministic",
            analysis_mode=AnalysisMode.DETERMINISTIC,
        ),
    )
    if deterministic_report.termination_reason != "deterministic_analysis_complete":
        raise RuntimeError(
            "Goat deterministic benchmark did not complete: "
            f"{deterministic_report.termination_reason}"
        )
    deterministic_state = deterministic_report.state

    reasoning_state = deterministic_state.model_copy(deep=True)
    reasoning_state.findings = []
    reasoning_state.paths = []
    reasoning_state.candidates = []
    reasoning_state.coverage_ledger = []
    reasoning_candidates = derive_security_candidates(reasoning_state)
    reasoning_state.candidates, reasoning_state.coverage_ledger = reconcile_candidate_state(
        (), reasoning_candidates
    )
    parity = _candidate_projection(deterministic_state.candidates) == _candidate_projection(
        reasoning_state.candidates
    )
    if not parity:
        raise RuntimeError("deterministic and reasoning initial candidate projections differ")

    # Use the same bounded semantic search defaults as the live capability path. Presentation
    # pagination is separate and must never change search completeness or submit eligibility.
    frontier_result = build_attack_frontiers(reasoning_state)
    initial_policy = decide_investigation_policy(frontier_result, reasoning_state)
    initial_portfolio_metrics = _portfolio_metrics(initial_policy, deterministic_state)
    preferred = (
        CandidateKind.MUTABLE_ACTION,
        CandidateKind.SECRET_LOG,
        CandidateKind.REVISION_TOCTOU,
        CandidateKind.ARTIFACT_EXECUTION,
    )
    context = CapabilityContext(
        state=reasoning_state,
        boundary=RepositoryBoundary(goat_root),
        analysis_mode=AnalysisMode.REASONING,
        investigation_policy=initial_policy,
    )
    all_root_causes = {candidate.root_cause_key for candidate in reasoning_state.candidates}
    batch_records: list[dict[str, object]] = []
    silently_dropped: list[str] = []
    accepted_candidate_id: str | None = None
    accepted_frontier_id: str | None = None
    final_policy = initial_policy
    final_response: dict[str, object] | None = None

    for batch_number in range(1, len(all_root_causes) + 2):
        policy = decide_investigation_policy(frontier_result, reasoning_state)
        context.investigation_policy = policy
        final_policy = policy
        if policy.transition.value == "SUBMIT":
            final_response = json.loads(
                await SubmitAnalysis(context)(json.dumps({"schema_version": "1.0"}))
            )
            if not final_response.get("accepted") or not context.submitted:
                raise RuntimeError(
                    f"final Goat multi-batch submission failed: {final_response}"
                )
            break
        if not policy.decision_submission_allowed:
            raise RuntimeError(
                "Goat multi-batch policy did not allow a complete decision batch: "
                f"{policy.transition.value}: {policy.reason}"
            )

        selected_candidates = [
            entry.candidate for entry in policy.portfolio if entry.candidate is not None
        ]
        accepted_candidate = None
        if accepted_candidate_id is None:
            accepted_candidate = next(
                (
                    candidate
                    for kind in preferred
                    for candidate in selected_candidates
                    if candidate.candidate_kind is kind
                    and candidate.status.value != "REJECTED"
                    and candidate.path_variants
                ),
                None,
            )
            if accepted_candidate is not None:
                accepted_candidate_id = accepted_candidate.candidate_id

        candidate_decisions: list[dict[str, object]] = []
        frontier_decisions: list[dict[str, object]] = []
        path_drafts: list[dict[str, object]] = []
        for entry in policy.portfolio:
            if entry.candidate is not None:
                accept = (
                    accepted_candidate is not None
                    and entry.candidate.candidate_id == accepted_candidate.candidate_id
                )
                decision: dict[str, object] = {
                    "candidate_id": entry.candidate.candidate_id,
                    "decision": (
                        CandidateDisposition.ACCEPTED.value
                        if accept
                        else CandidateDisposition.REJECTED.value
                    ),
                }
                if accept:
                    decision["selected_path_variant"] = entry.candidate.path_variants[0].path_id
                    decision["confidence"] = entry.candidate.confidence.value
                candidate_decisions.append(decision)
            elif entry.frontier is not None:
                accept_frontier = (
                    accepted_frontier_id is None and entry.frontier.proposal_ready
                )
                if accept_frontier:
                    accepted_frontier_id = entry.frontier.frontier_id
                    path_drafts.append(
                        {
                            "path_id": f"path_{entry.frontier.frontier_id}",
                            "relation_refs": list(entry.frontier.relation_refs),
                        }
                    )
                frontier_decisions.append(
                    {
                        "frontier_id": entry.frontier.frontier_id,
                        "decision": (
                            CandidateDisposition.ACCEPTED.value
                            if accept_frontier
                            else CandidateDisposition.REJECTED.value
                        ),
                        "confidence": "HIGH",
                    }
                )

        selected_ids = {entry.item_id for entry in policy.portfolio}
        selected_root_causes = {
            entry.candidate.root_cause_key
            for entry in policy.portfolio
            if entry.candidate is not None
        }
        uncovered_before = {
            root_cause
            for root_cause in all_root_causes
            if any(
                candidate.root_cause_key == root_cause
                and candidate.disposition.value not in _TERMINAL_DISPOSITIONS
                for candidate in reasoning_state.candidates
            )
        }
        smoke_response = json.loads(
            await SubmitAnalysis(context)(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "candidate_decisions": candidate_decisions,
                        "frontier_decisions": frontier_decisions,
                        "path_drafts": path_drafts,
                    }
                )
            )
        )
        if (
            not smoke_response.get("accepted")
            or not smoke_response.get("batch_committed")
            or context.submitted
        ):
            raise RuntimeError(
                f"candidate-backed Goat batch {batch_number} failed: {smoke_response}"
            )

        decision_ids = {
            *(item["candidate_id"] for item in candidate_decisions),
            *(item["frontier_id"] for item in frontier_decisions),
        }
        selected_ledger = {
            entry.item_id: entry
            for entry in reasoning_state.coverage_ledger
            if entry.item_id in selected_ids
        }
        dropped_this_batch = sorted(
            item_id
            for item_id in selected_ids
            if item_id not in decision_ids
            or selected_ledger.get(item_id) is None
            or selected_ledger[item_id].disposition.value not in _TERMINAL_DISPOSITIONS
        )
        silently_dropped.extend(dropped_this_batch)
        uncovered_after = {
            root_cause
            for root_cause in all_root_causes
            if any(
                candidate.root_cause_key == root_cause
                and candidate.disposition.value not in _TERMINAL_DISPOSITIONS
                for candidate in reasoning_state.candidates
            )
        }
        batch_records.append(
            {
                "batch": batch_number,
                "selected_items": sorted(selected_ids),
                "selected_root_causes": sorted(selected_root_causes),
                "uncovered_before": len(uncovered_before),
                "uncovered_after": len(uncovered_after),
                "silently_dropped_items": dropped_this_batch,
            }
        )
        if dropped_this_batch:
            raise RuntimeError(
                "selected Goat portfolio items were silently dropped: "
                f"{dropped_this_batch}"
            )
    else:
        raise RuntimeError("Goat multi-batch coverage did not converge")

    if final_response is None:
        raise RuntimeError("Goat multi-batch coverage never reached final submission")
    uncovered_root_causes = sorted(
        root_cause
        for root_cause in all_root_causes
        if any(
            candidate.root_cause_key == root_cause
            and candidate.disposition.value not in _TERMINAL_DISPOSITIONS
            for candidate in reasoning_state.candidates
        )
    )
    if uncovered_root_causes:
        raise RuntimeError(
            f"Goat multi-batch coverage left root causes uncovered: {uncovered_root_causes}"
        )
    execution_frontiers = sum(
        frontier.sink_tag == "execution" for frontier in frontier_result.frontiers
    )
    privileged_execution_frontiers = sum(
        frontier.sink_tag == "privileged_execution" for frontier in frontier_result.frontiers
    )
    if not execution_frontiers or privileged_execution_frontiers:
        raise RuntimeError(
            "Goat frontier execution/privilege separation failed: "
            f"execution={execution_frontiers}, privileged={privileged_execution_frontiers}"
        )

    candidate_statuses = Counter(
        candidate.status.value for candidate in deterministic_state.candidates
    )
    candidate_dispositions = Counter(
        candidate.disposition.value for candidate in reasoning_state.candidates
    )
    result = {
        "repository": {
            "url": GOAT_URL,
            "commit": commit,
            "root": str(goat_root),
            "workflow_execution": "not_run",
        },
        "deterministic_run": str(deterministic_run),
        "counts": {
            "workflows": len(deterministic_state.workflows),
            "jobs": sum(len(workflow.jobs) for workflow in deterministic_state.workflows),
            "steps": sum(
                len(job.steps)
                for workflow in deterministic_state.workflows
                for job in workflow.jobs
            ),
            "evidence": len(deterministic_state.evidence),
            "relations": len(deterministic_state.relations),
            "candidates": len(deterministic_state.candidates),
            "findings": len(deterministic_state.findings),
            "attack_paths": len(deterministic_state.paths),
            "candidate_family_count": len(
                {candidate.candidate_kind.value for candidate in deterministic_state.candidates}
            ),
            "root_cause_family_count": len(
                {
                    candidate.root_cause_family or candidate.root_cause_key
                    for candidate in deterministic_state.candidates
                }
            ),
            "root_cause_instance_count": len(
                {candidate.root_cause_key for candidate in deterministic_state.candidates}
            ),
            "mutable_action_candidate_count": sum(
                candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
                for candidate in deterministic_state.candidates
            ),
            "mutable_action_distinct_action_ref_count": len(
                {
                    candidate.root_cause_key.removeprefix("mutable-action-reference:")
                    for candidate in deterministic_state.candidates
                    if candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
                }
            ),
            "mutable_candidates_with_zero_supporting_relations": sum(
                not candidate.supporting_relation_ids
                for candidate in deterministic_state.candidates
                if candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
            ),
            "mutable_candidates_with_zero_required_relation_types": sum(
                not candidate.claim_contract.required_relation_types
                for candidate in deterministic_state.candidates
                if candidate.candidate_kind is CandidateKind.MUTABLE_ACTION
            ),
        },
        "candidate_statuses": dict(sorted(candidate_statuses.items())),
        "candidate_dispositions_after_smoke": dict(sorted(candidate_dispositions.items())),
        "candidate_families": dict(
            sorted(
                Counter(
                    candidate.candidate_kind.value
                    for candidate in deterministic_state.candidates
                ).items()
            )
        ),
        "root_causes": sorted(
            {candidate.root_cause_key for candidate in deterministic_state.candidates}
        ),
        "root_cause_families": sorted(
            {
                candidate.root_cause_family or candidate.root_cause_key
                for candidate in deterministic_state.candidates
            }
        ),
        "candidate_parity": {
            "passed": parity,
            "deterministic_count": len(deterministic_state.candidates),
            "reasoning_initial_count": len(reasoning_state.candidates),
        },
        "portfolio": initial_portfolio_metrics,
        "policy": {
            "initial_transition": initial_policy.transition.value,
            "initial_decision_submission_allowed": initial_policy.decision_submission_allowed,
            "final_transition": final_policy.transition.value,
            "search_complete": frontier_result.search_complete,
            "truncated": frontier_result.truncated,
            "frontiers_returned": len(frontier_result.frontiers),
            "expansions_used": frontier_result.expansions_used,
        },
        "coverage": {
            "batch_count": len(batch_records),
            "batches": batch_records,
            "silently_dropped_items": silently_dropped,
            "root_cause_instance_count": len(all_root_causes),
            "covered_root_cause_instances": len(all_root_causes) - len(uncovered_root_causes),
            "uncovered_root_cause_instances": uncovered_root_causes,
            "candidate_coverage_complete_before_submit": final_policy.candidate_coverage_complete,
        },
        "reasoning_smoke": {
            "mode": "candidate-backed-multibatch-framework-smoke-without-external-llm",
            "accepted": bool(final_response["accepted"]),
            "accepted_candidate_id": accepted_candidate_id,
            "rounds": 0,
            "tool_calls": sum(reasoning_state.repeated_calls.values()),
            "observation_count": 0,
            "semantic_refresh_count": 0,
            "token_use": 0,
            "termination_reason": "candidate_multibatch_smoke_proposal_validated",
            "materialized_findings": len(reasoning_state.findings),
            "materialized_attack_paths": len(reasoning_state.paths),
        },
        "frontier_semantics": {
            "execution_frontiers": execution_frontiers,
            "privileged_execution_frontiers": privileged_execution_frontiers,
        },
        "gates": {
            "A_candidate_parity": parity,
            "B_no_silent_drop": not silently_dropped,
            "C_multibatch_root_cause_coverage": not uncovered_root_causes,
            "D_execution_not_privileged": bool(execution_frontiers)
            and not privileged_execution_frontiers,
            "candidate_backed_submit": bool(final_response["accepted"]),
        },
    }
    (destination / "benchmark.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pinned offline P0 Goat benchmark")
    parser.add_argument("--goat-root", type=Path, default=DEFAULT_GOAT_ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    result = asyncio.run(run_p0_goat_benchmark(args.goat_root, args.output))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
