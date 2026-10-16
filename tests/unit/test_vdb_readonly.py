"""Default tests need no VDB download/package, credentials or external server."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from redteam.capability.servers import vdb_readonly
from redteam.capability.servers.vdb_readonly import VDBError, VDBPolicy, VDBReader
from redteam.capability.settings import MCPConfigurationError, MCPServerSettings


def policy(tmp_path: Path, **changes) -> VDBPolicy:
    return VDBPolicy(
        root=str(tmp_path),
        asset_ref="vdb-test",
        engagement_id="eng-test",
        run_id="run-test",
        synthetic=True,
        **changes,
    )


def test_missing_status_does_not_materialize_db(tmp_path):
    reader = VDBReader(policy(tmp_path))
    assert reader.status().state == "missing"
    assert list(tmp_path.iterdir()) == []
    tracked_environment = {
        key: os.environ.get(key)
        for key in ("VDB_HOME", "VDB_CACHE", "VDB_AUTO_FETCH", "VDB_INCLUDE_METADATA")
    }
    with pytest.raises(VDBError, match="vdb_lookup_unavailable"):
        reader.lookup("cve", "CVE-2026-900001", 2)
    assert list(tmp_path.iterdir()) == []
    assert {key: os.environ.get(key) for key in tracked_environment} == tracked_environment


def test_status_does_not_take_the_lookup_global_lock(tmp_path, monkeypatch):
    class UnexpectedLock:
        def __enter__(self):
            pytest.fail("status must not wait on AppThreat lookup globals")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(vdb_readonly, "_UPSTREAM_LOCK", UnexpectedLock())
    assert VDBReader(policy(tmp_path)).status().state == "missing"


@pytest.mark.parametrize(("cap", "expected_peak"), [(1, 1), (2, 2)])
def test_lookup_concurrency_is_bounded_by_policy_and_defaults_to_one(
    tmp_path, monkeypatch, cap, expected_peak
):
    assert VDBPolicy(
        asset_ref="vdb-test", engagement_id="eng-test", run_id="run-test", root=str(tmp_path)
    ).model_dump(mode="json").get("max_concurrent_lookups") is None
    assert policy(tmp_path, max_concurrent_lookups=3).model_dump(mode="json")[
        "max_concurrent_lookups"
    ] == 3
    with pytest.raises(ValueError):
        policy(tmp_path, max_concurrent_lookups=0)
    with pytest.raises(ValueError):
        policy(tmp_path, max_concurrent_lookups=9)

    status = vdb_readonly.VDBStatus(asset_ref="vdb-test", state="missing", synthetic=True)
    active = peak = 0
    active_guard = threading.Lock()
    first_waiting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def fake_worker(request, _deadline, _timeout_code):
        nonlocal active, peak
        with active_guard:
            active += 1
            peak = max(peak, active)
            if request.query == "CVE-2026-900001":
                first_waiting.set()
            else:
                second_entered.set()
        if request.query == "CVE-2026-900001":
            release_first.wait(2)
        with active_guard:
            active -= 1
        return vdb_readonly.VDBLookup(
            database=status,
            query=request.query,
            matches=[],
            truncated=False,
            interpretation="no_known_match_in_loaded_snapshot",
        )

    monkeypatch.setattr(vdb_readonly, "_invoke_lookup_worker", fake_worker)
    reader = VDBReader(policy(tmp_path, max_concurrent_lookups=cap))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reader.lookup, "cve", "CVE-2026-900001", 1)
        assert first_waiting.wait(1)
        second = pool.submit(reader.lookup, "cve", "CVE-2026-900002", 1)
        try:
            if cap == 1:
                assert not second_entered.wait(0.05)
                release_first.set()
                assert first.result(timeout=1).query == "CVE-2026-900001"
                assert second.result(timeout=1).query == "CVE-2026-900002"
            else:
                assert second_entered.wait(1)
                assert second.result(timeout=1).query == "CVE-2026-900002"
                release_first.set()
                assert first.result(timeout=1).query == "CVE-2026-900001"
        finally:
            release_first.set()
    assert peak == expected_peak


def test_lookup_worker_kills_a_worker_that_exceeds_bounded_stdout(tmp_path, monkeypatch):
    class InputPipe:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            chunk = bytes(data[:7])
            self.data.extend(chunk)
            return len(chunk)

        def close(self):
            pass

    class OutputPipe:
        def __init__(self, data):
            self.data = data

        def read(self, count):
            chunk, self.data = self.data[:count], self.data[count:]
            return chunk

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = InputPipe()
            self.stdout = OutputPipe(b"x" * (vdb_readonly._LOOKUP_WORKER_OUTPUT_BYTES + 1))
            self.killed = False

        def wait(self, timeout=None):
            return -9 if self.killed else 0

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(vdb_readonly.subprocess, "Popen", lambda *_args, **_kwargs: process)
    reader = VDBReader(policy(tmp_path))
    with pytest.raises(VDBError, match="vdb_lookup_unavailable"):
        reader.lookup("cve", "CVE-2026-900001", 1)
    assert process.killed
    assert json.loads(process.stdin.data)["query"] == "CVE-2026-900001"


def test_lookup_worker_timeout_kills_and_reaps_child(tmp_path, monkeypatch):
    class InputPipe:
        def write(self, data):
            return len(data)

        def close(self):
            pass

    class OutputPipe:
        def read(self, _count):
            return b""

        def close(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = InputPipe()
            self.stdout = OutputPipe()
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if not self.killed:
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(vdb_readonly.subprocess, "Popen", lambda *_args, **_kwargs: process)
    reader = VDBReader(policy(tmp_path))
    with pytest.raises(VDBError, match="vdb_batch_timeout"):
        reader.lookup(
            "cve", "CVE-2026-900001", 1, deadline=time.monotonic() + 0.5
        )
    assert process.killed and process.wait_calls == 2


@pytest.mark.parametrize("uncertain_resource", ["process", "reader", "writer"])
def test_uncertain_worker_cleanup_is_bounded_and_retains_slots(
    tmp_path, monkeypatch, uncertain_resource
):
    retained = []
    monkeypatch.setattr(vdb_readonly, "_retain_lookup_slots", lambda *args: retained.append(
        vdb_readonly._RetainedLookupSlots(*args)
    ))
    class Pipe:
        def __init__(self):
            self.closed = False

        def write(self, data):
            return len(data)

        def read(self, _count):
            return b""

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.stdin = Pipe()
            self.stdout = Pipe()
            self.killed = False
            self.wait_timeouts = []
            self.finished = False

        def poll(self):
            if self.finished:
                return 0
            if uncertain_resource == "process":
                return None
            return -9 if self.killed else 0

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if uncertain_resource == "process":
                raise subprocess.TimeoutExpired("worker", timeout)
            return -9 if self.killed else 0

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(vdb_readonly.subprocess, "Popen", lambda *_args, **_kwargs: process)

    class FakeThread:
        next_index = 0
        instances = []
        finished = False

        def __init__(self, target, daemon):
            del target, daemon
            self.index = FakeThread.next_index
            FakeThread.next_index += 1
            self.started = False
            self.join_timeouts = []
            FakeThread.instances.append(self)

        def start(self):
            self.started = True

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            stuck_index = {"reader": 0, "writer": 1}.get(uncertain_resource)
            return self.started and self.index == stuck_index and not self.finished

    monkeypatch.setattr(vdb_readonly, "Thread", FakeThread)
    policy_slots = threading.BoundedSemaphore(1)
    worker_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(vdb_readonly, "_LOOKUP_WORKER_SLOTS", worker_slots)
    reader = VDBReader(policy(tmp_path))
    reader._lookup_slots = policy_slots
    started = time.monotonic()
    with pytest.raises(VDBError, match="vdb_lookup_cleanup_uncertain"):
        reader.lookup("cve", "CVE-2026-900001", 1)
    assert time.monotonic() - started < 1
    assert not policy_slots.acquire(timeout=0)
    assert not worker_slots.acquire(timeout=0)
    stuck_index = {"reader": 0, "writer": 1}.get(uncertain_resource)
    if stuck_index is not None:
        cleanup_join = FakeThread.instances[stuck_index].join_timeouts[-1]
        assert cleanup_join <= vdb_readonly._LOOKUP_WORKER_CLEANUP_SECONDS
    if uncertain_resource == "process":
        assert process.killed
        assert len(process.wait_timeouts) == 2
        assert process.wait_timeouts[1] <= vdb_readonly._LOOKUP_WORKER_CLEANUP_SECONDS
        assert process.stdin.closed and process.stdout.closed
    elif uncertain_resource == "reader":
        assert process.stdout.closed is False
        assert process.stdin.closed is True
    else:
        assert process.stdin.closed is False
        assert process.stdout.closed is True

    assert len(retained) == 1 and not retained[0].reap()
    process.finished = True
    FakeThread.finished = True
    assert retained[0].reap()
    assert retained[0].reap()  # A repeated proof cannot over-release bounded semaphores.
    assert policy_slots.acquire(timeout=0) and worker_slots.acquire(timeout=0)
    assert not policy_slots.acquire(timeout=0) and not worker_slots.acquire(timeout=0)
    policy_slots.release()
    worker_slots.release()
    output = vdb_readonly.VDBLookup(
        database=vdb_readonly.VDBStatus(asset_ref="vdb-test", state="missing", synthetic=True),
        query="CVE-2026-900001", matches=[], truncated=False,
        interpretation="no_known_match_in_loaded_snapshot",
    )
    monkeypatch.setattr(vdb_readonly, "_invoke_lookup_worker", lambda *_args: output)
    assert reader.lookup("cve", "CVE-2026-900001", 1) == output


def test_background_reaper_restores_only_owned_slots_after_termination():
    policy_slots = threading.BoundedSemaphore(1)
    worker_slots = threading.BoundedSemaphore(1)
    other_reader_slots = threading.BoundedSemaphore(1)
    assert policy_slots.acquire(timeout=0) and worker_slots.acquire(timeout=0)
    assert other_reader_slots.acquire(timeout=0)
    process_done = threading.Event()
    pipe_done = threading.Event()
    inspected = threading.Event()

    def stopped():
        inspected.set()
        return process_done.is_set() and pipe_done.is_set()

    vdb_readonly._retain_lookup_slots(stopped, policy_slots, worker_slots)
    assert inspected.wait(1)
    assert not policy_slots.acquire(timeout=0) and not worker_slots.acquire(timeout=0)
    process_done.set()
    inspected.clear()
    assert inspected.wait(1)
    assert not policy_slots.acquire(timeout=0) and not worker_slots.acquire(timeout=0)
    pipe_done.set()
    assert policy_slots.acquire(timeout=2) and worker_slots.acquire(timeout=2)
    assert not other_reader_slots.acquire(timeout=0)
    # Slots remain consumed by this subsequent owner, not returned on each reaper tick.
    assert not policy_slots.acquire(timeout=0.2) and not worker_slots.acquire(timeout=0)
    policy_slots.release()
    worker_slots.release()
    other_reader_slots.release()


def test_lookup_worker_uses_overlapping_isolated_real_processes(tmp_path, monkeypatch):
    fixture_script = tmp_path / "lookup_worker_fixture.py"
    fixture_script.write_text(
        "\n".join(
            [
                "import json, os, pathlib, sys, time",
                "request = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
                "root = pathlib.Path(os.environ['VDB_TEST_DIR'])",
                "record = root / f\"{request['query'][-1]}.json\"",
                "record.write_text(",
                "    json.dumps({",
                "        'pid': os.getpid(),",
                "        'home': os.getenv('VDB_HOME'),",
                "        'cache': os.getenv('VDB_CACHE'),",
                "    }),",
                "    encoding='utf-8',",
                ")",
                "deadline = time.monotonic() + 3",
                "while len(list(root.glob('*.json'))) < 2 and time.monotonic() < deadline:",
                "    time.sleep(0.005)",
                "if len(list(root.glob('*.json'))) < 2: raise SystemExit(3)",
                "result = {",
                "    'database': {'asset_ref': 'vdb-test', 'state': 'missing', 'synthetic': True},",
                "    'query': request['query'],",
                "    'matches': [],",
                "    'truncated': False,",
                "    'interpretation': 'no_known_match_in_loaded_snapshot',",
                "}",
                "sys.stdout.write(json.dumps({'lookup': result}, separators=(',', ':'))) ",
                "sys.stdout.flush()",
            ]
        ),
        encoding="utf-8",
    )
    real_popen = subprocess.Popen

    def fixture_popen(argv, **kwargs):
        assert argv[0] == sys.executable
        assert argv[-1] == "--lookup-worker"
        env = dict(kwargs["env"])
        env["VDB_TEST_DIR"] = str(tmp_path)
        options = {**kwargs, "env": env}
        return real_popen([sys.executable, str(fixture_script)], **options)

    monkeypatch.setattr(vdb_readonly.subprocess, "Popen", fixture_popen)
    for name in ("VDB_HOME", "VDB_CACHE", "VDB_AUTO_FETCH", "VDB_INCLUDE_METADATA"):
        monkeypatch.setenv(name, f"parent-secret-{name}")
    reader = VDBReader(policy(tmp_path, max_concurrent_lookups=2))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reader.lookup, "cve", "CVE-2026-900001", 1)
        second = pool.submit(reader.lookup, "cve", "CVE-2026-900002", 1)
        results = [first.result(timeout=7), second.result(timeout=7)]

    records = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*.json")]
    assert len(records) == 2
    assert records[0]["pid"] != records[1]["pid"]
    assert all(record["home"] is None and record["cache"] is None for record in records)
    assert {result.query for result in results} == {"CVE-2026-900001", "CVE-2026-900002"}
    assert all(
        os.environ[name] == f"parent-secret-{name}"
        for name in ("VDB_HOME", "VDB_CACHE", "VDB_AUTO_FETCH", "VDB_INCLUDE_METADATA")
    )


def test_lookup_worker_kills_and_reaps_a_real_timed_out_process(tmp_path, monkeypatch):
    fixture_script = tmp_path / "sleeping_lookup_worker.py"
    fixture_script.write_text(
        "import os, pathlib, sys, time; "
        "sys.stdin.buffer.read(); "
        "pathlib.Path(os.environ['VDB_TEST_PID_FILE']).write_text(str(os.getpid())); "
        "time.sleep(30)",
        encoding="utf-8",
    )
    pid_file = tmp_path / "worker.pid"
    real_popen = subprocess.Popen
    children = []

    def fixture_popen(argv, **kwargs):
        env = dict(kwargs["env"])
        env["VDB_TEST_PID_FILE"] = str(pid_file)
        options = {**kwargs, "env": env}
        process = real_popen([sys.executable, str(fixture_script)], **options)
        children.append(process)
        return process

    monkeypatch.setattr(vdb_readonly.subprocess, "Popen", fixture_popen)
    reader = VDBReader(policy(tmp_path))
    with pytest.raises(VDBError, match="vdb_batch_timeout"):
        reader.lookup(
            "cve", "CVE-2026-900001", 1, deadline=time.monotonic() + 2
        )
    assert len(children) == 1
    assert pid_file.exists()
    assert children[0].poll() is not None


def test_mcp_sync_tool_dispatch_allows_configured_worker_overlap(tmp_path, monkeypatch):
    policy_config = policy(tmp_path, max_concurrent_lookups=2)
    server = vdb_readonly.create_server(policy_config)
    status = vdb_readonly.VDBStatus(asset_ref="vdb-test", state="missing", synthetic=True)
    active = peak = 0
    active_guard = threading.Lock()
    first_waiting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def fake_worker(request, _deadline, _timeout_code):
        nonlocal active, peak
        with active_guard:
            active += 1
            peak = max(peak, active)
            if request.query == "CVE-2026-900001":
                first_waiting.set()
            else:
                second_entered.set()
        if request.query == "CVE-2026-900001":
            release_first.wait(2)
        with active_guard:
            active -= 1
        return vdb_readonly.VDBLookup(
            database=status,
            query=request.query,
            matches=[],
            truncated=False,
            interpretation="no_known_match_in_loaded_snapshot",
        )

    monkeypatch.setattr(vdb_readonly, "_invoke_lookup_worker", fake_worker)

    async def invoke_concurrently():
        first = asyncio.create_task(
            server.call_tool(
                "search_by_cve",
                {
                    "asset_ref": "vdb-test",
                    "engagement_id": "eng-test",
                    "run_id": "run-test",
                    "cve_id": "CVE-2026-900001",
                    "limit": 1,
                },
            )
        )
        assert await asyncio.to_thread(first_waiting.wait, 1)
        second = asyncio.create_task(
            server.call_tool(
                "search_by_cve",
                {
                    "asset_ref": "vdb-test",
                    "engagement_id": "eng-test",
                    "run_id": "run-test",
                    "cve_id": "CVE-2026-900002",
                    "limit": 1,
                },
            )
        )
        try:
            assert await asyncio.to_thread(second_entered.wait, 1)
            return await asyncio.gather(first, second)
        finally:
            release_first.set()

    results = asyncio.run(invoke_concurrently())
    assert len(results) == 2
    assert peak == 2


@pytest.mark.parametrize("query", ["CVE-2026-%", "CVE-2026-1", "cve-2026-900001", "';DROP", "x\n"])
def test_cve_queries_fail_closed(tmp_path, query):
    with pytest.raises(VDBError, match="vdb_invalid"):
        VDBReader(policy(tmp_path)).lookup("cve", query, 1)


@pytest.mark.parametrize("limit", [0, -1, 51, True])
def test_limit_fail_closed(tmp_path, limit):
    with pytest.raises(VDBError, match="vdb_invalid_limit"):
        VDBReader(policy(tmp_path)).lookup("cve", "CVE-2026-900001", limit)


def test_root_symlink_blocked(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(VDBError, match="vdb_invalid_root"):
        VDBReader(policy(link))


def test_snapshot_links_blocked(tmp_path):
    target = tmp_path / "operator-db"
    target.write_bytes(b"x")
    (tmp_path / "data.vdb7").symlink_to(target)
    reader = VDBReader(policy(tmp_path))
    assert reader.status().state == "invalid"
    assert target.read_bytes() == b"x"


def test_context_and_bounded_source_text(tmp_path):
    reader = VDBReader(policy(tmp_path))
    reader.bind("vdb-test", "eng-test", "run-test")
    with pytest.raises(VDBError, match="vdb_context_mismatch"):
        reader.bind("vdb-test", "eng-other", "run-test")
    hit = {"cve_id": "CVE-2026-900001", "references": ["x" * 2000] * 50}
    original = deepcopy(hit)
    match = reader._match(hit)
    assert len(match.references) == 20
    assert len(match.references[0]) == 1024
    assert match.severity is None and match.source is None
    assert hit == original


def test_missing_backend_and_version_drift_are_explicit(tmp_path, monkeypatch):
    for name in ("data.vdb7", "data.index.vdb7"):
        (tmp_path / name).write_bytes(b"placeholder")
    reader = VDBReader(policy(tmp_path))

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr("redteam.capability.servers.vdb_readonly.version", missing)
    assert reader.status().state == "backend_unavailable"
    monkeypatch.setattr("redteam.capability.servers.vdb_readonly.version", lambda name: "9.0.0")
    assert reader.status().state == "backend_version_mismatch"


def test_snapshot_hardlink_blocked(tmp_path):
    import os

    target = tmp_path / "operator-db"
    target.write_bytes(b"x")
    os.link(target, tmp_path / "data.vdb7")
    assert VDBReader(policy(tmp_path)).status().state == "invalid"


def configure(monkeypatch):
    values = {
        "KIND": "analysis",
        "TRANSPORT": "stdio",
        "COMMAND": "python",
        "PROFILE": "vdb_readonly",
        "VDB_ASSET_REF": "vdb-test",
        "RISK_LEVEL": "medium",
        "ALLOWED_TOOLS_JSON": '["vdb_status","search_by_cve","search_by_purl_like"]',
        "TOOL_OPERATIONS_JSON": '{"vdb_status":["response_analysis"],'
        '"search_by_cve":["static_analysis"],'
        '"search_by_purl_like":["static_analysis"]}',
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)


def test_profile_exact_allowlist_and_bindings(monkeypatch):
    configure(monkeypatch)
    settings = MCPServerSettings.from_environment("vdb-test")
    assert settings.vdb_asset_ref == "vdb-test"
    assert settings.workspace_asset_ref is None
    monkeypatch.setenv(
        "REDTEAM_MCP_VDB_TEST_ALLOWED_TOOLS_JSON",
        '["vdb_status","search_by_cve","search_by_purl_like","db_refresh"]',
    )
    with pytest.raises(MCPConfigurationError, match="exact allowlist"):
        MCPServerSettings.from_environment("vdb-test")


@pytest.mark.parametrize(
    "suffix,value", [("KIND", "web_api"), ("RISK_LEVEL", "low"), ("VDB_ASSET_REF", "/host/path")]
)
def test_profile_rejects_unsafe_configuration(monkeypatch, suffix, value):
    configure(monkeypatch)
    monkeypatch.setenv(f"REDTEAM_MCP_VDB_TEST_{suffix}", value)
    with pytest.raises(MCPConfigurationError):
        MCPServerSettings.from_environment("vdb-test")
