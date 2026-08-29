from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AttackerProfile,
    Job,
    Relation,
    Step,
    TriState,
    Workflow,
)

_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_GH_PR_CHECKOUT = re.compile(r"\bgh\s+pr\s+checkout\b", re.IGNORECASE)
_GIT_FETCH = re.compile(r"\bgit\s+fetch\b[^\r\n;&|]*", re.IGNORECASE)
_GIT_CHECKOUT = re.compile(r"\bgit\s+(?:checkout|switch)\b[^\r\n;&|]*", re.IGNORECASE)
_FETCH_HEAD = re.compile(r"(?<![\w])FETCH_HEAD(?![\w])", re.IGNORECASE)
_PR_HEAD_MARKER = re.compile(
    r"(?:refs/pull/[^\s'\"]+/head|github\.event\.pull_request\.(?:number|head\.ref)|"
    r"github\.head_ref|pull_request\.head|\bPR_(?:NUMBER|HEAD|REF)\b)",
    re.IGNORECASE,
)
_PR_EVENT_SHA = re.compile(r"github\.event\.pull_request\.head\.sha", re.IGNORECASE)
_REPOSITORY_EXECUTION = re.compile(
    r"(?:^|[\s;&|])(?:\./|bash\s+|sh\s+|python(?:3)?\s+|node\s+|pwsh\s+|"
    r"npm\s+(?:test|run|install|ci)\b|pnpm\s+(?:test|run|install)\b|"
    r"yarn\s+(?:test|run|install)\b|pytest\b|make(?:\s|$)|cargo\s+(?:test|run|build)\b|"
    r"go\s+test\b|mvn\s+(?:test|verify|package)\b|gradle(?:w)?\s+|"
    r"dotnet\s+(?:test|build|run)\b|docker\s+build\b)",
    re.IGNORECASE,
)


class WorkspaceRevisionKind(StrEnum):
    TRUSTED_BASE = "TRUSTED_BASE"
    EXACT_COMMIT = "EXACT_COMMIT"
    PR_EVENT_HEAD_SHA = "PR_EVENT_HEAD_SHA"
    PR_CURRENT_HEAD = "PR_CURRENT_HEAD"
    MUTABLE_REF = "MUTABLE_REF"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class WorkspaceRevisionState:
    kind: WorkspaceRevisionKind
    node_id: str
    selector: str
    evidence_ids: tuple[str, ...]
    selected_by_step_id: str


@dataclass(frozen=True)
class _Transition:
    kind: WorkspaceRevisionKind
    selector: str
    end: int


def _relation(
    source_id: str,
    relation_type: str,
    target_id: str,
    evidence_ids: list[str] | tuple[str, ...],
    *,
    category: str,
    verdict: TriState = TriState.TRUE,
    missing_evidence: list[str] | None = None,
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        category=category,
        verdict=verdict,
        evidence_ids=sorted(set(evidence_ids)),
        missing_evidence=missing_evidence or [],
    )


def _revision_state(
    workflow: Workflow,
    job: Job,
    step: Step,
    kind: WorkspaceRevisionKind,
    selector: str,
) -> WorkspaceRevisionState:
    return WorkspaceRevisionState(
        kind=kind,
        node_id=stable_id(
            "workspace-revision",
            workflow.workflow_id,
            job.job_id,
            step.step_id,
            kind.value,
            selector,
        ),
        selector=selector,
        evidence_ids=tuple(step.evidence_ids),
        selected_by_step_id=step.step_id,
    )


def _classify_selector(selector: str) -> WorkspaceRevisionKind:
    value = selector.strip().strip("'\"")
    if _FULL_SHA.fullmatch(value):
        return WorkspaceRevisionKind.EXACT_COMMIT
    if _PR_EVENT_SHA.search(value):
        return WorkspaceRevisionKind.PR_EVENT_HEAD_SHA
    if _PR_HEAD_MARKER.search(value):
        return WorkspaceRevisionKind.PR_CURRENT_HEAD
    if not value or "${{" in value or value.startswith("$"):
        return WorkspaceRevisionKind.UNKNOWN
    return WorkspaceRevisionKind.MUTABLE_REF


def _checkout_action_transition(step: Step, workflow: Workflow) -> _Transition | None:
    if not step.uses or not step.uses.startswith("actions/checkout@"):
        return None
    ref = str(step.with_.get("ref", "")).strip()
    if ref:
        return _Transition(_classify_selector(ref), ref, 0)
    if "pull_request_target" in workflow.triggers:
        return _Transition(WorkspaceRevisionKind.TRUSTED_BASE, "default-base", 0)
    if "pull_request" in workflow.triggers:
        return _Transition(WorkspaceRevisionKind.PR_EVENT_HEAD_SHA, "pull-request-default", 0)
    return _Transition(WorkspaceRevisionKind.TRUSTED_BASE, "default-ref", 0)


def _last_token(command: str) -> str:
    tokens = re.findall(r"[^\s]+", command.strip())
    return tokens[-1] if tokens else ""


def _run_transitions(run: str) -> list[_Transition]:
    events: list[tuple[int, str, re.Match[str]]] = []
    for kind, pattern in (
        ("gh", _GH_PR_CHECKOUT),
        ("fetch", _GIT_FETCH),
        ("checkout", _GIT_CHECKOUT),
    ):
        events.extend((match.start(), kind, match) for match in pattern.finditer(run))
    events.sort(key=lambda item: item[0])

    transitions: list[_Transition] = []
    fetched_kind: WorkspaceRevisionKind | None = None
    fetched_selector = ""
    for _, kind, match in events:
        command = match.group(0)
        if kind == "gh":
            transitions.append(
                _Transition(WorkspaceRevisionKind.PR_CURRENT_HEAD, "gh-pr-checkout", match.end())
            )
            continue
        if kind == "fetch":
            if _PR_EVENT_SHA.search(command):
                fetched_kind = WorkspaceRevisionKind.PR_EVENT_HEAD_SHA
            elif _PR_HEAD_MARKER.search(command):
                fetched_kind = WorkspaceRevisionKind.PR_CURRENT_HEAD
            else:
                fetched_kind = _classify_selector(_last_token(command))
            fetched_selector = command.strip()
            continue

        if _FETCH_HEAD.search(command) and fetched_kind is not None:
            transitions.append(
                _Transition(fetched_kind, fetched_selector or "FETCH_HEAD", match.end())
            )
            continue
        selector = _last_token(command)
        if selector.casefold() in {"--detach", "-d"}:
            transitions.append(
                _Transition(WorkspaceRevisionKind.UNKNOWN, command.strip(), match.end())
            )
            continue
        transitions.append(_Transition(_classify_selector(selector), selector, match.end()))
    return transitions


def _selection_relation(
    state: WorkspaceRevisionState,
    *,
    profile: AttackerProfile,
) -> Relation:
    if state.kind is WorkspaceRevisionKind.PR_CURRENT_HEAD and profile is AttackerProfile.FORK_PR:
        return _relation(
            "attacker:fork-pr",
            "attacker_selects_current_pull_request_head",
            state.node_id,
            state.evidence_ids,
            category="source",
        )
    if state.kind is WorkspaceRevisionKind.PR_EVENT_HEAD_SHA and profile is AttackerProfile.FORK_PR:
        return _relation(
            "attacker:fork-pr",
            "attacker_selects_event_pull_request_head_sha",
            state.node_id,
            state.evidence_ids,
            category="source",
        )
    relation_type = {
        WorkspaceRevisionKind.TRUSTED_BASE: "selects_trusted_base_revision",
        WorkspaceRevisionKind.EXACT_COMMIT: "selects_exact_commit_revision",
        WorkspaceRevisionKind.MUTABLE_REF: "selects_mutable_workspace_ref",
        WorkspaceRevisionKind.UNKNOWN: "selects_unknown_workspace_revision",
    }.get(state.kind, "selects_unknown_workspace_revision")
    return _relation(
        state.selected_by_step_id,
        relation_type,
        state.node_id,
        state.evidence_ids,
        category="transform",
        verdict=(
            TriState.UNKNOWN
            if state.kind is WorkspaceRevisionKind.UNKNOWN
            else TriState.TRUE
        ),
        missing_evidence=(
            ["resolved workspace revision"]
            if state.kind is WorkspaceRevisionKind.UNKNOWN
            else []
        ),
    )


def _step_executes_repository_content(step: Step, *, suffix: str | None = None) -> bool:
    if step.uses and step.uses.startswith("./"):
        return True
    run = suffix if suffix is not None else step.run
    return bool(run and _REPOSITORY_EXECUTION.search(run))


def derive_workspace_revision_relations(
    workflow: Workflow,
    job: Job,
    profile: AttackerProfile,
) -> list[Relation]:
    """Track a bounded ordered workspace revision state through one GitHub Actions job.

    The parser intentionally recognizes only checkout/fetch forms whose security meaning is
    deterministic. Unsupported dynamic shell behavior remains UNKNOWN instead of being guessed.
    """

    relations: list[Relation] = []
    current: WorkspaceRevisionState | None = None
    for step in job.steps:
        action_transition = _checkout_action_transition(step, workflow)
        transitions = [action_transition] if action_transition is not None else []
        if step.run:
            transitions.extend(_run_transitions(step.run))

        last_end: int | None = None
        for transition in transitions:
            current = _revision_state(
                workflow,
                job,
                step,
                transition.kind,
                transition.selector,
            )
            selection = _selection_relation(current, profile=profile)
            relations.append(selection)
            if selection.source_id != step.step_id:
                relations.append(
                    _relation(
                        step.step_id,
                        "step_selects_workspace_revision",
                        current.node_id,
                        current.evidence_ids,
                        category="transform",
                        verdict=selection.verdict,
                        missing_evidence=selection.missing_evidence,
                    )
                )
            last_end = max(last_end or 0, transition.end)

        if current is None:
            continue
        same_step_suffix = None
        if step.run is not None and last_end is not None:
            same_step_suffix = step.run[last_end:]
        execution = _step_executes_repository_content(step, suffix=same_step_suffix)
        if not execution and (not transitions or step.step_id != current.selected_by_step_id):
            execution = _step_executes_repository_content(step)
        if execution:
            evidence_ids = sorted(set(current.evidence_ids + tuple(step.evidence_ids)))
            relations.append(
                _relation(
                    current.node_id,
                    "workspace_revision_reaches_execution",
                    step.step_id,
                    evidence_ids,
                    category="execution",
                    verdict=(
                        TriState.UNKNOWN
                        if current.kind is WorkspaceRevisionKind.UNKNOWN
                        else TriState.TRUE
                    ),
                    missing_evidence=(
                        ["resolved workspace revision"]
                        if current.kind is WorkspaceRevisionKind.UNKNOWN
                        else []
                    ),
                )
            )
    return relations
