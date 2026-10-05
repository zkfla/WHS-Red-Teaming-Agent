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
from redteam.orchestration.analysis import claim_target_is_grounded, is_final_admission_receipt
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.paths import (
    ALLOWED_CRITERION_KINDS,
    CompletionCriterion,
    PathPlan,
    PathStep,
    criteria_key,
    is_active_status,
    is_known_criterion,
)
from redteam.orchestration.session_proof import collect_actual_sessions

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
    for key in ("canonical_operation", "operation_key"):
        value = _s(meta.get(key, "")).strip().lower()
        if value:
            names.add(value)
    return names


def _canonical_evidence_value(meta: dict[str, Any]) -> str:
    for key in ("canonical_operation", "operation_key"):
        value = _s(meta.get(key, "")).strip()
        if value:
            return value
    return ""


def _stored_raw_result(meta: dict[str, Any]) -> dict[str, Any] | None:
    # Same key contract as the reader; the dict-only requirement stays.
    # Both writers only ever store mappings under these keys, so the first
    # preserved payload is the mapping completion reasons over.
    raw = raw_from_metadata(meta)
    return raw if isinstance(raw, dict) else None


def _claimed_proposal_principal(stored_proposal: dict[str, Any]) -> str | None:
    args = stored_proposal.get("arguments")
    if not isinstance(args, dict):
        return None
    for key in ("principal_ref", "session_ref", "principal"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _raw_account_names(raw: dict[str, Any], meta: dict[str, Any]) -> set[str]:
    # Session names come from the shared actual-use collector only; principal
    # echoes stay readable here as before.
    found: set[str] = collect_actual_sessions(meta, raw)
    comparison = meta.get("session_comparison")
    if isinstance(comparison, dict):
        for key in ("first_principal_ref", "second_principal_ref"):
            value = comparison.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    if isinstance(output, dict):
        for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
        for nested_key in ("first", "second", "authenticated", "anonymous"):
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                value = nested.get("principal_ref")
                if isinstance(value, str) and value.strip():
                    found.add(value.strip())
    return found


def _clean_completion_identity(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _has_exact_account_proof(
    meta: dict[str, Any],
    raw: dict[str, Any],
    claimed_accounts: set[str],
    stored_args: dict[str, Any] | None,
) -> bool:
    """Exact structural identity equality only; never bare or substring."""
    cleaned_claimed = {
        str(item).strip() for item in (claimed_accounts or set()) if str(item).strip()
    }
    if not cleaned_claimed:
        return False
    args = stored_args if isinstance(stored_args, dict) else {}
    expected_principals = set(cleaned_claimed)
    for key in ("principal_ref", "principal", "first_principal_ref", "second_principal_ref"):
        cleaned = _clean_completion_identity(args.get(key))
        if cleaned is not None:
            expected_principals.add(cleaned)
    expected_sessions: set[str] = set()
    for key in ("session_ref", "first_session_ref", "second_session_ref"):
        cleaned = _clean_completion_identity(args.get(key))
        if cleaned is not None:
            expected_sessions.add(cleaned)
    raw_principals: set[str] = set()
    # Session names come from the shared actual-use collector only; the
    # per-unit receipt-vs-echo rule lives in session_proof, not here.
    raw_sessions: set[str] = collect_actual_sessions(meta, raw)
    # Actual-only values for the mismatch gate below.
    actual_receipts: set[str] = set()
    http_obs = meta.get("http_observation")
    if isinstance(http_obs, dict):
        cleaned = _clean_completion_identity(http_obs.get("actual_session_ref"))
        if cleaned is not None:
            actual_receipts.add(cleaned)
    comparison = meta.get("session_comparison")
    comparison_dict: dict[str, Any] | None = comparison if isinstance(comparison, dict) else None
    if comparison_dict is not None:
        for key in ("first_actual_session_ref", "second_actual_session_ref"):
            cleaned = _clean_completion_identity(comparison_dict.get(key))
            if cleaned is not None:
                actual_receipts.add(cleaned)
        for key in ("first_principal_ref", "second_principal_ref"):
            cleaned = _clean_completion_identity(comparison_dict.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    if isinstance(output, dict):
        for key in (
            "actual_session_ref",
            "first_actual_session_ref",
            "second_actual_session_ref",
        ):
            cleaned = _clean_completion_identity(output.get(key))
            if cleaned is not None:
                actual_receipts.add(cleaned)
        for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
            cleaned = _clean_completion_identity(output.get(key))
            if cleaned is not None:
                raw_principals.add(cleaned)
        for nested_key in ("first", "second", "authenticated", "anonymous"):
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                cleaned = _clean_completion_identity(nested.get("actual_session_ref"))
                if cleaned is not None:
                    actual_receipts.add(cleaned)
                cleaned = _clean_completion_identity(nested.get("principal_ref"))
                if cleaned is not None:
                    raw_principals.add(cleaned)
    # Configured session inventory (session_list rows) is never actual-use
    # proof: it is not merged into the raw receipt sets above. Session to
    # principal binding stays on the separate alias-grounded lookup, and an
    # absent receipt proves nothing, including anonymous use.
    if actual_receipts and expected_sessions and actual_receipts.isdisjoint(expected_sessions):
        # The response says it used sessions nobody asked for: requested echo
        # must not rescue it, so the whole account proof is rejected.
        return False
    if comparison_dict is not None:
        first_principal = _clean_completion_identity(comparison_dict.get("first_principal_ref"))
        second_principal = _clean_completion_identity(comparison_dict.get("second_principal_ref"))
        if first_principal is not None or second_principal is not None:
            if not expected_principals:
                return False
            for observed in (first_principal, second_principal):
                if observed is not None and observed not in expected_principals:
                    return False
            if first_principal is None or second_principal is None:
                return False
        first_session = _clean_completion_identity(comparison_dict.get("first_session_ref"))
        second_session = _clean_completion_identity(comparison_dict.get("second_session_ref"))
        if (first_session is not None or second_session is not None) and expected_sessions:
            for observed in (first_session, second_session):
                if observed is not None and observed not in expected_sessions:
                    return False
    if expected_sessions and (raw_sessions & expected_sessions):
        return True
    if expected_principals and (raw_principals & expected_principals):
        return True
    return False


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


def _receipt_review_key(meta: dict[str, Any]) -> tuple:
    hypothesis = str(meta.get("hypothesis_id") or "")
    claim = str(meta.get("claim_identity") or "")
    if meta.get("posthoc") is True and claim:
        origins = tuple(sorted({
            (str(item.get("evidence_id", "")), str(item.get("action_id", "")),
             str(item.get("invocation_id", "")))
            for item in meta.get("source_origins", []) or []
            if isinstance(item, dict)
        }))
        return (
            str(meta.get("engagement_id", "")), hypothesis, claim,
            tuple(sorted({str(item) for item in meta.get("source_evidence_ids", []) or []})),
            origins,
        )
    return (str(meta.get("action_id", "")), str(meta.get("invocation_id", "")), hypothesis)


async def _load_final_receipts(evidence_store: Any, run_id: str) -> list[dict[str, Any]]:
    """Latest per claim and normalized sources; legacy receipts retain origin grouping."""
    records = await evidence_store.list_for_run(run_id)
    latest: dict[tuple, tuple[Any, dict[str, Any]]] = {}
    for rec in list(records or []):
        try:
            if not is_final_admission_receipt(rec):
                continue
            meta = getattr(rec, "metadata", None) or {}
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        if str(getattr(rec, "run_id", "")) != str(run_id):
            continue
        action = str(meta.get("action_id", "") or "").strip()
        invocation = str(meta.get("invocation_id", "") or "").strip()
        if not action or not invocation:
            continue
        created = getattr(rec, "created_at", None)
        meta = {**meta, "receipt_id": str(rec.evidence_id),
                "engagement_id": str(rec.engagement_id)}
        # First origin is audit data; source ordering never creates a new claim.
        key = _receipt_review_key(meta)
        prev = latest.get(key)
        if prev is None:
            latest[key] = (created, dict(meta))
            continue
        try:
            if created is not None and (prev[0] is None or created >= prev[0]):
                latest[key] = (created, dict(meta))
        except Exception:
            continue
    return [meta for _, meta in latest.values()]


def _receipt_links_record(meta: dict[str, Any], record: Any) -> bool:
    """Receipt links to a preserved original without changing its metadata."""
    return (
        str(_field(record, "evidence_id", "")) in {
            str(item) for item in meta.get("source_evidence_ids", []) or []
        }
        and (
            (str(_field(record, "action_id", "")), str(_field(record, "invocation_id", "")))
            == (str(meta.get("action_id", "")), str(meta.get("invocation_id", "")))
            or any(
                str(item.get("evidence_id", "")) == str(_field(record, "evidence_id", ""))
                and str(item.get("action_id", "")) == str(_field(record, "action_id", ""))
                and str(item.get("invocation_id", "")) == str(_field(record, "invocation_id", ""))
                for item in meta.get("source_origins", []) or [] if isinstance(item, dict)
            )
        )
        and str(_field(record, "engagement_id", "")) == str(meta.get("engagement_id", ""))
    )


async def _verify_prior_lineage(
    path_store: Any,
    run_id: str,
    path_id: str,
    current_version: Any,
    source_version: Any,
    step_id: str,
    step: PathStep,
) -> tuple[bool, str]:
    from redteam.orchestration.paths.models import step_definition_equal

    plans = await path_store.list_paths(run_id)
    versions = {
        str(item.version): item
        for item in plans
        if item.run_id == run_id and item.path_id == path_id
    }
    current = str(current_version)
    source = str(source_version)
    seen = set()
    while True:
        plan = versions.get(current)
        if plan is None or current in seen:
            return False, "version lineage does not reach the source version"
        seen.add(current)
        saved_step = next((item for item in plan.steps if item.step_id == step_id), None)
        if saved_step is None or not step_definition_equal(saved_step, step):
            return False, "step redefined in the version lineage"
        if current == source:
            return True, ""
        if step_id in plan.changed_step_ids:
            return False, "step changed or reset in the version lineage"
        if plan.parent_version is None:
            return False, "version lineage does not reach the source version"
        current = str(plan.parent_version)


def check_record_linkage(
    record: Any,
    meta: dict[str, Any],
    raw: dict[str, Any],
    evidence_id: str,
    *,
    require_proposal: bool = True,
) -> list[tuple[str, str, str]]:
    """Shared actual-provenance linkage: action, invocation, tool, proposal.

    Returns fully formatted (message, code, summary) triples, stopping at
    the first failure so callers keep the existing single-issue behavior.
    """
    action_ref = _s(_field(record, "action_id", None)).strip()
    invocation_ref = _s(_field(record, "invocation_id", None)).strip()
    if not action_ref or not invocation_ref:
        return [
            (
                f"evidence '{evidence_id}' needs both action_id and invocation_id linkage",
                "completion_evidence_reference_needs_both_action_id_25",
                "evidence 'reference' needs both action_id and invocation_id linkage",
            )
        ]
    if _field(record, "source_type", None) is not EvidenceSourceType.TOOL:
        return [
            (
                f"evidence '{evidence_id}' is not tool evidence and cannot prove execution",
                "completion_evidence_reference_is_not_tool_evidence_79",
                "evidence 'reference' is not tool evidence and cannot prove execution",
            )
        ]
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
        return [
            (
                f"evidence '{evidence_id}' {mismatched} disagrees with its original result",
                "completion_evidence_reference_reference_disagrees_with_its_26",
                "evidence 'reference' reference disagrees with its original result",
            )
        ]
    if str(raw.get("action_id", "")).strip() != action_ref:
        return [
            (
                f"evidence '{evidence_id}' preserved raw result answers another action",
                "completion_evidence_reference_preserved_raw_result_answers_27",
                "evidence 'reference' preserved raw result answers another action",
            )
        ]
    if str(raw.get("invocation_id", "")).strip() != invocation_ref:
        return [
            (
                f"evidence '{evidence_id}' preserved raw result answers another invocation",
                "completion_evidence_reference_preserved_raw_result_answers_28",
                "evidence 'reference' preserved raw result answers another invocation",
            )
        ]
    if not str(raw.get("tool_id", "") or "").strip():
        return [
            (
                f"evidence '{evidence_id}' preserved raw result names no tool",
                "completion_evidence_reference_preserved_raw_result_names_no_tool_76",
                "evidence 'reference' preserved raw result names no tool",
            )
        ]
    meta_tool = _s(meta.get("tool_id", "")).strip()
    if meta_tool and meta_tool != str(raw.get("tool_id", "") or "").strip():
        return [
            (
                f"evidence '{evidence_id}' tool differs from its preserved raw result",
                "completion_evidence_reference_tool_differs_from_preserved_raw_80",
                "evidence 'reference' tool differs from its preserved raw result",
            )
        ]
    stored_proposal = meta.get("proposal")
    if not isinstance(stored_proposal, dict):
        if require_proposal:
            return [
                (
                    f"evidence '{evidence_id}' has no stored proposal to prove linkage; "
                    "identifier alone is not proof",
                    "completion_evidence_reference_has_no_stored_proposal_29",
                    "evidence 'reference' has no stored proposal to prove linkage; "
                    "identifier alone is not proof",
                )
            ]
        return []
    if stored_proposal.get("binding_mode") == "selected_tool" and (
        not stored_proposal.get("tool_id")
        or str(stored_proposal["tool_id"]) != str(raw.get("tool_id"))
    ):
        return [(
            f"evidence '{evidence_id}' selected tool differs from its original result",
            "completion_selected_tool_mismatch",
            "evidence selected tool differs from its original result",
        )]
    stored_action = _s(stored_proposal.get("action_id", "")).strip()
    if not stored_action or stored_action != action_ref:
        return [
            (
                f"evidence '{evidence_id}' stored proposal answers another action",
                "completion_evidence_reference_stored_proposal_answers_another_30",
                "evidence 'reference' stored proposal answers another action",
            )
        ]
    stored_capability = stored_proposal.get("capability")
    stored_operation = stored_proposal.get("operation")
    if isinstance(stored_capability, str) and stored_capability.strip():
        if stored_capability.strip() != _s(meta.get("capability", "")).strip():
            return [
                (
                    f"evidence '{evidence_id}' stored behavior disagrees with its record",
                    "completion_evidence_reference_stored_behavior_disagrees_with_44",
                    "evidence 'reference' stored behavior disagrees with its record",
                )
            ]
    if isinstance(stored_operation, str) and stored_operation.strip():
        if stored_operation.strip() != _s(meta.get("operation", "")).strip():
            return [
                (
                    f"evidence '{evidence_id}' stored behavior disagrees with its record",
                    "completion_evidence_reference_stored_behavior_disagrees_with_45",
                    "evidence 'reference' stored behavior disagrees with its record",
                )
            ]
    for key in ("run_id", "engagement_id"):
        proposed_value = _s(stored_proposal.get(key, "") or "").strip()
        record_value = _s(_field(record, key, "") or "").strip()
        if not proposed_value or not record_value:
            return [
                (
                    f"evidence '{evidence_id}' stored proposal has no {key} binding",
                    "completion_evidence_reference_stored_proposal_has_no_binding_81",
                    "evidence 'reference' stored proposal has no binding",
                )
            ]
        if proposed_value != record_value:
            return [
                (
                    f"evidence '{evidence_id}' stored proposal answers another {key}",
                    "completion_evidence_reference_stored_proposal_answers_another_run_77",
                    "evidence 'reference' stored proposal answers another run",
                )
            ]
    return []


async def verify_stored_origin(
    *,
    path_store: Any,
    run_id: str,
    m_path_id: str,
    m_version: Any,
    m_step_id: str,
    mission_id: Any | None = None,
) -> list[str]:
    """Confirm a cited origin step is really stored in this run and mission.

    Lineage staleness is judged by the caller; this only checks existence
    and binding so other steps or other workers origins can count.
    """
    if not m_path_id or m_version is None or _s(m_version).strip() == "" or not m_step_id:
        return ["has no path linkage (path_id/path_version/path_step_id required)"]
    if path_store is None:
        return ["origin needs stored path lookup (path store unavailable)"]
    try:
        plans = await path_store.list_paths(run_id)  # type: ignore[union-attr]
    except Exception:
        return ["origin lookup failed; treating as unproven"]
    source = None
    for plan in plans or []:
        if _s(getattr(plan, "run_id", "")).strip() != str(run_id):
            continue
        if _s(getattr(plan, "path_id", "")).strip() != m_path_id:
            continue
        if _s(getattr(plan, "version", "")).strip() != _s(m_version).strip():
            continue
        source = plan
        break
    if source is None:
        return ["origin path version is not stored in this run"]
    if mission_id:
        source_mission = _s(getattr(source, "mission_id", "") or "").strip()
        if not source_mission:
            return ["origin has no mission binding; treating as unproven"]
        if source_mission != str(mission_id).strip():
            return ["origin belongs to another mission"]
    origin_step = next(
        (
            item
            for item in (getattr(source, "steps", []) or [])
            if _s(getattr(item, "step_id", "")).strip() == m_step_id
        ),
        None,
    )
    if origin_step is None:
        return ["origin step is not stored in this run"]
    return []


async def verify_origin_step(
    *,
    path_store: Any,
    run_id: str,
    path: PathPlan,
    step: PathStep,
    m_path_id: str,
    m_version: Any,
    m_step_id: str,
    evidence_id: str,
) -> list[tuple[str, str, str]]:
    """Admit cited origins by stored existence, not by current-step equality.

    Other steps or other workers origins pass when the cited path version
    and step are really stored in this run under the same mission. The
    same step in an older version keeps the lineage check; a foreign step
    must not have been redefined after the evidence. Returns fully
    formatted (message, code, summary) triples, empty when admitted.
    """
    path_id = _s(path.path_id).strip()
    path_version = path.version
    step_id = _s(step.step_id).strip()
    if (
        m_path_id == path_id
        and _s(m_version).strip() == _s(path_version).strip()
        and m_step_id == step_id
    ):
        return []
    if path_store is None:
        return [
            (
                f"evidence '{evidence_id}' origin needs stored path lookup "
                "(path store unavailable)",
                "completion_evidence_reference_origin_needs_stored_lookup_75",
                "evidence 'reference' origin needs stored path lookup",
            )
        ]
    try:
        plans = await path_store.list_paths(run_id)  # type: ignore[union-attr]
    except Exception:
        return [
            (
                f"evidence '{evidence_id}' origin lookup failed; treating as unproven",
                "completion_evidence_reference_origin_lookup_failed_74",
                "evidence 'reference' origin lookup failed; treating as unproven",
            )
        ]
    source = None
    newest = None
    for plan in plans or []:
        if _s(getattr(plan, "run_id", "")).strip() != str(run_id):
            continue
        if _s(getattr(plan, "path_id", "")).strip() != m_path_id:
            continue
        if _s(getattr(plan, "version", "")).strip() == _s(m_version).strip():
            source = plan
        if newest is None or getattr(plan, "version", 0) > getattr(newest, "version", 0):
            newest = plan
    if source is None:
        return [
            (
                f"evidence '{evidence_id}' origin path version is not stored in this run",
                "completion_evidence_reference_origin_not_stored_70",
                "evidence 'reference' origin path version is not stored",
            )
        ]
    current_mission = _s(getattr(path, "mission_id", "") or "").strip()
    source_mission = _s(getattr(source, "mission_id", "") or "").strip()
    if current_mission and not source_mission:
        return [
            (
                f"evidence '{evidence_id}' origin has no mission binding; treating as unproven",
                "completion_evidence_reference_origin_has_no_mission_binding_78",
                "evidence 'reference' origin has no mission binding; treating as unproven",
            )
        ]
    if current_mission and source_mission and current_mission != source_mission:
        return [
            (
                f"evidence '{evidence_id}' origin belongs to another mission",
                "completion_evidence_reference_origin_other_mission_71",
                "evidence 'reference' origin belongs to another mission",
            )
        ]
    origin_step = next(
        (
            item
            for item in (getattr(source, "steps", []) or [])
            if _s(getattr(item, "step_id", "")).strip() == m_step_id
        ),
        None,
    )
    if origin_step is None:
        return [
            (
                f"evidence '{evidence_id}' origin step is not stored in this run",
                "completion_evidence_reference_origin_step_missing_72",
                "evidence 'reference' origin step is not stored",
            )
        ]
    if m_path_id == path_id and m_step_id == step_id:
        compare_version = path_version
        compare_step = step
    elif newest is not None and _s(getattr(newest, "version", "")).strip() != _s(m_version).strip():
        compare_version = newest.version
        compare_step = origin_step
    else:
        return []
    try:
        lineage_ok, lineage_reason = await _verify_prior_lineage(
            path_store, str(run_id), m_path_id, compare_version, m_version, m_step_id, compare_step
        )
    except Exception:
        return [
            (
                f"evidence '{evidence_id}' origin lineage lookup failed; treating as unproven",
                "completion_evidence_reference_origin_lineage_lookup_failed_62",
                "evidence 'reference' origin lineage lookup failed; treating as unproven",
            )
        ]
    if not lineage_ok:
        return [
            (
                f"evidence '{evidence_id}' origin step changed after the evidence: "
                f"{lineage_reason}",
                "completion_evidence_reference_origin_redefined_73",
                "evidence 'reference' origin step changed after the evidence",
            )
        ]
    return []


def _pool_record_ids(pool: list[Any]) -> tuple[set[str], set[tuple[str, str]]]:
    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for rec in pool:
        try:
            eid = _s(_field(rec, "evidence_id", "")).strip()
            action = _s(_field(rec, "action_id", "")).strip()
            invocation = _s(_field(rec, "invocation_id", "")).strip()
        except Exception:
            continue
        if eid:
            ids.add(eid)
        if action and invocation:
            pairs.add((action, invocation))
    return (ids, pairs)


def _final_receipt_confirms(
    receipts: list[dict[str, Any]],
    pool_ids: set[str],
    pool_pairs: set[tuple[str, str]],
    hypothesis_id: str | None = None,
) -> bool:
    for meta in receipts:
        reviewed_outcome = meta.get("outcome")
        if hypothesis_id and meta.get("hypothesis_outcome") is not None:
            reviewed_outcome = meta.get("hypothesis_outcome")
        if reviewed_outcome != "success":
            continue
        if str(meta.get("verdict") or "").strip().lower() == "unknown":
            continue
        if str(meta.get("verdict") or "").strip().lower() not in ("violation", "pass"):
            continue
        if meta.get("hypothesis_outcome") is None and meta.get("claim_supported") is not True:
            continue
        if hypothesis_id is not None and hypothesis_id.strip():
            receipt_hid = meta.get("hypothesis_id")
            if receipt_hid is None or str(receipt_hid).strip() != hypothesis_id.strip():
                continue
        sources = {
            str(item) for item in (meta.get("source_evidence_ids", []) or []) if str(item).strip()
        }
        if not (sources & pool_ids):
            continue
        action = str(meta.get("action_id", "") or "").strip()
        invocation = str(meta.get("invocation_id", "") or "").strip()
        if not action or not invocation:
            continue
        receipt_pairs = {(action, invocation)}
        receipt_pairs.update(
            (str(item.get("action_id", "")), str(item.get("invocation_id", "")))
            for item in meta.get("source_origins", []) or []
            if isinstance(item, dict) and str(item.get("evidence_id", "")) in pool_ids
        )
        if not (receipt_pairs & pool_pairs):
            continue
        return True
    return False


def _final_receipt_refutes(
    receipts: list[dict[str, Any]],
    pool_ids: set[str],
    pool_pairs: set[tuple[str, str]],
    hypothesis_id: str | None = None,
) -> bool:
    for meta in receipts:
        reviewed_outcome = meta.get("outcome")
        if hypothesis_id and meta.get("hypothesis_outcome") is not None:
            reviewed_outcome = meta.get("hypothesis_outcome")
        if reviewed_outcome != "refuted":
            continue
        if str(meta.get("verdict") or "").strip().lower() == "unknown":
            continue
        if (
            meta.get("hypothesis_outcome") is None
            and str(meta.get("verdict") or "").strip().lower()
            not in ("pass", "anomaly")
        ):
            continue
        if meta.get("hypothesis_outcome") is None and meta.get("claim_supported") is not False:
            continue
        if hypothesis_id is not None and hypothesis_id.strip():
            receipt_hid = meta.get("hypothesis_id")
            if receipt_hid is None or str(receipt_hid).strip() != hypothesis_id.strip():
                continue
        sources = {
            str(item) for item in (meta.get("source_evidence_ids", []) or []) if str(item).strip()
        }
        if not (sources & pool_ids):
            continue
        action = str(meta.get("action_id", "") or "").strip()
        invocation = str(meta.get("invocation_id", "") or "").strip()
        if not action or not invocation:
            continue
        receipt_pairs = {(action, invocation)}
        receipt_pairs.update(
            (str(item.get("action_id", "")), str(item.get("invocation_id", "")))
            for item in meta.get("source_origins", []) or []
            if isinstance(item, dict) and str(item.get("evidence_id", "")) in pool_ids
        )
        if not (receipt_pairs & pool_pairs):
            continue
        return True
    return False


def _final_success_violation_receipts(
    receipts: list[dict[str, Any]],
    pool_ids: set[str],
    pool_pairs: set[tuple[str, str]],
    hypothesis_id: str | None = None,
) -> list[dict[str, Any]]:
    matching = []
    for meta in receipts:
        if meta.get("outcome") != "success":
            continue
        verdict = meta.get("verdict")
        if str(verdict or "").strip().lower() != "violation":
            continue
        if meta.get("claim_supported") is not True:
            continue
        if (
            meta.get("hypothesis_outcome") is not None
            and meta.get("hypothesis_outcome") != "success"
        ):
            continue
        if hypothesis_id is not None and hypothesis_id.strip():
            receipt_hid = meta.get("hypothesis_id")
            if receipt_hid is None or str(receipt_hid).strip() != hypothesis_id.strip():
                continue
        sources = {
            str(item) for item in (meta.get("source_evidence_ids", []) or []) if str(item).strip()
        }
        if not (sources & pool_ids):
            continue
        action = str(meta.get("action_id", "") or "").strip()
        invocation = str(meta.get("invocation_id", "") or "").strip()
        if not action or not invocation:
            continue
        receipt_pairs = {(action, invocation)}
        receipt_pairs.update(
            (str(item.get("action_id", "")), str(item.get("invocation_id", "")))
            for item in meta.get("source_origins", []) or []
            if isinstance(item, dict) and str(item.get("evidence_id", "")) in pool_ids
        )
        if not (receipt_pairs & pool_pairs):
            continue
        matching.append(meta)
    return matching


def _verified_completion_accounts(
    meta: dict[str, Any],
    raw: dict[str, Any],
    claimed_accounts: set[str],
    stored_args: dict[str, Any] | None,
) -> set[str]:
    """Exact account names proven by raw identity; for criterion coverage."""
    cleaned_claimed = {
        str(item).strip() for item in (claimed_accounts or set()) if str(item).strip()
    }
    if not cleaned_claimed:
        return set()
    args = stored_args if isinstance(stored_args, dict) else {}
    expected_principals = set(cleaned_claimed)
    for key in ("principal_ref", "principal", "first_principal_ref", "second_principal_ref"):
        value = args.get(key) if isinstance(args, dict) else None
        if isinstance(value, str) and value.strip():
            expected_principals.add(value.strip())
    expected_sessions: set[str] = set()
    for key in ("session_ref", "first_session_ref", "second_session_ref"):
        value = args.get(key) if isinstance(args, dict) else None
        if isinstance(value, str) and value.strip():
            expected_sessions.add(value.strip())
    raw_principals: set[str] = set()
    # Session names come from the shared actual-use collector only; the
    # per-unit receipt-vs-echo rule lives in session_proof, not here.
    raw_sessions: set[str] = collect_actual_sessions(meta, raw)
    # Actual-only values for the mismatch gate below.
    actual_receipts: set[str] = set()
    http_obs = meta.get("http_observation")
    if isinstance(http_obs, dict):
        value = http_obs.get("actual_session_ref")
        if isinstance(value, str) and value.strip():
            actual_receipts.add(value.strip())
    comparison = meta.get("session_comparison")
    if isinstance(comparison, dict):
        for key in ("first_actual_session_ref", "second_actual_session_ref"):
            value = comparison.get(key)
            if isinstance(value, str) and value.strip():
                actual_receipts.add(value.strip())
        for key in ("first_principal_ref", "second_principal_ref"):
            value = comparison.get(key)
            if isinstance(value, str) and value.strip():
                raw_principals.add(value.strip())
    try:
        output = raw.get("output") if isinstance(raw, dict) else None
    except Exception:
        output = None
    if isinstance(output, dict):
        for key in (
            "actual_session_ref",
            "first_actual_session_ref",
            "second_actual_session_ref",
        ):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                actual_receipts.add(value.strip())
        for key in ("principal_ref", "first_principal_ref", "second_principal_ref"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                raw_principals.add(value.strip())
        for nested_key in ("first", "second", "authenticated", "anonymous"):
            nested = output.get(nested_key)
            if isinstance(nested, dict):
                value = nested.get("actual_session_ref")
                if isinstance(value, str) and value.strip():
                    actual_receipts.add(value.strip())
                value = nested.get("principal_ref")
                if isinstance(value, str) and value.strip():
                    raw_principals.add(value.strip())
    # Configured session inventory (session_list rows) is never actual-use
    # proof: it is not merged into the raw receipt sets above. Session to
    # principal binding stays on the separate alias-grounded lookup, and an
    # absent receipt proves nothing, including anonymous use.
    if actual_receipts and expected_sessions and actual_receipts.isdisjoint(expected_sessions):
        # The response says it used sessions nobody asked for: the whole
        # receipt channel is rejected and only the separate alias-grounded
        # lookup (outside this function) may still ground the claim.
        return set()
    verified: set[str] = set()
    for name in cleaned_claimed:
        if name in raw_principals:
            verified.add(name)
    for session in expected_sessions:
        if session in raw_sessions and session in cleaned_claimed:
            verified.add(session)
    return verified


async def _completion_alias_grounded(
    *,
    evidence_store: Any,
    graph_service: Any,
    run_id: str,
    target_ref: str,
    session_name: str,
    principal_name: str,
    arguments_target_ref: str | None = None,
) -> bool:
    from redteam.orchestration.analysis import _session_alias_grounded

    return await _session_alias_grounded(
        evidence_store=evidence_store,
        graph_service=graph_service,
        run_id=run_id,
        target_ref=target_ref,
        session_name=session_name,
        principal_name=principal_name,
        arguments_target_ref=arguments_target_ref,
    )


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


class CompletionIssues(list[str]):
    """Compatibility messages plus code-owned correction data without inputs."""

    def __init__(self) -> None:
        super().__init__()
        self.diagnostics: list[DecisionDiagnostic] = []

    def add(self, message: str, *, code: str, summary: str) -> None:
        self.append(message)
        self.diagnostics.append(
            DecisionDiagnostic(
                code=code,
                stage="completion_validation",
                kind="format",
                location="$.completion",
                summary=summary,
                retryable=True,
            )
        )

    def deduplicated(self) -> CompletionIssues:
        result = CompletionIssues()
        seen: set[str] = set()
        for message, diagnostic in zip(self, self.diagnostics, strict=True):
            if message not in seen:
                result.append(message)
                result.diagnostics.append(diagnostic)
                seen.add(message)
        return result


async def validate_completion(
    *,
    path: PathPlan,
    step: PathStep,
    proposal: dict,
    evidence_store: Any,
    graph_service: Any,
    run_id: str,
    path_store: Any | None = None,
) -> list[str]:
    """Code checks for a step completion proposal. Empty list means pass."""
    issues = CompletionIssues()
    if not isinstance(proposal, dict):
        return ["proposal must be a dict with evidence_ids/outcome/reason/criterion_results"]
    try:
        criteria_key(step.completion_criteria)
    except Exception as exc:  # noqa: BLE001 - malformed fixed criteria block success
        issues.add(
            f"step '{step.step_id or '?'}' has malformed completion criteria: {exc}",
            code="completion_step_reference_has_malformed_completion_criteria_11",
            summary="step 'reference' has malformed completion criteria: reference",
        )
    run = _s(run_id).strip()
    if not run:
        issues.add(
            "run_id is required",
            code="completion_run_id_is_required_1",
            summary="run_id is required",
        )
    path_id = _s(path.path_id).strip()
    step_id = _s(step.step_id).strip()
    if not path_id:
        issues.add(
            "path has no path_id",
            code="completion_path_has_no_path_id_2",
            summary="path has no path_id",
        )
    if not step_id:
        issues.add(
            "step has no step_id",
            code="completion_step_has_no_step_id_3",
            summary="step has no step_id",
        )
    if not is_active_status(path.status):
        issues.add(
            f"path '{path_id or '?'}' is not active (status={path.status or 'missing'})",
            code="completion_path_reference_is_not_active_status_4",
            summary="path 'reference' is not active (status=reference)",
        )
    path_step_ids = {_s(item.step_id).strip() for item in path.steps}
    if step_id and path_step_ids and step_id not in path_step_ids:
        issues.add(
            f"step '{step_id}' does not belong to path '{path_id or '?'}'",
            code="completion_step_reference_does_not_belong_to_5",
            summary="step 'reference' does not belong to path 'reference'",
        )
    # Predecessors: only required ones block completion.
    dep_ids = [_s(item).strip() for item in (step.depends_on or []) if _s(item).strip()]
    by_id = {_s(item.step_id).strip(): item for item in path.steps}
    for dep_id in dep_ids:
        dep = by_id.get(dep_id)
        if dep is None:
            issues.add(
                f"predecessor step '{dep_id}' not found in path '{path_id or '?'}'",
                code="completion_predecessor_step_reference_not_found_in_12",
                summary="predecessor step 'reference' not found in path 'reference'",
            )
            continue
        if bool(dep.required) and not _is_completed_status(dep.status):
            issues.add(
                f"required predecessor step '{dep_id}' is not completed "
                f"(status={dep.status or 'missing'})",
                code="completion_required_predecessor_step_reference_is_not_13",
                summary=(
                    "required predecessor step 'reference' is not completed (status=reference)"
                ),
            )
    outcome = _s(proposal.get("outcome", "")).strip().lower()
    if outcome not in _VALID_OUTCOMES:
        issues.add(
            "outcome must be one of success/refuted/inconclusive/blocked "
            f"(got={proposal.get('outcome')!r})",
            code="completion_outcome_must_be_one_of_success_6",
            summary=("outcome must be one of success/refuted/inconclusive/blocked (got=reference)"),
        )
        outcome = ""
    reason = proposal.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        issues.add(
            "proposal reason is required",
            code="completion_proposal_reason_is_required_7",
            summary="proposal reason is required",
        )
    raw_evidence_ids = proposal.get("evidence_ids", [])
    if isinstance(raw_evidence_ids, str):
        raw_evidence_ids = [raw_evidence_ids]
    try:
        evidence_ids = [
            _s(item).strip() for item in list(raw_evidence_ids or []) if _s(item).strip()
        ]
    except TypeError:
        evidence_ids = []
        issues.add(
            "proposal evidence_ids must be a list of evidence id strings",
            code="completion_proposal_evidence_ids_must_be_a_14",
            summary="proposal evidence_ids must be a list of evidence id strings",
        )
    if not evidence_ids:
        issues.add(
            "proposal has no evidence; completion needs real evidence",
            code="completion_proposal_has_no_evidence_completion_needs_8",
            summary="proposal has no evidence; completion needs real evidence",
        )
    if outcome in {"inconclusive", "blocked"}:
        issues.add(
            f"outcome '{outcome}' is not a successful completion",
            code="completion_outcome_reference_is_not_a_successful_9",
            summary="outcome 'reference' is not a successful completion",
        )
    return await validate_result_claims(
        criteria=_criteria_list(step),
        evidence_ids=evidence_ids,
        target_node_ids=list(step.target_node_ids),
        principal_refs=list(step.principal_refs),
        evidence_store=evidence_store,
        graph_service=graph_service,
        run_id=run,
        path_store=path_store,
        mission_id=path.mission_id,
        outcome=outcome,
        proposal=proposal,
        path=path,
        step=step,
        issues=issues,
    )


async def validate_result_claims(
    *,
    criteria: list[CompletionCriterion],
    evidence_ids: list[str],
    target_node_ids: list[str],
    principal_refs: list[str],
    evidence_store: Any,
    graph_service: Any,
    run_id: str,
    path_store: Any | None,
    mission_id: str | None,
    outcome: str = "success",
    proposal: dict | None = None,
    path: PathPlan | None = None,
    step: PathStep | None = None,
    issues: CompletionIssues | None = None,
) -> list[str]:
    """Verify actual result claims without requiring provisional steps to finish.

    Step completion optionally supplies its lineage context. Task/mission
    assessment uses stored origin existence and its actual result subjects,
    while sharing account proof, final verdict receipts and finding checks.
    Neither mode changes the source record's path, step or execution binding.
    """
    if issues is None:
        issues = CompletionIssues()
    run = str(run_id)
    proposal = proposal or {"evidence_ids": evidence_ids, "outcome": outcome}
    reason = proposal.get("reason", "")
    step_id = step.step_id if step else "result"
    # Per-evidence linkage checks. Missing path linkage can never succeed
    # (worker D records path_id/path_version/path_step_id/principal_ref).
    valid_records: list[Any] = []
    linked_records: list[Any] = []
    verified_by_evidence: dict[str, set[str]] = {}
    has_semantic = any(_criterion_kind(item) == "semantic" for item in criteria)
    step_target_set = {_s(item).strip() for item in target_node_ids if _s(item).strip()}
    step_principal_set = {_s(item).strip() for item in principal_refs if _s(item).strip()}
    # Resolve explicit receipt references into originals before the unchanged
    # path/origin/raw checks. Keep only latest per-claim receipts as links.
    try:
        admission_receipts = await _load_final_receipts(evidence_store, run)
    except Exception:
        admission_receipts = []
        issues.add("review receipt lookup failed", code="completion_review_receipt_lookup_failed",
                   summary="review receipt lookup failed")
    expanded_ids: list[str] = []
    for eid in evidence_ids:
        try:
            cited = await evidence_store.get(eid)
        except Exception:
            cited = None
        if cited is not None and is_final_admission_receipt(cited):
            admitted = next(
                (meta for meta in admission_receipts if meta.get("receipt_id") == str(eid)), None
            )
            if admitted is None:
                issues.add("review receipt is stale or unavailable",
                           code="completion_stale_review_receipt",
                           summary="review receipt is stale or unavailable")
                continue
            cited_meta = getattr(cited, "metadata", {}) or {}
            if (
                str(cited_meta.get("action_id", "")) != str(cited.action_id)
                or str(cited_meta.get("invocation_id", "")) != str(cited.invocation_id)
            ):
                issues.add(
                    "review receipt origin disagrees",
                    code="completion_receipt_origin_invalid",
                    summary="review receipt origin disagrees",
                )
                continue
            expanded_ids.extend(str(ref) for ref in admitted.get("source_evidence_ids", []) or [])
        else:
            expanded_ids.append(str(eid))
    evidence_ids = list(dict.fromkeys(expanded_ids))
    for eid in evidence_ids:
        try:
            record = await evidence_store.get(eid)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - store failure is a validation issue
            issues.add(
                f"evidence '{eid}' lookup failed: {type(exc).__name__}",
                code="completion_evidence_reference_lookup_failed_reference_39",
                summary="evidence 'reference' lookup failed: reference",
            )
            continue
        if record is None:
            issues.add(
                f"evidence '{eid}' does not exist",
                code="completion_evidence_reference_does_not_exist_15",
                summary="evidence 'reference' does not exist",
            )
            continue
        if _s(_field(record, "run_id", "")) != run:
            issues.add(
                f"evidence '{eid}' belongs to a different run",
                code="completion_evidence_reference_belongs_to_a_different_16",
                summary="evidence 'reference' belongs to a different run",
            )
            continue
        meta = _field(record, "metadata", {}) or {}
        if not isinstance(meta, dict):
            issues.add(
                f"evidence '{eid}' has unreadable metadata",
                code="completion_evidence_reference_has_unreadable_metadata_17",
                summary="evidence 'reference' has unreadable metadata",
            )
            continue
        m_path_id = _s(meta.get("path_id", "")).strip()
        m_version = meta.get("path_version", None)
        m_step_id = _s(meta.get("path_step_id", "")).strip()
        if not m_path_id or m_version is None or _s(m_version).strip() == "" or not m_step_id:
            issues.add(
                f"evidence '{eid}' has no path linkage "
                "(path_id/path_version/path_step_id required)",
                code="completion_evidence_reference_has_no_path_linkage_18",
                summary=(
                    "evidence 'reference' has no path linkage "
                    "(path_id/path_version/path_step_id required)"
                ),
            )
            continue
        ev_principals = _evidence_principal_refs(record)
        anonymous = _is_anonymous_evidence(meta)
        step_principal_lower = {str(item).strip().lower() for item in step_principal_set}
        explicit_anonymous = "anonymous" in step_principal_lower
        ev_lower = {str(item).strip().lower() for item in ev_principals}
        has_anonymous_name = "anonymous" in ev_lower
        if anonymous or has_anonymous_name:
            if not step_principal_set or explicit_anonymous:
                pass
            else:
                issues.add(
                    f"evidence '{eid}' is anonymous but the step requires an account; "
                    "anonymous receipt cannot satisfy an account-scoped step",
                    code="completion_evidence_reference_is_anonymous_but_the_40",
                    summary=(
                        "evidence 'reference' is anonymous but the step requires an "
                        "account; anonymous receipt cannot satisfy an account-scoped step"
                    ),
                )
                continue
        elif step_principal_set:
            if not ev_principals:
                issues.add(
                    f"evidence '{eid}' has no principal_ref",
                    code="completion_evidence_reference_has_no_principal_ref_60",
                    summary="evidence 'reference' has no principal_ref",
                )
                continue
            if ev_principals.isdisjoint(step_principal_set):
                issues.add(
                    f"evidence '{eid}' uses a principal outside the step scope",
                    code="completion_evidence_reference_uses_a_principal_outside_61",
                    summary="evidence 'reference' uses a principal outside the step scope",
                )
                continue
        # A step with no principal scope accepts anonymous initial
        # reconnaissance evidence as-is; an account-scoped step never does
        # unless it explicitly lists anonymous.
        if path is not None and step is not None:
            origin_errors = await verify_origin_step(
                path_store=path_store,
                run_id=run,
                path=path,
                step=step,
                m_path_id=m_path_id,
                m_version=m_version,
                m_step_id=m_step_id,
                evidence_id=eid,
            )
        else:
            origin_errors = [
                (f"evidence '{eid}' {message}", "completion_origin_invalid", message)
                for message in await verify_stored_origin(
                    path_store=path_store,
                    run_id=run,
                    mission_id=mission_id,
                    m_path_id=m_path_id,
                    m_version=m_version,
                    m_step_id=m_step_id,
                )
            ]
        for origin_message, origin_code, origin_summary in origin_errors:
            issues.add(origin_message, code=origin_code, summary=origin_summary)
        if origin_errors:
            continue
        canonical_value = _canonical_evidence_value(meta)
        if not canonical_value:
            issues.add(
                f"evidence '{eid}' has no canonical operation proof "
                "(bare operation alone proves nothing)",
                code="completion_evidence_reference_has_no_canonical_operation_21",
                summary=(
                    "evidence 'reference' has no canonical operation proof (bare "
                    "operation alone proves nothing)"
                ),
            )
            continue
        raw = _stored_raw_result(meta)
        if raw is None:
            issues.add(
                f"evidence '{eid}' has no preserved raw ToolResult (metadata tool_result required)",
                code="completion_evidence_reference_has_no_preserved_raw_22",
                summary=(
                    "evidence 'reference' has no preserved raw ToolResult (metadata "
                    "tool_result required)"
                ),
            )
            continue
        target_ref = _s(meta.get("target_node_id", "")).strip()
        if not target_ref:
            issues.add(
                f"evidence '{eid}' has no target linkage",
                code="completion_evidence_reference_has_no_target_linkage_23",
                summary="evidence 'reference' has no target linkage",
            )
            continue
        if step_target_set and target_ref not in step_target_set:
            related = False
            for receipt_meta in admission_receipts:
                claimed_target = str(receipt_meta.get("target_node_id", "") or "")
                if (
                    claimed_target in step_target_set
                    and _receipt_links_record(receipt_meta, record)
                ):
                    try:
                        related = await claim_target_is_grounded(
                            graph_service, record, claimed_target
                        )
                    except Exception:
                        related = False
                    if related:
                        break
            if not related:
                issues.add(
                    f"evidence '{eid}' targets a different object",
                    code="completion_evidence_reference_targets_a_different_object_24",
                    summary="evidence 'reference' targets a different object",
                )
                continue
        linkage_errors = check_record_linkage(record, meta, raw, eid)
        for linkage_message, linkage_code, linkage_summary in linkage_errors:
            issues.add(linkage_message, code=linkage_code, summary=linkage_summary)
        if linkage_errors:
            continue
        stored_proposal = meta.get("proposal")
        # Prefer the code-stored step account over tool args (same false-
        # rejection as the review path: args may carry only a session_ref).
        plan_step_dump = meta.get("plan_step")
        step_account_raw = (
            plan_step_dump.get("principal_ref") if isinstance(plan_step_dump, dict) else None
        )
        step_account = (
            step_account_raw.strip()
            if isinstance(step_account_raw, str) and step_account_raw.strip()
            else None
        )
        if step_account is not None and step_account in ev_principals:
            stored_claimed: str | None = step_account
        else:
            stored_claimed = (
                _claimed_proposal_principal(stored_proposal)
                if isinstance(stored_proposal, dict)
                else None
            )
        if stored_claimed is not None and stored_claimed not in ev_principals:
            issues.add(
                f"evidence '{eid}' stored account differs from its record",
                code="completion_evidence_reference_stored_account_differs_from_31",
                summary="evidence 'reference' stored account differs from its record",
            )
            continue
        raw_success = raw.get("success")
        if not isinstance(raw_success, bool):
            raw_success = meta.get("success")
        failed_original = raw_success is False or (
            raw_success is None and isinstance(meta.get("error"), dict) and bool(meta.get("error"))
        )
        if step_principal_set and not explicit_anonymous:
            claimed_accounts = {str(item).strip() for item in ev_principals if str(item).strip()}
            stored_args = (
                stored_proposal.get("arguments") if isinstance(stored_proposal, dict) else None
            )
            verified_names = _verified_completion_accounts(meta, raw, claimed_accounts, stored_args)
            if verified_names:
                verified_by_evidence[eid] = set(verified_names)
            else:
                # Past mappings interpret identity only for sessions whose
                # use is proven by this record's actual receipts.
                actual_sessions = collect_actual_sessions(meta, raw)
                bound_session: str | None = None
                if isinstance(stored_args, dict):
                    candidate = stored_args.get("session_ref")
                    if isinstance(candidate, str) and candidate.strip():
                        bound_session = candidate.strip()
                if bound_session is not None:
                    actual_sessions = actual_sessions & {bound_session}
                alias_verified: set[str] = set()
                if actual_sessions:
                    try:
                        for claimed_name in sorted(claimed_accounts):
                            for actual_session in sorted(actual_sessions):
                                if await _completion_alias_grounded(
                                    evidence_store=evidence_store,
                                    graph_service=graph_service,
                                    run_id=run,
                                    target_ref=target_ref,
                                    session_name=actual_session,
                                    principal_name=claimed_name,
                                    arguments_target_ref=stored_args.get("target_ref")
                                    if isinstance(stored_args, dict)
                                    else None,
                                ):
                                    alias_verified.add(claimed_name)
                                    break
                    except Exception:
                        issues.add(
                            f"evidence '{eid}' session binding lookup failed; "
                            "treating as ungrounded",
                            code="completion_evidence_reference_session_binding_lookup_failed_68",
                            summary=(
                                "evidence 'reference' session binding lookup failed; treating as "
                                "ungrounded"
                            ),
                        )
                        continue
                if alias_verified:
                    verified_by_evidence[eid] = set(alias_verified)
                else:
                    issues.add(
                        f"evidence '{eid}' declares account {sorted(claimed_accounts)} but has "
                        "no exact raw session receipt or grounded session binding; "
                        "declaration alone is not proof",
                        code="completion_evidence_reference_declares_account_reference_but_63",
                        summary=(
                            "evidence 'reference' declares account reference but has no exact "
                            "raw session receipt or grounded session binding; declaration "
                            "alone is not proof"
                        ),
                    )
                    continue
        if failed_original and outcome in {"success", "refuted"}:
            if has_semantic:
                # Failed originals stay readable as general results for
                # natural-language conditions; the review call weighs them
                # with the raw excerpt. Strong linkage above still applies.
                linked_records.append(record)
                continue
            if outcome == "success":
                issues.add(
                    f"evidence '{eid}' records a failed original result and cannot support success",
                    code="completion_evidence_reference_records_a_failed_original_32",
                    summary=(
                        "evidence 'reference' records a failed original result and cannot "
                        "support success"
                    ),
                )
            else:
                issues.add(
                    f"evidence '{eid}' records a failed original result; "
                    "refuted needs a successful observation with a valid negative result, "
                    "never inconclusive or failure",
                    code="completion_evidence_reference_records_a_failed_original_33",
                    summary=(
                        "evidence 'reference' records a failed original result; refuted "
                        "needs a successful observation with a valid negative result, "
                        "never inconclusive or failure"
                    ),
                )
            continue
        valid_records.append(record)
    if not criteria:
        issues.add(
            f"step '{step_id or '?'}' has no fixed completion criteria",
            code="completion_step_reference_has_no_fixed_completion_10",
            summary="step 'reference' has no fixed completion criteria",
        )
    evaluated: list[tuple[CompletionCriterion | str, bool, str]] = []
    final_receipts: list[dict[str, Any]] | None = None
    receipts_failed = False
    verified_findings: list[Any] | None = None
    findings_failed = False
    for index, criterion in enumerate(criteria):
        label = _criterion_label(criterion, index)
        kind = _criterion_kind(criterion)
        if not is_known_criterion(criterion):
            issues.add(
                f"unknown completion condition cannot succeed: {label}",
                code="completion_unknown_completion_condition_cannot_succeed_reference_34",
                summary="unknown completion condition cannot succeed: reference",
            )
            evaluated.append((criterion, False, "unknown"))
            continue
        min_evidence = _criterion_min_evidence(criterion)
        hid = _criterion_hypothesis_id(criterion)
        required_principals = set(_criterion_principal_refs(criterion))
        if kind in {"hypothesis_confirmed", "hypothesis_refuted"} and not hid:
            issues.add(
                f"{label} needs a hypothesis_id",
                code="completion_reference_needs_a_hypothesis_id_35",
                summary="reference needs a hypothesis_id",
            )
            evaluated.append((criterion, False, "missing hypothesis"))
            continue
        pool = list(valid_records)
        if kind == "semantic" and linked_records:
            pool = pool + [rec for rec in linked_records if rec not in pool]
        if required_principals:
            verified_pool: list[Any] = []
            have = set()
            for rec in pool:
                try:
                    rec_id = _s(_field(rec, "evidence_id", "")).strip()
                except Exception:
                    rec_id = ""
                verified = verified_by_evidence.get(rec_id, set())
                if verified & required_principals:
                    verified_pool.append(rec)
                    have |= verified & required_principals
            pool = verified_pool
            missing_principals = required_principals - have
            if missing_principals:
                issues.add(
                    f"{label} missing principal scope: {sorted(missing_principals)}",
                    code="completion_reference_missing_principal_scope_reference_46",
                    summary="reference missing principal scope: reference",
                )
                evaluated.append((criterion, False, "principal scope"))
                continue
        if kind in {"hypothesis_confirmed", "hypothesis_refuted"} and hid:
            pool = [
                rec for rec in pool
                if _evidence_hypothesis_id(rec) == hid or any(
                    str(meta.get("hypothesis_id", "")) == hid and _receipt_links_record(meta, rec)
                    for meta in admission_receipts
                )
            ]
            status = ""
            try:
                hypothesis = await graph_service.get_hypothesis(hid)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - graph failure blocks success
                issues.add(
                    f"{label} hypothesis lookup failed: {type(exc).__name__}",
                    code="completion_reference_hypothesis_lookup_failed_reference_64",
                    summary="reference hypothesis lookup failed: reference",
                )
                evaluated.append((criterion, False, "hypothesis lookup"))
                continue
            if hypothesis is None:
                issues.add(
                    f"{label} references an unknown hypothesis '{hid}'",
                    code="completion_reference_references_an_unknown_hypothesis_reference_47",
                    summary="reference references an unknown hypothesis 'reference'",
                )
                evaluated.append((criterion, False, "unknown hypothesis"))
                continue
            status = _norm_status(_field(hypothesis, "status", ""))
            if kind == "hypothesis_confirmed" and status != "confirmed":
                issues.add(
                    f"{label} hypothesis '{hid}' is not confirmed (status={status or '?'})",
                    code="completion_reference_hypothesis_reference_is_not_confirmed_48",
                    summary="reference hypothesis 'reference' is not confirmed (status=reference)",
                )
                evaluated.append((criterion, False, "hypothesis status"))
                continue
            if kind == "hypothesis_refuted" and status not in {"rejected", "refuted"}:
                issues.add(
                    f"{label} hypothesis '{hid}' is not refuted (status={status or '?'})",
                    code="completion_reference_hypothesis_reference_is_not_refuted_49",
                    summary="reference hypothesis 'reference' is not refuted (status=reference)",
                )
                evaluated.append((criterion, False, "hypothesis status"))
                continue
            if final_receipts is None and not receipts_failed:
                try:
                    final_receipts = await _load_final_receipts(evidence_store, run)
                except Exception:
                    receipts_failed = True
                    final_receipts = []
            if receipts_failed:
                issues.add(
                    f"{label} review receipt lookup failed; treating as unproven",
                    code="completion_reference_review_receipt_lookup_failed_treating_50",
                    summary="reference review receipt lookup failed; treating as unproven",
                )
                evaluated.append((criterion, False, "receipt lookup"))
                continue
            assert final_receipts is not None
            pool_ids, pool_pairs = _pool_record_ids(pool)
            if kind == "hypothesis_confirmed":
                if not _final_receipt_confirms(final_receipts, pool_ids, pool_pairs, hid):
                    issues.add(
                        f"{label} has no final admission receipt confirming it "
                        "(benign success with claim_supported True required; interim excluded)",
                        code="completion_reference_has_no_final_admission_receipt_65",
                        summary=(
                            "reference has no final admission receipt confirming it (benign "
                            "success with claim_supported True required; interim excluded)"
                        ),
                    )
                    evaluated.append((criterion, False, "receipt confirmation"))
                    continue
            else:
                if not _final_receipt_refutes(final_receipts, pool_ids, pool_pairs, hid):
                    issues.add(
                        f"{label} has no final admission receipt refuting it "
                        "(claim_supported False required; interim excluded)",
                        code="completion_reference_has_no_final_admission_receipt_66",
                        summary=(
                            "reference has no final admission receipt refuting it "
                            "(claim_supported False required; interim excluded)"
                        ),
                    )
                    evaluated.append((criterion, False, "receipt confirmation"))
                    continue
        if len(pool) < min_evidence:
            issues.add(
                f"{label} needs {min_evidence} evidence(s), found {len(pool)}",
                code="completion_reference_needs_reference_evidence_s_found_36",
                summary="reference needs reference evidence(s), found reference",
            )
            evaluated.append((criterion, False, "evidence count"))
            continue
        if kind == "semantic":
            # Code admits here: origin reality, per-purpose success handling,
            # and required target and principal linkage already passed above.
            # The natural-language meaning stays with the caller existing
            # review, which receives the raw excerpts with the proposal.
            evaluated.append((criterion, True, "ok"))
            continue
        if kind == "poc":
            if outcome == "refuted":
                issues.add(
                    f"{label}: refuted is an investigation result, not PoC success",
                    code="completion_reference_refuted_is_an_investigation_result_51",
                    summary="reference: refuted is an investigation result, not PoC success",
                )
                evaluated.append((criterion, False, "refuted is not poc"))
                continue
            backed = False
            for rec in pool:
                rec_meta = _field(rec, "metadata", {}) or {}
                ok = isinstance(rec_meta, dict) and rec_meta.get("success") is True
                if (
                    _field(rec, "source_type", None) is not None
                    and _field(rec, "source_type", None) is not EvidenceSourceType.TOOL
                ):
                    ok = False
                if ok:
                    backed = True
                    break
            if not backed:
                issues.add(
                    f"{label} needs successful tool evidence",
                    code="completion_reference_needs_successful_tool_evidence_52",
                    summary="reference needs successful tool evidence",
                )
                evaluated.append((criterion, False, "poc backing"))
                continue
            if final_receipts is None and not receipts_failed:
                try:
                    final_receipts = await _load_final_receipts(evidence_store, run)
                except Exception:
                    receipts_failed = True
                    final_receipts = []
            if receipts_failed:
                issues.add(
                    f"{label} review receipt lookup failed; treating as unproven",
                    code="completion_reference_review_receipt_lookup_failed_treating_53",
                    summary="reference review receipt lookup failed; treating as unproven",
                )
                evaluated.append((criterion, False, "receipt lookup"))
                continue
            assert final_receipts is not None
            poc_ids, poc_pairs = _pool_record_ids(pool)
            poc_receipts = _final_success_violation_receipts(
                final_receipts, poc_ids, poc_pairs, hid
            )
            if not poc_receipts:
                issues.add(
                    f"{label} has no final admission success/violation receipt "
                    "(same run/action/invocation/source; interim excluded)",
                    code="completion_reference_has_no_final_admission_success_54",
                    summary=(
                        "reference has no final admission success/violation receipt (same "
                        "run/action/invocation/source; interim excluded)"
                    ),
                )
                evaluated.append((criterion, False, "poc receipt"))
                continue
            if verified_findings is None and not findings_failed:
                try:
                    verified_findings = await graph_service.list_findings(verified=True)  # type: ignore[union-attr]
                except Exception:
                    findings_failed = True
                    verified_findings = []
            if findings_failed:
                issues.add(
                    f"{label} finding lookup failed; treating as unproven",
                    code="completion_reference_finding_lookup_failed_treating_as_55",
                    summary="reference finding lookup failed; treating as unproven",
                )
                evaluated.append((criterion, False, "finding lookup"))
                continue
            assert verified_findings is not None
            finding_ok = False
            for finding in verified_findings:
                try:
                    finding_evidence = {
                        str(item) for item in (finding.evidence_ids or []) if str(item).strip()
                    }
                    finding_meta = getattr(finding, "metadata", None) or {}
                except Exception:
                    continue
                if not isinstance(finding_meta, dict):
                    finding_meta = {}
                if not (finding_evidence & poc_ids):
                    continue
                if hid is not None and hid.strip():
                    linked_hid = finding_meta.get("hypothesis_id")
                    if linked_hid is None or str(linked_hid).strip() != hid.strip():
                        continue
                receipt_ref = str(finding_meta.get("review_receipt_id") or "")
                check_hid = hid or (finding_meta.get("hypothesis_id") if receipt_ref else None)
                if check_hid:
                    try:
                        current_hypothesis = await graph_service.get_hypothesis(check_hid)
                    except Exception:
                        continue
                    if (
                        current_hypothesis is None
                        or _norm_status(current_hypothesis.status) != "confirmed"
                    ):
                        continue
                if receipt_ref:
                    if not any(
                        str(meta.get("receipt_id", "")) == receipt_ref
                        for meta in poc_receipts
                    ):
                        # A later review of the same claim/source group can
                        # reconfirm an already promoted finding. Another claim
                        # or a latest negative result cannot borrow it.
                        try:
                            promoted_review = await evidence_store.get(receipt_ref)
                        except Exception:
                            continue
                        if (
                            promoted_review is None
                            or str(promoted_review.run_id) != run
                            or not is_final_admission_receipt(promoted_review)
                        ):
                            continue
                        promoted_meta = {
                            **promoted_review.metadata,
                            "engagement_id": str(promoted_review.engagement_id),
                        }
                        if not any(
                            _receipt_review_key(meta) == _receipt_review_key(promoted_meta)
                            for meta in poc_receipts
                        ):
                            continue
                elif not any(meta.get("posthoc") is not True for meta in poc_receipts):
                    # Older findings can consume only their legacy admission
                    # semantics, never a new claim's unpromoted review.
                    continue
                finding_ok = True
                break
            if not finding_ok:
                issues.add(
                    f"{label} has no verified Finding for the same hypothesis and source",
                    code="completion_reference_has_no_verified_finding_for_56",
                    summary="reference has no verified Finding for the same hypothesis and source",
                )
                evaluated.append((criterion, False, "poc finding"))
                continue
        evaluated.append((criterion, True, "ok"))
    if outcome == "refuted":
        refuted_kinds = [c for c in criteria if _criterion_kind(c) == "hypothesis_refuted"]
        if not refuted_kinds:
            issues.add(
                "refuted outcome needs a hypothesis_refuted completion condition",
                code="completion_refuted_outcome_needs_a_hypothesis_refuted_37",
                summary="refuted outcome needs a hypothesis_refuted completion condition",
            )
        else:
            refuted_ok = any(
                passed
                for criterion, passed, _ in evaluated
                if _criterion_kind(criterion) == "hypothesis_refuted"
            )
            if not refuted_ok:
                issues.add(
                    "refuted outcome has no satisfied hypothesis_refuted condition",
                    code="completion_refuted_outcome_has_no_satisfied_hypothesis_57",
                    summary="refuted outcome has no satisfied hypothesis_refuted condition",
                )
        for eval_criterion, passed, _ in evaluated:
            kind = _criterion_kind(eval_criterion)
            if kind == "poc" and passed:
                issues.add(
                    "refuted outcome cannot claim PoC success",
                    code="completion_refuted_outcome_cannot_claim_poc_success_58",
                    summary="refuted outcome cannot claim PoC success",
                )
                break
            if kind == "hypothesis_confirmed" and passed:
                issues.add(
                    "refuted outcome contradicts a confirmed hypothesis condition",
                    code="completion_refuted_outcome_contradicts_a_confirmed_hypothesis_59",
                    summary="refuted outcome contradicts a confirmed hypothesis condition",
                )
                break
    if outcome == "success" and not valid_records:
        lowered_reason = _s(reason).lower()
        if any(phrase in lowered_reason for phrase in _NO_CANDIDATE_PHRASES):
            issues.add(
                "lack of candidates alone cannot justify completion",
                code="completion_lack_of_candidates_alone_cannot_justify_38",
                summary="lack of candidates alone cannot justify completion",
            )
    # Proposal claim vs reality: only flag explicit false-met claims.
    claims = _proposal_claims(proposal)
    for key, claimed in claims:
        if claimed is not True:
            continue
        for index, criterion in enumerate(criteria):
            if _claim_matches_criterion(key, criterion, index):
                actual = next((passed for crit, passed, _ in evaluated if crit is criterion), None)
                if actual is False:
                    issues.add(
                        f"proposal claims '{key}' is met but "
                        f"{_criterion_label(criterion, index)} is not satisfied",
                        code="completion_proposal_claims_reference_is_met_but_67",
                        summary="proposal claims 'reference' is met but reference is not satisfied",
                    )
                break
    return issues.deduplicated()


__all__ = [
    "check_record_linkage",
    "verify_stored_origin",
    "verify_origin_step",
    "validate_completion",
    "validate_result_claims",
]
