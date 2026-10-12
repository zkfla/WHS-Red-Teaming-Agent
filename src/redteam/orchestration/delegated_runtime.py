"""Terminal projection of retained delegation records; no execution engine."""


def close_delegations(runtime, reason):
    """Keep collected references and the original exit cause without inference."""
    if reason in {"approval_pending", "operator_paused", "resource_wait"}:
        return runtime
    values = {
        identity: state
        if state.status == "finished"
        else state.model_copy(
            update={"status": "finished", "termination_reason": reason},
        )
        for identity, state in runtime.delegations.items()
    }
    return runtime.model_copy(update={"delegations": values})
