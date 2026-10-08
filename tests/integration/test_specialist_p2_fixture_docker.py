"""Opt-in real Docker, synthetic fixture programs only. No LLM or target scans."""

import asyncio
import os

import pytest

from redteam.app.agent_profile import load_specialist_profile
from redteam.capability.fixture_runner import DockerFixtureRunner
from redteam.core.ports.fixture_runner import FixtureProgram

pytestmark = pytest.mark.skipif(
    os.environ.get("REDTEAM_TEST_FIXTURE_DOCKER") != "1",
    reason="set REDTEAM_TEST_FIXTURE_DOCKER=1 with the profile's pinned cached image",
)


class ObservedRunner(DockerFixtureRunner):
    def __init__(self, policy):
        super().__init__(policy)
        self.name = None
        self.started = asyncio.Event()

    def arguments(self, directory, name):
        self.name = name
        self.started.set()
        return super().arguments(directory, name)


def runner(**changes):
    policy = load_specialist_profile("config/agents/specialists-p2.yaml").profile.fixture_policy
    return ObservedRunner(policy.model_copy(update=changes))


def program(source, value=None, expected=True):
    return FixtureProgram(source=source, cases=[{"input": value, "expected": expected}])


async def assert_removed(executor):
    process = await asyncio.create_subprocess_exec(
        "docker", "inspect", executor.name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    assert await asyncio.wait_for(process.wait(), 5) != 0


async def test_actual_network_host_file_permissions_and_fixture_result(tmp_path):
    executor = runner()
    canary = tmp_path / "host-secret"
    canary.write_text("never-mounted", encoding="utf-8")
    source = '''import os, socket
from pathlib import Path
def evaluate(host_path):
    absent = not Path(host_path).exists()
    read_only = False
    try:
        Path("/fixture/unapproved").write_text("bad")
    except OSError:
        read_only = True
    denied = False
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=0.5)
    except OSError:
        denied = True
    status = Path("/proc/self/status").read_text()
    no_caps = any(line.startswith("CapEff:") and int(line.split()[1], 16) == 0
                  for line in status.splitlines())
    memory = Path("/sys/fs/cgroup/memory.max").read_text().strip() == "134217728"
    pids = Path("/sys/fs/cgroup/pids.max").read_text().strip() == "32"
    quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    limited_cpu = quota != "max" and int(quota) / int(period) <= 0.5
    return (absent and read_only and denied and no_caps and os.getuid() != 0
            and memory and pids and limited_cpu)
'''
    result = await executor.run(program(source, str(canary)))
    assert result.status == "passed", result
    assert result.fixture_only is True and result.passed == 1
    assert canary.read_text() == "never-mounted"
    await assert_removed(executor)


async def test_actual_fixture_failure_is_not_silently_success():
    executor = runner()
    result = await executor.run(program("def evaluate(value):\n    return False\n"))
    assert result.status == "failed" and result.passed == 0
    await assert_removed(executor)


@pytest.mark.parametrize("source", [
    "import os\nprint('{\"passed\":1,\"total\":1}', flush=True)\nos._exit(0)\n",
    "import os\ndef evaluate(value): return False\n"
    "print('{\"passed\":1,\"total\":1}', flush=True)\nos._exit(0)\n",
    "def evaluate(value):\n    from pathlib import Path\n"
    "    return Path('/fixture/cases.json').exists()\n",
    "def evaluate(value): return 1\n",
])
async def test_actual_candidate_cannot_self_report_verdict_or_read_oracle(source):
    executor = runner()
    result = await executor.run(program(source))
    assert result.status == "failed" and result.passed == 0
    await assert_removed(executor)


async def test_actual_host_oracle_preserves_partial_counts():
    executor = runner()
    result = await executor.run(FixtureProgram(
        source="def evaluate(value): return value + 1\n",
        cases=[{"input": 1, "expected": 2}, {"input": 2, "expected": 99}],
    ))
    assert result.status == "failed" and (result.passed, result.total) == (1, 2)
    await assert_removed(executor)


async def test_actual_infinite_fixture_timeout_and_container_reclaimed():
    executor = runner(timeout_seconds=2)
    result = await executor.run(program("def evaluate(value):\n    while True:\n        pass\n"))
    assert result.status == "timed_out"
    await assert_removed(executor)


async def test_actual_untrusted_output_is_bounded_and_not_returned():
    executor = runner()
    source = "def evaluate(value):\n    print('X' * 100000)\n    return True\n"
    result = await executor.run(program(source))
    assert result.status == "output_limit"
    assert "XXX" not in result.model_dump_json()
    await assert_removed(executor)


async def test_actual_cancel_removes_task_owned_container():
    executor = runner(timeout_seconds=10)
    task = asyncio.create_task(executor.run(
        program("def evaluate(value):\n    while True: pass\n"),
    ))
    await asyncio.wait_for(executor.started.wait(), 3)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 8)
    await assert_removed(executor)
