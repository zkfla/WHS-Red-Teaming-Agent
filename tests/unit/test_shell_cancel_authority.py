"""A clean tag scan is not proof that scrubbed descendants were reaped."""

import json
from types import SimpleNamespace

import pytest

from redteam.capability import shell_workroom as module
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ShellWorkroomManager


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


class ObservedManager(ShellWorkroomManager):
    def __init__(self, clock):
        super().__init__(ShellWorkroomPolicy(image="sha256:" + "a" * 64))
        self.clock, self.modes = clock, []
        self.state, self.owned = "running", True
        self.record_at, self.clean_at = 3.0, 3.0
        self.result = {"outcome": "cancelled", "protected": True,
                       "subreaper": True, "unreaped": []}
        self.removed = False

    async def workroom_container_state(self, key):
        return self.state

    async def verify_workroom_ownership(self, key):
        return self.owned

    async def _run_docker(self, argv, *args):
        mode = next((arg.partition("=")[2] for arg in argv
                     if arg.startswith("WHS_SHELL_MODE=")), None)
        if mode:
            self.modes.append((mode, self.clock.now))
        return 0, b"", b"", False, False

    async def _read_result_record(self, name, tag):
        return self.result if self.clock.now >= self.record_at else None

    async def _read_outcome(self, name, tag):
        record = await self._read_result_record(name, tag)
        return record["outcome"] if record else None

    async def _check_tag_clean(self, name, tag):
        return self.clock.now >= self.clean_at

    async def _remove_run_files(self, name, tag):
        self.removed = True


def observed(monkeypatch):
    clock = Clock()
    manager = ObservedManager(clock)
    # Replace only this module's references, never the event loop's clock/sleep.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=clock.sleep))
    return manager


async def test_supervisor_grace_and_reap_finish_before_escalation(monkeypatch):
    manager = observed(monkeypatch)
    result = await manager.kill_tagged_exec("test.scope", "test-exec")
    assert result.verified and result.outcome == "cancelled"
    assert not any(mode == "kill" and now < 3.0 for mode, now in manager.modes)
    assert not manager.removed  # keep the protected terminal original for recovery


@pytest.mark.parametrize("record", [None, {"outcome": "cancelled", "protected": False},
    {"outcome": "cancelled", "protected": True, "subreaper": False, "unreaped": []},
    {"outcome": "cancelled", "protected": True, "subreaper": True, "unreaped": [99]},
    {"outcome": "invented", "protected": True, "subreaper": True, "unreaped": []},
])
async def test_clean_scan_without_trusted_terminal_proof_is_held(monkeypatch, record):
    manager = observed(monkeypatch)
    manager.clean_at, manager.record_at, manager.result = 0, 0, record
    result = await manager.kill_tagged_exec("test.scope", "test-exec")
    assert not result.verified and not manager.removed


@pytest.mark.parametrize("state,owned", [("unknown", True), ("running", False),
                                        ("stopped", False)])
async def test_unverified_container_is_not_signalled_or_reported_gone(monkeypatch, state, owned):
    manager = observed(monkeypatch)
    manager.state, manager.owned = state, owned
    result = await manager.kill_tagged_exec("test.scope", "test-exec")
    assert not result.verified and not manager.modes


@pytest.mark.parametrize("state", ["gone", "stopped"])
async def test_authoritative_stopped_or_gone_has_no_live_children(monkeypatch, state):
    manager = observed(monkeypatch)
    manager.state = state
    result = await manager.kill_tagged_exec("test.scope", "test-exec")
    assert result.verified and not manager.modes


class AnswerManager(ShellWorkroomManager):
    def __init__(self, answer):
        super().__init__(ShellWorkroomPolicy(image="sha256:" + "a" * 64))
        self.answer, self.limits = answer, []

    async def _run_docker(self, argv, stdin, timeout, limit):
        self.limits.append(limit)
        return self.answer


@pytest.mark.parametrize("body,code,outer,trunc,expected", [
    (b"true\n", 0, False, False, "running"),
    (b"false\n", 0, False, False, "stopped"),
    (b"", 0, False, False, "unknown"),
    (b"invalid", 0, False, False, "unknown"),
    (b"false", 0, True, False, "unknown"),
    (b"false", 0, False, True, "unknown"),
    (b"", 1, False, False, "gone"),
    (b"", 1, True, False, "unknown"),
    (b"", 1, False, True, "unknown"),
])
async def test_container_state_requires_complete_exact_authority(
    body, code, outer, trunc, expected,
):
    manager = AnswerManager((code, body, b"No such container", outer, trunc))
    assert await manager.workroom_container_state("test.scope") == expected
    assert manager.limits == [65536]


@pytest.mark.parametrize("outer,trunc", [(False, False), (True, False), (False, True)])
async def test_protected_record_read_refuses_partial_collection(outer, trunc):
    record = {"outcome": "cancelled", "protected": True, "subreaper": True, "unreaped": []}
    manager = AnswerManager((0, json.dumps(record).encode(), b"", outer, trunc))
    assert await manager.supervisor_record("test.scope", "test-exec") == (
        None if outer or trunc else record
    )
    assert manager.limits == [65536]


@pytest.mark.parametrize("outer,trunc", [(False, False), (True, False), (False, True)])
async def test_image_identity_read_refuses_partial_collection(outer, trunc):
    image_id = "sha256:" + "a" * 64
    manager = AnswerManager((0, image_id.encode(), b"", outer, trunc))
    assert await manager._resolve_image_id(image_id) == (None if outer or trunc else image_id)


@pytest.mark.parametrize("outer,trunc", [(False, False), (True, False), (False, True)])
async def test_clean_scan_requires_complete_collection(outer, trunc):
    manager = AnswerManager((0, b"", b"", outer, trunc))
    assert await manager._check_tag_clean("container", "test-exec") is (not outer and not trunc)
