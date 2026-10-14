"""Central resume/collection forwards pinned execution identity, without Docker."""

from contextlib import AsyncExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.app.central_run import bind_run_metadata
from redteam.app.cli.commands import resume, run
from redteam.core.config import LLMConfig
from tests.integration.test_central_completion import selection
from tests.integration.test_llm_connection_resume import cli_config

EXEC_FP = "ab" * 32


@pytest.mark.parametrize("collect", [False, True])
@pytest.mark.parametrize("fingerprint", [EXEC_FP, None])
@pytest.mark.parametrize("network_fingerprint", ["cd" * 32, None])
async def test_central_dispatch_forwards_original_environment(
    tmp_path,
    monkeypatch,
    collect,
    fingerprint,
    network_fingerprint,
):
    async with selection(tmp_path) as (session, _):
        binding = session.deps.component_binding.model_copy(
            update={
                "execution_environment_fingerprint": fingerprint,
                "network_environment_fingerprint": network_fingerprint,
            }
        )
        # Pin before re-recording the initial metadata; no Task/provider activity.
        await session.deps.central_state.transaction(
            lambda tx: tx.db.execute(
                "DELETE FROM state_requests WHERE run_id=? AND kind='run_configuration'",
                (tx.run_id,),
            )
        )
        deps = replace(session.deps, component_binding=binding)
        await bind_run_metadata(deps)
        captured = AsyncMock(side_effect=RuntimeError("stop at tested assembly boundary"))
        monkeypatch.setattr(run, "build", captured)
        args = SimpleNamespace(
            run_id=str(session.runtime.run_id),
            instance=None,
            max_steps=None,
            workers=None,
            json=True,
        )
        if not collect:
            # Existing checkpoint artifact is needed for the resume preflight;
            # this test stops before the checkpoint can be opened.
            from pathlib import Path

            Path(deps.settings.checkpoint_path).touch()
        dispatch = resume._run_collect_central if collect else resume._run_central
        async with AsyncExitStack() as stack:
            assert (
                await dispatch(
                    args,
                    cli_config(
                        session,
                        LLMConfig(),
                    ),
                    mcp_stack=stack,
                )
                == 1
            )
        captured.assert_awaited_once()
        actual = captured.await_args.kwargs["original_component_binding"]
        if fingerprint or network_fingerprint:
            assert actual.get("execution_environment_fingerprint") == fingerprint
            assert actual.get("network_environment_fingerprint") == network_fingerprint
        else:
            assert actual is None
