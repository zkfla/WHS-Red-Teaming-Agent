"""Opt-in verification of UNMODIFIED upstream discovery, synthetic snapshot only.

Upstream startup is intentionally not used for Agent execution. An operator
selects a local source checkout; source SHA is verified before execution.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from redteam.capability.adapters.mcp.transport import StdioMCPTransport
from redteam.capability.servers.vdb_readonly import UPSTREAM_COMMIT


async def test_pinned_upstream_actual_discovery(tmp_path):
    python = os.environ.get("REDTEAM_VDB_TEST_PYTHON")
    source_path = os.environ.get("REDTEAM_VDB_UPSTREAM_SOURCE")
    if not python or not source_path:
        pytest.skip("set isolated interpreter and pinned source checkout for upstream verification")
    source = Path(source_path)
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    assert actual == UPSTREAM_COMMIT
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "synthetic-db"
    subprocess.run(
        [python, str(repo / "examples/build_vdb_synthetic_fixture.py"), "--root", str(root)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    cache = tmp_path / "cache"
    cache.mkdir()
    transport = StdioMCPTransport(
        command=python,
        args=(
            "-c",
            "import asyncio;from mcp_server_vdb.server import run;asyncio.run(run())",
        ),
        env={
            "PYTHONPATH": str(source / "packages/mcp-server-vdb/src") + os.pathsep + str(source),
            "VDB_HOME": str(root),
            "VDB_CACHE": str(cache),
            "VDB_AGE_DAYS": "9999",
            "VDB_AUTO_FETCH": "false",
            "VDB_INCLUDE_METADATA": "false",
        },
    )
    async with transport.session():
        tools = {t.name: t for t in await transport.list_tools()}
        assert {"search_by_cve", "search_by_purl_like", "db_refresh"} <= set(tools)
        assert tools["search_by_cve"].input_schema["required"] == ["cve_id"]
        assert tools["search_by_purl_like"].input_schema["required"] == ["purl"]
        assert "max_results" in tools["search_by_cve"].input_schema["properties"]
        # The bridge publishes its own precise schema, not an invented upstream one.
        assert tools["search_by_cve"].output_schema is None
