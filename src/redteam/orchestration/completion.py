"""Step completion code checks against fixed criteria and real evidence.

LLM semantic review stays with the root engine. This module only checks
code-verifiable facts: real evidence existence, same run/path/version/
step/target/principal/action linkage, fixed completion criteria,
predecessor completion, and outcome separation (refuted is an
investigation result, not PoC success; inconclusive/blocked never count
as successful completion).
"""

from __future__ import annotations

from typing import Any

from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.paths import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    PathPlan,
    PathStep,
    criteria_key,
    is_active_status,
    is_known_criterion,
)

_ALLOWED_KINDS = ALLOWED_CRITERION_KINDS
_VALID_OUTCOMES = frozenset({"success", "refuted", "inconclusive", "blocked"})

# Phrases that mean "no candidates left" and must never alone justify completion.
_NO_CANDIDATE_PHRASES = (
    "no candidate",
    "no further",
    "exhausted",
    "no actionable",
    "후보가 없",
    "후보 소진",
)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _norm_status(value: Any) -> str:
    return _s(value).strip().lower()


def _is_completed_status(value: Any) -> bool:
    return _norm_status(value) == "completed"


def _criteria_list(step: PathStep) -> list[CompletionCriterion]:
    return list(step.completion_criteria or [])


def _criterion_kind(criterion: CompletionCriterion | str) -> str | None:
    if isinstance(criterion, str):
        return None
    text = _s(criterion.kind).strip().lower()
    return text or None


def _criterion_min_evidence(criterion: CompletionCriterion) -> int:
    try:
        value = int(criterion.min_evidence)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _criterion_hypothesis_id(criterion: CompletionCriterion) -> str | None:
    hid = criterion.hypothesis_id
    if hid is None or not hid.strip():
        return None
    return hid


def _criterion_principal_refs(criterion: CompletionCriterion) -> list[str]:
    return [ref for ref in (criterion.principal_refs or []) if _s(ref).strip()]


def _criterion_label(criterion: CompletionCriterion | str, index: int) -> str:
    if isinstance(criterion, str):
        return f"criterion[{index}]:unknown:{criterion[:80]}"
    kind = _criterion_kind(criterion)
    desc = criterion.description.strip()
    hid = _criterion_hypothesis_id(criterion)
    label = f"criterion[{index}]:{kind or 'unknown'}"
    if hid:
        label += f":{hid}"
    if desc:
        label += f":{desc[:60]}"
    return label


def _evidence_principal_refs(record: Any) -> set[str]:
    meta = _field(record, "metadata", {}) or {}
    if not isinstance(meta, dict):
        return set()
    out: set[str] = set()
    single = meta.get("principal_ref")
    if isinstance(single, str) and single.strip():
        out.add(single.strip())
    multi = meta.get("principal_refs")
    if isinstance(multi, (list, tuple, set)):
        for item in multi:
            text = _s(item).strip()
            if text:
                out.add(text)
    return out


def _is_anonymous_evidence(meta: dict[str, Any]) -> bool:
    if meta.get("anonymous") is True:
        return True
    if _s(meta.get("principal_ref", "")).strip().lower() == "anonymous":
        return True
    refs = meta.get("principal_refs")
    if isinstance(refs, (list, tuple, set)):
        return any(_s(item).strip().lower() == "anonymous" for item in refs)
    return False


def _evidence_operation_names(meta: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    bare = _s(meta.get("operation", "")).strip().lower()
    capability = _s(meta.get("capability", "")).strip().lower()
    if bare:
        names.add(bare)
    if bare and capability:
        names.add(capability + "." + bare)
    for key in ("canonical_operation", "operation_key"):
        value = _s(meta.get(key, "")).strip().lower()
        if value:
            names.add(value)
    return names


def _evidence_hypothesis_id(record: Any) -> str:
    top = _field(record, "hypothesis_id", None)
    if top is not None and _s(top).strip():
        return _s(top).strip()
    meta = _field(record, "metadata", {}) or {}
    if isinstance(meta, dict):
        hid = meta.get("hypothesis_id")
        if hid is not None and _s(hid).strip():
            return _s(hid).strip()
    return ""


def _proposal_claims(proposal: dict) -> list[tuple[str, bool | None]]:
    raw = proposal.get("criterion_results", [])
    claims: list[tuple[str, bool | None]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, bool):
                claims.append((_s(key), value))
            elif isinstance(value, dict):
                met = value.get("met", value.get("satisfied", value.get("passed")))
                claims.append((_s(key), bool(met) if isinstance(met, bool) else None))
            else:
                claims.append((_s(key), None))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                key = _s(item.get("criterion", item.get("kind", item.get("description", ""))))
                met = item.get("met", item.get("satisfied", item.get("passed")))
                claims.append((key, bool(met) if isinstance(met, bool) else None))
            elif isinstance(item, str):
                claims.append((item, None))
    return claims


def _claim_matches_criterion(key: str, criterion: CompletionCriterion | str, index: int) -> bool:
    if not key or isinstance(criterion, str):
        return False
    lowered = key.strip().lower()
    kind = _criterion_kind(criterion) or ""
    hid = (_criterion_hypothesis_id(criterion) or "").lower()
    desc = criterion.description.strip().lower()
    if lowered in {f"criterion[{index}]", str(index)}:
        return True
    if kind and kind in lowered:
        return True
    if hid and hid in lowered:
        return True
    if desc and (desc in lowered or lowered in desc):
        return True
    return False


async def validate_completion(
    *,
    path: PathPlan,
    step: PathStep,
    proposal: dict,
    evidence_store: Any,
    graph_service: Any,
    run_id: str,
) -> list[str]:
    """Code checks for a step completion proposal. Empty list means pass."""
    issues: list[str] = []
    if not isinstance(proposal, dict):
        return ["proposal must be a dict with evidence_ids/outcome/reason/criterion_results"]
    try:
        criteria_key(step.completion_criteria)
    except Exception as exc:  # noqa: BLE001 - malformed fixed criteria block success
        issues.append(f"step '{step.step_id or '?'}' has malformed completion criteria: {exc}")
    run = _s(run_id).strip()
    if not run:
        issues.append("run_id is required")
    path_id = _s(path.path_id).strip()
    path_version = path.version
    step_id = _s(step.step_id).strip()
    if not path_id:
        issues.append("path has no path_id")
    if not step_id:
        issues.append("step has no step_id")
    if not is_active_status(path.status):
        issues.append(f"path '{path_id or '?'}' is not active (status={path.status or 'missing'})")
    path_step_ids = {_s(item.step_id).strip() for item in path.steps}
    if step_id and path_step_ids and step_id not in path_step_ids:
        issues.append(f"step '{step_id}' does not belong to path '{path_id or '?'}'")
    # Predecessors: only required ones block completion.
    dep_ids = [_s(item).strip() for item in (step.depends_on or []) if _s(item).strip()]
    by_id = {_s(item.step_id).strip(): item for item in path.steps}
    for dep_id in dep_ids:
        dep = by_id.get(dep_id)
        if dep is None:
            issues.append(f"predecessor step '{dep_id}' not found in path '{path_id or '?'}'")
            continue
        if bool(dep.required) and not _is_completed_status(dep.status):
            issues.append(
                f"required predecessor step '{dep_id}' is not completed "
                f"(status={dep.status or 'missing'})"
            )
    outcome = _s(proposal.get("outcome", "")).strip().lower()
    if outcome not in _VALID_OUTCOMES:
        issues.append(
            f"outcome must be one of success/refuted/inconclusive/blocked (got={proposal.get('outcome')!r})"
        )
        outcome = ""
    reason = proposal.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        issues.append("proposal reason is required")
    raw_evidence_ids = proposal.get("evidence_ids", [])
    if isinstance(raw_evidence_ids, str):
        raw_evidence_ids = [raw_evidence_ids]
    try:
        evidence_ids = [_s(item).strip() for item in list(raw_evidence_ids or []) if _s(item).strip()]
    except TypeError:
        evidence_ids = []
        issues.append("proposal evidence_ids must be a list of evidence id strings")
    if not evidence_ids:
        issues.append("proposal has no evidence; completion needs real evidence")
    if outcome in {"inconclusive", "blocked"}:
        issues.append(f"outcome '{outcome}' is not a successful completion")
    # Per-evidence linkage checks. Missing path linkage can never succeed
    # (worker D records path_id/path_version/path_step_id/principal_ref).
    valid_records: list[Any] = []
    step_target_set = {_s(item).strip() for item in (step.target_node_ids or []) if _s(item).strip()}
    step_principal_set = {_s(item).strip() for item in (step.principal_refs or []) if _s(item).strip()}
    step_op_set = {
        _s(item).strip().lower() for item in (step.allowed_operations or []) if _s(item).strip()
    }
    for eid in evidence_ids:
        try:
            record = await evidence_store.get(eid)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - store failure is a validation issue
            issues.append(f"evidence '{eid}' lookup failed: {type(exc).__name__}")
            continue
        if record is None:
            issues.append(f"evidence '{eid}' does not exist")
            continue
        if _s(_field(record, "run_id", "")) != run:
            issues.append(f"evidence '{eid}' belongs to a different run")
            continue
        meta = _field(record, "metadata", {}) or {}
        if not isinstance(meta, dict):
            issues.append(f"evidence '{eid}' has unreadable metadata")
            continue
        m_path_id = _s(meta.get("path_id", "")).strip()
        m_version = meta.get("path_version", None)
        m_step_id = _s(meta.get("path_step_id", "")).strip()
        if not m_path_id or m_version is None or _s(m_version).strip() == "" or not m_step_id:
            issues.append(
                f"evidence '{eid}' has no path linkage "
                "(path_id/path_version/path_step_id required)"
            )
            continue
        ev_principals = _evidence_principal_refs(record)
        anonymous = _is_anonymous_evidence(meta)
        if step_principal_set:
            if not ev_principals and not anonymous:
                issues.append(f"evidence '{eid}' has no principal_ref")
                continue
            if (
                ev_principals
                and ev_principals.isdisjoint(step_principal_set)
                and not anonymous
            ):
                issues.append(f"evidence '{eid}' uses a principal outside the step scope")
                continue
        # A step with no principal scope accepts anonymous initial
        # reconnaissance evidence as-is.
        if m_path_id != path_id:
            issues.append(f"evidence '{eid}' belongs to a different path (path_id mismatch)")
            continue
        if _s(m_version).strip() != _s(path_version).strip():
            issues.append(f"evidence '{eid}' belongs to a different path version")
            continue
        if m_step_id != step_id:
            issues.append(f"evidence '{eid}' belongs to a different step")
            continue
        if step_op_set:
            ev_op_names = _evidence_operation_names(meta)
            if ev_op_names and ev_op_names.isdisjoint(step_op_set):
                issues.append(f"evidence '{eid}' uses an operation outside the step scope")
                continue
        target_ref = _s(meta.get("target_node_id", "")).strip()
        if step_target_set:
            if not target_ref:
                issues.append(f"evidence '{eid}' has no target linkage")
                continue
            if target_ref not in step_target_set:
                issues.append(f"evidence '{eid}' targets a different object")
                continue
        action_id = _field(record, "action_id", None)
        invocation_id = _field(record, "invocation_id", None)
        action_ref = _s(action_id).strip()
        invocation_ref = _s(invocation_id).strip()
        if not action_ref or not invocation_ref:
            issues.append(f"evidence '{eid}' needs both action_id and invocation_id linkage")
            continue
        mismatched = next(
            (
                key
                for key, value in (("action_id", action_ref), ("invocation_id", invocation_ref))
                if meta.get(key) is not None
                and _s(meta.get(key)).strip()
                and _s(meta.get(key)).strip() != value
            ),
            None,
        )
        if mismatched is not None:
            issues.append(f"evidence '{eid}' {mismatched} disagrees with its original result")
            continue
        if outcome == "success":
            failed_original = meta.get("success") is False or (
                meta.get("success") is None
                and isinstance(meta.get("error"), dict)
                and bool(meta.get("error"))
            )
            if failed_original:
                issues.append(
                    f"evidence '{eid}' records a failed original result "
                    "and cannot support success"
                )
                continue
        valid_records.append(record)
    criteria = _criteria_list(step)
    if not criteria:
        issues.append(f"step '{step_id or '?'}' has no fixed completion criteria")
    evaluated: list[tuple[CompletionCriterion | str, bool, str]] = []
    for index, criterion in enumerate(criteria):
        label = _criterion_label(criterion, index)
        kind = _criterion_kind(criterion)
        if not is_known_criterion(criterion):
            issues.append(f"unknown completion condition cannot succeed: {label}")
            evaluated.append((criterion, False, "unknown"))
            continue
        min_evidence = _criterion_min_evidence(criterion)
        hid = _criterion_hypothesis_id(criterion)
        required_principals = set(_criterion_principal_refs(criterion))
        if kind in {"hypothesis_confirmed", "hypothesis_refuted"} and not hid:
            issues.append(f"{label} needs a hypothesis_id")
            evaluated.append((criterion, False, "missing hypothesis"))
            continue
        pool = list(valid_records)
        if required_principals:
            pool = [rec for rec in pool if _evidence_principal_refs(rec) & required_principals]
            have = set()
            for rec in pool:
                have |= _evidence_principal_refs(rec)
            missing_principals = required_principals - have
            if missing_principals:
                issues.append(f"{label} missing principal scope: {sorted(missing_principals)}")
                evaluated.append((criterion, False, "principal scope"))
                continue
        if kind in {"hypothesis_confirmed", "hypothesis_refuted"} and hid:
            pool = [rec for rec in pool if _evidence_hypothesis_id(rec) == hid]
            status = ""
            try:
                hypothesis = await graph_service.get_hypothesis(hid)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - graph failure blocks success
                issues.append(f"{label} hypothesis lookup failed: {type(exc).__name__}")
                evaluated.append((criterion, False, "hypothesis lookup"))
                continue
            if hypothesis is None:
                issues.append(f"{label} references an unknown hypothesis '{hid}'")
                evaluated.append((criterion, False, "unknown hypothesis"))
                continue
            status = _norm_status(_field(hypothesis, "status", ""))
            if kind == "hypothesis_confirmed" and status != "confirmed":
                issues.append(f"{label} hypothesis '{hid}' is not confirmed (status={status or '?'})")
                evaluated.append((criterion, False, "hypothesis status"))
                continue
            if kind == "hypothesis_refuted" and status not in {"rejected", "refuted"}:
                issues.append(f"{label} hypothesis '{hid}' is not refuted (status={status or '?'})")
                evaluated.append((criterion, False, "hypothesis status"))
                continue
        if len(pool) < min_evidence:
            issues.append(f"{label} needs {min_evidence} evidence(s), found {len(pool)}")
            evaluated.append((criterion, False, "evidence count"))
            continue
        if kind == "poc":
            if outcome == "refuted":
                issues.append(f"{label}: refuted is an investigation result, not PoC success")
                evaluated.append((criterion, False, "refuted is not poc"))
                continue
            backed = False
            for rec in pool:
                rec_meta = _field(rec, "metadata", {}) or {}
                ok = isinstance(rec_meta, dict) and rec_meta.get("success") is True
                if _field(rec, "source_type", None) is not None and _field(
                    rec, "source_type", None
                ) is not EvidenceSourceType.TOOL:
                    ok = False
                if ok:
                    backed = True
                    break
            if not backed:
                issues.append(f"{label} needs successful tool evidence")
                evaluated.append((criterion, False, "poc backing"))
                continue
        evaluated.append((criterion, True, "ok"))
    if outcome == "success":
        for criterion, passed, _why in evaluated:
            if not passed:
                break
        else:
            if evaluated and all(passed for _, passed, _ in evaluated):
                pass
            elif not evaluated:
                pass
    if outcome == "refuted":
        refuted_kinds = [c for c in criteria if _criterion_kind(c) == "hypothesis_refuted"]
        if not refuted_kinds:
            issues.append("refuted outcome needs a hypothesis_refuted completion condition")
        else:
            refuted_ok = any(
                passed for criterion, passed, _ in evaluated
                if _criterion_kind(criterion) == "hypothesis_refuted"
            )
            if not refuted_ok:
                issues.append("refuted outcome has no satisfied hypothesis_refuted condition")
        for criterion, passed, _ in evaluated:
            kind = _criterion_kind(criterion)
            if kind == "poc" and passed:
                issues.append("refuted outcome cannot claim PoC success")
                break
            if kind == "hypothesis_confirmed" and passed:
                issues.append("refuted outcome contradicts a confirmed hypothesis condition")
                break
    if outcome == "success" and not valid_records:
        lowered_reason = _s(reason).lower()
        if any(phrase in lowered_reason for phrase in _NO_CANDIDATE_PHRASES):
            issues.append("lack of candidates alone cannot justify completion")
    # Proposal claim vs reality: only flag explicit false-met claims.
    claims = _proposal_claims(proposal)
    for key, claimed in claims:
        if claimed is not True:
            continue
        for index, criterion in enumerate(criteria):
            if _claim_matches_criterion(key, criterion, index):
                actual = next(
                    (passed for crit, passed, _ in evaluated if crit is criterion), None
                )
                if actual is False:
                    issues.append(
                        f"proposal claims '{key}' is met but "
                        f"{_criterion_label(criterion, index)} is not satisfied"
                    )
                break
    seen: set[str] = set()
    deduped: list[str] = []
    for item in issues:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped

__all__ = ["validate_completion"]
