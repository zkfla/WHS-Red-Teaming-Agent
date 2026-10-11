"""Fixture-only adapter over the existing supervised, network-none workroom.

No new shell engine, Docker policy, egress switch, image pull or host fallback.
Expected values stay outside the container. Source/output are untrusted DATA;
passing self-supplied cases is never a vulnerability or security assessment.
"""

import asyncio
import hashlib
import json
import time
from typing import Literal

from redteam.capability.shell_policy import ShellWorkroomPolicy, load_shell_workroom_policy
from redteam.capability.shell_workroom import ShellWorkroomManager
from redteam.core.ports.code_fixtures import (
    CodeFixtureRequest,
    CodeFixtureResult,
)


def _reject_constant(_value):
    raise ValueError("fixture_non_json_number")


def fixture_script(source: str, value: dict) -> str:
    """Provider mechanism; called only inside the existing confined child."""
    # repr of JSON text prevents source/data from breaking the harness syntax.
    material = json.dumps({"source": source, "input": value}, allow_nan=False)
    return (
        "import json\n"
        f"_fixture = json.loads({material!r})\n"
        "_namespace = {}\n"
        "exec(compile(_fixture['source'], '<artifact>', 'exec'), _namespace)\n"
        "_value = _namespace['evaluate'](_fixture['input'])\n"
        "if not isinstance(_value, dict):\n"
        "    raise TypeError('fixture_output_not_object')\n"
        "print(json.dumps(_value, allow_nan=False, sort_keys=True))\n"
    )


class WorkroomCodeFixtureRunner:
    def __init__(self, policy: ShellWorkroomPolicy, *, workroom=None) -> None:
        policy.validate_image()
        if policy.allow_unpinned_image:
            raise ValueError("fixture_pinned_image_required")
        if policy.default_timeout_seconds > policy.max_timeout_seconds:
            raise ValueError("fixture_timeout_policy_invalid")
        self.policy = policy
        # This manager is private to fixture validation. In particular, never
        # attach the gated-network admission used by target shell tools.
        self.workroom = workroom or ShellWorkroomManager(policy)
        self._slots = asyncio.Semaphore(policy.max_jobs)
        material = {"mechanism": "supervised-json-fixtures-v1",
                    "policy": policy.model_dump(mode="json")}
        self._fingerprint = hashlib.sha256(
            json.dumps(material, sort_keys=True).encode()
        ).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    async def _cleanup(self, key: str) -> bool:
        try:
            if not await self.workroom.verify_workroom_ownership(key):
                return False  # never remove a foreign/same-name container
            return await self.workroom.remove_workroom(key)
        except Exception:
            return False

    async def run(self, request: CodeFixtureRequest) -> CodeFixtureResult:
        request = CodeFixtureRequest.model_validate(request.model_dump(mode="json"))
        scripts = [fixture_script(request.source, case.input) for case in request.cases]
        if any(len(script.encode()) > self.policy.max_script_bytes for script in scripts):
            return CodeFixtureResult(state="failed", reason_code="fixture_input_oversized",
                                     cases_checked=0, cases_passed=0, cleanup_verified=True)
        # The journaled validation identity deterministically locates a lost
        # fixture container too. Never an execution actor's warm workroom.
        # The caller's atomic one-shot admission forbids uncertain redispatch.
        key = "fixture." + request.validation_id
        checked = passed = 0
        state: Literal["passed", "failed", "held"] = "held"
        reason = "fixture_execution_unknown"
        cleanup = False
        attempted = False
        async with self._slots:
            try:
                attempted = True
                await self.workroom.ensure_workroom(key)
                deadline = time.monotonic() + self.policy.default_timeout_seconds
                for index, (case, script) in enumerate(zip(request.cases, scripts, strict=True)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state, reason = "failed", "fixture_timeout"
                        break
                    identity = hashlib.sha256(
                        f"{request.validation_id}:{index}".encode()
                    ).hexdigest()
                    result = await self.workroom.exec_script(
                        key, "python3", script, timeout_seconds=remaining,
                        exec_tag=identity, invocation_id=identity,
                        input_digest=hashlib.sha256(script.encode()).hexdigest(),
                    )
                    if result.timed_out:
                        state, reason = "failed", "fixture_timeout"
                        break
                    if result.exit_code is None:
                        state, reason = "held", "fixture_execution_unknown"
                        break
                    if result.truncated or result.exit_code != 0:
                        state, reason = "failed", "fixture_output_or_execution_failed"
                        break
                    try:
                        observed = json.loads(result.stdout, parse_constant=_reject_constant)
                        if not isinstance(observed, dict):
                            raise ValueError("not_an_object")
                        # Canonical JSON, not Python's True == 1 coercion.
                        actual = json.dumps(observed, allow_nan=False, sort_keys=True)
                        expected = json.dumps(case.expected, allow_nan=False, sort_keys=True)
                    except (ValueError, TypeError, RecursionError):
                        state, reason = "failed", "fixture_output_invalid"
                        break
                    checked += 1
                    passed += int(actual == expected)
                else:
                    state = "passed" if checked == passed else "failed"
                    reason = ("fixture_cases_passed" if state == "passed"
                              else "fixture_case_mismatch")
            except asyncio.CancelledError:
                # Existing supervisor tears down tagged execs; remove only this
                # verified fixture container too. Never swallow caller cancel.
                raise
            except Exception:
                # Do not expose arbitrary child/stdout/Docker/policy values.
                state, reason = "held", "fixture_execution_unknown"
            finally:
                if attempted:
                    cleanup_task = asyncio.create_task(self._cleanup(key))
                    try:
                        cleanup = await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        await cleanup_task
                        raise
            if not cleanup:
                state, reason = "held", "fixture_cleanup_unverified"
            return CodeFixtureResult(state=state, reason_code=reason,
                                     cases_checked=checked, cases_passed=passed,
                                     cleanup_verified=cleanup)


def build_code_fixture_runner(path: str | None) -> WorkroomCodeFixtureRunner | None:
    if path is None:
        return None
    try:
        return WorkroomCodeFixtureRunner(load_shell_workroom_policy(path))
    except (ValueError, OSError):
        raise ValueError("fixture_operator_policy_invalid") from None
