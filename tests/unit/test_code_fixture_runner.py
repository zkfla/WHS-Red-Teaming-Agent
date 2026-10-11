"""Fixture mechanism tests with explicit fake workrooms; no host exec."""

import ast
import asyncio
import json
from dataclasses import replace

import pytest

from redteam.capability.code_fixtures import WorkroomCodeFixtureRunner, fixture_script
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult
from redteam.core.ports.code_fixtures import CodeFixtureRequest, CodeFixtureResult

IMAGE = "sha256:" + "a" * 64


class Workroom:
    def __init__(self):
        self.keys = []
        self.scripts = []
        self.removed = []
        self.owned = True
        self.cleanup = True
        self.hook = None
        self.result = ExecResult(0, '{"observed": true}', "secret-child-output", False,
                                 False, 1, "fake", "fake")

    async def ensure_workroom(self, key):
        self.keys.append(key)

    async def exec_script(self, key, language, script, **kwargs):
        assert language == "python3" and kwargs["timeout_seconds"] > 0
        assert len(kwargs["input_digest"]) == 64
        self.scripts.append(script)
        if self.hook is not None:
            await self.hook()
        return self.result

    async def verify_workroom_ownership(self, key):
        return self.owned

    async def remove_workroom(self, key):
        self.removed.append(key)
        return self.cleanup


def request(**overrides):
    material = {"validation_id": "b" * 64,
                "source": "def evaluate(value):\n    return {'observed': bool(value)}",
                "cases": [{"name": "one", "input": {"value": 1},
                           "expected": {"observed": True}}]}
    return CodeFixtureRequest.model_validate({**material, **overrides})


async def test_existing_network_none_mechanism_no_expected_values_no_host_execution(tmp_path):
    room = Workroom()
    runner = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE), workroom=room)
    marker = tmp_path / "never-on-host"
    source = f"open({str(marker)!r}, 'w').write('bad')"
    result = await runner.run(request(source=source))
    assert result.state == "passed"  # fake output only, NOT a live sandbox proof
    assert not marker.exists()
    assert len(room.scripts) == len(room.removed) == 1
    assert "expected" not in room.scripts[0]
    ast.parse(fixture_script("def evaluate(v): return v", {"value": "'\n\\"}))
    assert not result.target_execution_enabled and result.fixture_only
    assert "secret" not in result.model_dump_json()
    await runner.run(request())
    assert room.removed == room.keys  # fresh removed workspace, never a target actor workspace
    assert room.keys[0] == "fixture." + request().validation_id


@pytest.mark.parametrize("kind,reason", [
    ("mismatch", "fixture_case_mismatch"), ("bool-int", "fixture_case_mismatch"),
    ("exit", "fixture_output_or_execution_failed"),
    ("truncate", "fixture_output_or_execution_failed"), ("timeout", "fixture_timeout"),
    ("unknown", "fixture_execution_unknown"), ("array", "fixture_output_invalid"),
    ("nan", "fixture_output_invalid"), ("extra-json", "fixture_output_invalid"),
])
async def test_untrusted_outputs_never_gain_success(kind, reason):
    room = Workroom()
    replacements = {
        "mismatch": {"stdout": '{"observed": false}'},
        "bool-int": {"stdout": '{"observed": 1}'}, "exit": {"exit_code": 1},
        "truncate": {"truncated": True}, "timeout": {"timed_out": True, "exit_code": None},
        "unknown": {"exit_code": None}, "array": {"stdout": "[]"},
        "nan": {"stdout": '{"observed": NaN}'},
        "extra-json": {"stdout": '{"observed": true}\n{}'},
    }
    room.result = replace(room.result, **replacements[kind])
    runner = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE), workroom=room)
    result = await runner.run(request())
    assert result.state != "passed" and result.reason_code == reason
    assert room.removed == room.keys


@pytest.mark.parametrize("foreign", [False, True])
async def test_unverified_cleanup_is_held_and_foreign_container_not_deleted(foreign):
    room = Workroom()
    room.owned = not foreign
    room.cleanup = False
    runner = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE), workroom=room)
    result = await runner.run(request())
    assert result.state == "held" and not result.cleanup_verified
    assert bool(room.removed) is not foreign


async def test_cancellation_teardown_then_propagation():
    room = Workroom()
    entered = asyncio.Event()

    async def blocked():
        entered.set()
        await asyncio.Event().wait()

    room.hook = blocked
    runner = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE), workroom=room)
    task = asyncio.create_task(runner.run(request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert room.removed == room.keys


def test_pinned_operator_policy_and_fingerprint():
    with pytest.raises(ValueError, match="fixture_pinned_image_required"):
        WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image="tag", allow_unpinned_image=True))
    with pytest.raises(ValueError, match="fixture_timeout_policy_invalid"):
        WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE, max_timeout_seconds=1))
    first = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE))
    same = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE))
    changed = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(image=IMAGE, max_output_bytes=1024))
    assert first.fingerprint == same.fingerprint != changed.fingerprint


@pytest.mark.parametrize("patch", [
    {"cleanup_verified": False}, {"cases_checked": 0, "cases_passed": 0},
    {"cases_passed": 2}, {"target_execution_enabled": True}, {"fixture_only": False},
])
def test_provider_result_contract_rejects_false_success(patch):
    base = {"state": "passed", "reason_code": "fixture_cases_passed", "cases_checked": 1,
            "cases_passed": 1, "cleanup_verified": True}
    with pytest.raises(ValueError):
        CodeFixtureResult.model_validate({**base, **patch})


async def test_input_limit_checked_before_creating_workroom():
    room = Workroom()
    runner = WorkroomCodeFixtureRunner(
        ShellWorkroomPolicy(image=IMAGE, max_script_bytes=1024), workroom=room,
    )
    result = await runner.run(request(source="#" * 2000))
    assert result.reason_code == "fixture_input_oversized" and not room.keys
    assert json.loads(request().model_dump_json())["cases"][0]["name"] == "one"


@pytest.mark.parametrize("value,reason", [
    (float("nan"), "fixture_json_invalid"), ("x" * 65536, "fixture_request_oversized"),
])
def test_request_json_and_total_byte_bound(value, reason):
    with pytest.raises(ValueError, match=reason):
        request(cases=[{"name": "bounded", "input": {"value": value}, "expected": {}}])
