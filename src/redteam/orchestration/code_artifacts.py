"""Retained artifact stage identity; execution is not part of the central lifecycle."""

STAGES = frozenset({"code_generation", "code_review"})


def validate_stages(stages) -> bool:
    """Owned optional feature contract; arbitrary registered role IDs work."""
    present = STAGES & stages.keys()
    if present and (present != STAGES or stages["code_generation"] == stages["code_review"]):
        raise ValueError("code_independent_stage_bindings_required")
    return bool(present)
