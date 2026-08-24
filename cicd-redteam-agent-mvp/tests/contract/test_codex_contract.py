import inspect

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox


def test_codex_sdk_supports_required_auth_and_structured_read_only_contract() -> None:
    start_parameters = inspect.signature(AsyncCodex.thread_start).parameters
    run_parameters = inspect.signature(AsyncThread.run).parameters

    assert "account" in dir(AsyncCodex)
    assert {"cwd", "ephemeral", "model", "sandbox", "approval_mode"} <= set(
        start_parameters
    )
    assert {"output_schema", "sandbox", "approval_mode"} <= set(run_parameters)
    assert Sandbox.read_only.value == "read-only"
    assert ApprovalMode.deny_all.value == "deny_all"
