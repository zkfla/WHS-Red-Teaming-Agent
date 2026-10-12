"""Explicit existing-original collection, not resume or claim handoff."""

from __future__ import annotations

import argparse
from contextlib import AsyncExitStack

from redteam.app.agent_profile import selected_specialists
from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    print_json,
)
from redteam.app.cli.commands.resume import build_resume_application
from redteam.app.runtime_components import binding_for_resume
from redteam.capability.settings import MCPConfigurationError
from redteam.orchestration.checkpointing import CheckpointThreadIdentity, list_checkpoint_threads
from redteam.orchestration.component_binding import ComponentRegistry, selection_of
from redteam.orchestration.coordination import CoordinationError, StateConflict
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_activation import (
    ActivationCollectionIdentity,
    read_activation_collection_identity,
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "collect-originals", help="Collect existing originals only; never resume or release claims",
    )
    parser.add_argument("run_id")
    parser.add_argument("engagement_path")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=run)


def _reason(exc: Exception) -> str:
    # Only the coordinator's code-owned rejection vocabulary may include
    # its message. Configuration/provider/storage exceptions are class-only.
    return str(exc) if isinstance(exc, (CoordinationError, StateConflict)) else type(exc).__name__


async def _inventory(
    settings: OrchestrationSettings, run_id: str,
) -> tuple[list[CheckpointThreadIdentity], set[str], ActivationCollectionIdentity | None]:
    threads = await list_checkpoint_threads(
        enabled=settings.checkpointing, backend=settings.checkpoint_backend,
        path=settings.checkpoint_path, run_id=run_id,
    )
    if any(thread.instance_id is None for thread in threads):
        raise StateConflict("execution_collection_requires_instance_cohort")
    instances = {str(thread.instance_id) for thread in threads}
    hint = await read_activation_collection_identity(
        settings.task_graph_path, run_id, checkpoint_instances=instances,
    )
    if hint is not None and any(
        thread.engagement_id != hint.engagement_id or thread.mission_id != hint.mission_id
        or thread.component_binding != hint.component_binding for thread in threads
    ):
        raise StateConflict("activation_collection_cohort_mismatch")
    return threads, instances, hint


async def run(
    args: argparse.Namespace, *, component_registry: ComponentRegistry | None = None,
) -> int:
    try:
        config = load_app_config_for_run(args.engagement_path)
        settings = OrchestrationSettings.for_durable_run(config.orchestration)
    except Exception as exc:  # noqa: BLE001 - configuration may contain secrets
        eprint(f"ERROR: original collection configuration unavailable: {type(exc).__name__}")
        return EXIT_CONFIG_ERROR

    try:
        before_threads, instances, hint = await _inventory(settings, args.run_id)
        if hint is None:
            eprint("ERROR: no proved entered/checkpointless actor; nothing collected. "
                   "Resume and claim release are not authorized.")
            return EXIT_FAILURE
        specialists = selected_specialists(settings)
        if (specialists is None
                or specialists.fingerprint != hint.component_binding.specialist_fingerprint
                or settings.llm_request_policy != hint.llm_request_policy):
            raise StateConflict("activation_configuration_changed_new_run_required")
        from redteam.llm.opencode_go_config import llm_connection_fingerprint_for_config

        binding = binding_for_resume(
            hint.component_binding, registry=component_registry,
            llm_connection_fingerprint=llm_connection_fingerprint_for_config(config.llm),
        )
        config = config.model_copy(update={
            "extensions": list(binding.extension_entry_points),
            "planning_provider": binding.planning_provider_id,
        })
    except Exception as exc:  # noqa: BLE001 - no raw records/handles/exception text
        eprint(f"ERROR: original collection preflight refused: {_reason(exc)}")
        return EXIT_FAILURE

    try:
        stack = AsyncExitStack()
        resource_error: BaseException | None = None
        try:
            _, deps = await build_resume_application(
                config, engagement_id=hint.engagement_id, settings=settings, mcp_stack=stack,
                component_profile=selection_of(binding), component_registry=component_registry,
            )
            driver = InstanceDriver(deps, settings=settings)
            body_error: BaseException | None = None
            try:
                await driver.open()
                await driver.own_run(args.run_id)
                # The first read is not an ownership proof. Check again under
                # ownership before any query; never bootstrap a fresh actor.
                threads, current_instances, current_hint = await _inventory(settings, args.run_id)
                if (threads != before_threads or current_instances != instances
                        or current_hint != hint):
                    raise StateConflict("activation_collection_inventory_changed")
                results = await driver.collect_checkpointless_originals(
                    args.run_id, checkpoint_instances=instances,
                )
            except BaseException as exc:  # noqa: BLE001 - cleanup must preserve cancellation
                body_error = exc
                raise
            finally:
                try:
                    await driver.close()
                except Exception as exc:  # noqa: BLE001 - don't replace the original body error
                    if body_error is None:
                        raise
                    eprint(f"ERROR: original collection teardown incomplete: {type(exc).__name__}")
        except BaseException as exc:  # noqa: BLE001 - also preserve errors across transport cleanup
            resource_error = exc
            raise
        finally:
            try:
                await stack.aclose()
            except Exception as exc:  # noqa: BLE001 - cleanup is not a substitute for cancellation
                if resource_error is None:
                    raise
                eprint(f"ERROR: original collection transport cleanup incomplete: "
                       f"{type(exc).__name__}")
    except MCPConfigurationError as exc:
        eprint(f"ERROR: original collection MCP configuration invalid: {type(exc).__name__}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - cancellation propagates after owned cleanup
        eprint(f"ERROR: original collection refused: {_reason(exc)}")
        return EXIT_FAILURE

    collected = bool(results) and all(
        result.originals and all(original.state == "known" for original in result.originals)
        for result in results
    )
    if args.json:
        print_json({
            "schema_version": 1, "run_id": args.run_id,
            "outcome": "collected" if collected else "held",
            "replay_permitted": False, "claim_release_permitted": False,
            "instances": [result.model_dump(mode="json") for result in results],
        })
    else:
        print(f"Original collection for Run {args.run_id}: {'collected' if collected else 'held'}")
        for result in results:
            print(f"  instance={result.instance_id}; originals={len(result.originals)}")
            for original in result.originals:
                print(f"    {original.invocation_id}: {original.state}, "
                      f"reason={original.reason_code}, receipt={original.original_receipt_id}")
        print("Originals only: no resume, Task completion or claim release. "
              "Empty inventory does not prove non-entry.")
    return EXIT_OK if collected else EXIT_FAILURE
