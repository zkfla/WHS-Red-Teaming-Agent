"""Opt-in local pinned-image fixture execution; no target/LLM requests."""

import asyncio
import os
from pathlib import Path

import pytest

from redteam.capability.code_fixtures import WorkroomCodeFixtureRunner
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.core.ports.code_fixtures import CodeFixtureRequest

IMAGE = os.environ.get("REDTEAM_CODE_FIXTURE_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="explicit pinned local fixture image required")


def runner(**limits):
    return WorkroomCodeFixtureRunner(ShellWorkroomPolicy(
        image=IMAGE, default_timeout_seconds=10, max_timeout_seconds=20, **limits,
    ))


def request(source, expected):
    return CodeFixtureRequest(validation_id="d" * 64, source=source,
                              cases=[{"name": "local-only", "input": {"n": 6},
                                      "expected": expected}])


async def test_real_supervised_fixture_and_security_flags():
    code = """import os
def evaluate(value):
    status = dict(line.strip().split(':', 1) for line in open('/proc/self/status') if ':' in line)
    return {'answer': value['n'] * 7, 'uid': os.getuid(),
            'caps': status['CapEff'].strip(), 'no_new_privs': status['NoNewPrivs'].strip()}
"""
    provider = runner()
    result = await provider.run(request(code, {
        "answer": 42, "uid": 65532, "caps": "0000000000000000", "no_new_privs": "1",
    }))
    assert result.state == "passed", result
    assert result.cleanup_verified and not provider.workroom.known_workrooms()


async def test_real_timeout_removes_whole_fixture_container():
    provider = WorkroomCodeFixtureRunner(ShellWorkroomPolicy(
        image=IMAGE, default_timeout_seconds=1, max_timeout_seconds=2, exec_grace_seconds=1,
    ))
    result = await provider.run(request("def evaluate(v):\n    while True: pass", {}))
    assert result.state == "failed" and result.reason_code == "fixture_timeout", result
    assert result.cleanup_verified and not provider.workroom.known_workrooms()


async def test_real_code_cannot_write_host_or_access_external_network(tmp_path):
    marker = Path(tmp_path) / "must-not-exist-on-host"
    # Only a documentation/test address, no financial/remote service traffic.
    # The socket cannot transmit through the network-none namespace.
    code = f"""import socket
def evaluate(v):
    try:
        open({str(marker)!r}, 'w').write('bad')
        host_write = True
    except OSError:
        host_write = False
    with socket.socket() as sock:
        sock.settimeout(0.25)
        try:
            sock.connect(('192.0.2.1', 9))
            connected = True
        except OSError:
            connected = False
    return {{'host_write': host_write, 'connected': connected}}
"""
    result = await runner().run(request(code, {"host_write": False, "connected": False}))
    assert result.state == "passed", result
    assert not marker.exists()


async def test_real_cancel_cleans_up_and_does_not_return_success():
    provider = runner()
    entered = asyncio.Event()
    original = provider.workroom.exec_script

    async def observed(*args, **kwargs):
        entered.set()
        return await original(*args, **kwargs)

    provider.workroom.exec_script = observed
    task = asyncio.create_task(provider.run(request("def evaluate(v):\n    while True: pass", {})))
    await asyncio.wait_for(entered.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not provider.workroom.known_workrooms()
