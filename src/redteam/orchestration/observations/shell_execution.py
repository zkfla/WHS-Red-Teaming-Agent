"""Shell execution envelopes: recognition and execution accounting.

A shell execution envelope is the fixed output dict the shell provider
writes for one synchronous run or finished job (exit_code, stdout,
stderr, timed_out, truncated, duration_ms, script_sha256,
workroom_key). The helpers here are pure and generic: they never branch
on tool, server, program, or model names, and they never interpret
stdout/stderr content.

Accounting (exit code, timeout, truncation, byte counts) is execution
metadata for reports and diagnostics. It is never a remote observation,
never a finding, and never goal-completion evidence: content stays out
on purpose (character counts only), so a secret in the output cannot
leak through this path into reports.
"""

from __future__ import annotations

from typing import Any

#: Exact output keys the shell provider writes per execution. The
#: registered output schema forbids additional properties, so presence
#: of all eight is the recognition rule; extras are tolerated so a
#: future additive envelope field cannot silently flip recognition.
SHELL_ENVELOPE_KEYS = frozenset(
    {
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "truncated",
        "duration_ms",
        "script_sha256",
        "workroom_key",
    }
)


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def is_shell_execution_envelope(output: Any) -> bool:
    """Whether output has the shell execution envelope shape.

    Shape only: key presence plus scalar types. stdout/stderr must be
    strings but their content is never inspected. exit_code uses
    type(...) is int (not isinstance) so a boolean never passes as a
    process exit code.
    """
    if not isinstance(output, dict):
        return False
    if not SHELL_ENVELOPE_KEYS.issubset(output.keys()):
        return False
    if output["exit_code"] is not None and type(output["exit_code"]) is not int:
        return False
    if not isinstance(output["stdout"], str):
        return False
    if not isinstance(output["stderr"], str):
        return False
    if type(output["timed_out"]) is not bool:
        return False
    if type(output["truncated"]) is not bool:
        return False
    if not _is_count(output["duration_ms"]):
        return False
    if not isinstance(output["script_sha256"], str) or not output["script_sha256"].strip():
        return False
    if not isinstance(output["workroom_key"], str) or not output["workroom_key"].strip():
        return False
    return True


def shell_execution_accounting(output: Any) -> dict[str, Any] | None:
    """Execution accounting for a shell envelope, or None.

    Counts and codes only: stdout_chars/stderr_chars replace the streams
    so report consumers can show scale without touching content. The
    streams themselves, and any meaning derived from them, are never
    included. Returns None when output is not an envelope.
    """
    if not is_shell_execution_envelope(output):
        return None
    assert isinstance(output, dict)
    return {
        "exit_code": output["exit_code"],
        "timed_out": output["timed_out"],
        "truncated": output["truncated"],
        "duration_ms": output["duration_ms"],
        "script_sha256": output["script_sha256"],
        "stdout_chars": len(output["stdout"]),
        "stderr_chars": len(output["stderr"]),
        "workroom_key": output["workroom_key"],
    }
