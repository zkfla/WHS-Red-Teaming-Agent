"""Focused 04C regressions: interrupted opening abort and trusted recovery."""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_window import (
    GatedNetworkError,
    GatedWindowExtension,
    GatedWindowRecovery,
    opaque_window_tag,
    workroom_key_for,
)
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
    WindowState,
)
from redteam.core.ports.network_grants import OutstandingNetworkWindow

IMAGE_ID = "sha256:" + "ab" * 32
CAPS = WindowCaps(
    max_flow_init_deliveries=1,
    max_output_bytes=10,
    delivery_rate_per_second=None,
    duration_seconds=1.0,
    purpose=NetworkPurpose.TARGET,
)
GRANT = SimpleNamespace(
    window_id="gw-1", epoch=1, reserved_bytes=4096, run_id="run", instance_id="inst"
)


def _policy() -> GatedNetworkPolicy:
    cap = NetworkCapacity(max_flow_init_deliveries=10, max_output_bytes=100000)
    zero = NetworkCapacity(max_flow_init_deliveries=0, max_output_bytes=0)
    budget = NetworkBudget(global_capacity=cap, target_capacity=cap, install_capacity=zero)
    target = WindowCaps(
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        delivery_rate_per_second=None,
        duration_seconds=60.0,
        purpose=NetworkPurpose.TARGET,
    )
    install = WindowCaps(
        max_flow_init_deliveries=2,
        max_output_bytes=2048,
        delivery_rate_per_second=None,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        sidecar_image=IMAGE_ID,
        budget=budget,
        default_target_caps=target,
        default_install_caps=install,
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={}),
        install_proxy=InstallProxyConfig(
            endpoint="http://proxy:8080",
            pinned_hosts=("10.0.0.10",),
            mirror_upstream="https://m:443",
        ),
    )


class FakeLedger:
    def __init__(self, rows: tuple[Any, ...] = (), *, request_grant: Any = GRANT) -> None:
        self.rows = rows
        self.request_grant = request_grant
        self.settles: list[tuple[str, Any, bool]] = []
        self.releases: list[tuple[str, bool]] = []
        self.settle_error: Exception | None = None
        self.settle_delay = 0.0
        self.release_result = True

    async def outstanding_windows(self, *, run_id: str, instance_id: str) -> tuple[Any, ...]:
        return self.rows

    async def request_window(self, **kwargs: Any) -> Any:
        return self.request_grant

    async def settle_window(self, *, window_id: str, meter: Any, verified_closed: bool) -> Any:
        self.settles.append((window_id, meter, verified_closed))
        if self.settle_delay:
            await asyncio.sleep(self.settle_delay)
        if self.settle_error is not None:
            raise self.settle_error
        return SimpleNamespace(window_id=window_id, state=WindowState.QUARANTINED)

    async def release_quarantine(self, *, window_id: str, verified_closed: bool) -> bool:
        self.releases.append((window_id, verified_closed))
        return self.release_result


class FakeManager:
    def __init__(self, endpoint_state: str = "endpoint_verified", room: str = "absent") -> None:
        self.endpoint_state = endpoint_state
        self.room = room

    async def revalidate_bound_endpoint(self, endpoint: Any) -> str:
        return self.endpoint_state

    async def observe_room_for_recovery(self, key: str) -> str:
        return self.room

    async def ensure_workroom(self, key: str) -> str:
        return "room-container"


class FakeDocker:
    """Argv-keyed runner. Handlers may return a result tuple or be awaitable."""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self, argv: tuple[str, ...], stdin: Any, timeout: float, limit: Any = None
    ) -> Any:
        self.calls.append(argv)
        handler = self.handlers.get(argv[0])
        if handler is None:
            raise AssertionError("unexpected docker argv: " + repr(argv))
        result = handler(argv)
        if inspect.isawaitable(result):
            result = await result
        return result


def _ok(text: str = "") -> Any:
    return lambda argv: (0, text.encode(), b"", False, False)


def _missing(argv: tuple[str, ...]) -> Any:
    return (1, b"", b"No such object", False, False)


def _image(argv: tuple[str, ...]) -> Any:
    return (0, (IMAGE_ID + "\n").encode(), b"", False, False)


def _lines(names: tuple[str, ...], field: str) -> bytes:
    return "".join(json.dumps({field: name}) + "\n" for name in names).encode()


def _extension(ledger: FakeLedger, docker: FakeDocker, manager: Any = None) -> GatedWindowExtension:
    return GatedWindowExtension(
        manager=manager or FakeManager(), ledger=ledger, run_docker=docker, policy=_policy()
    )


def _open_kwargs() -> dict[str, Any]:
    return dict(
        run_id="run",
        instance_id="inst",
        epoch=1,
        caps=CAPS,
        allowed=(),
        holder_action_id="act_1",
        holder_invocation_id="inv_1",
    )


KEY = workroom_key_for("run", "inst")
TAG = opaque_window_tag("run", "inst", 1)
NET = TAG.replace("gwin-", "gwn-", 1)


def test_cancel_during_sidecar_create_settles_same_id_and_clears_handle() -> None:
    async def go() -> None:
        ledger = FakeLedger()

        async def create(argv: tuple[str, ...]) -> Any:
            raise asyncio.CancelledError()

        docker = FakeDocker({"image": _image, "create": create, "inspect": _missing, "rm": _ok()})
        ext = _extension(ledger, docker)
        with pytest.raises(asyncio.CancelledError):
            await ext.open_window(**_open_kwargs())
        assert ledger.settles == [("gw-1", None, False)]
        assert not ext._held(KEY)
        assert KEY not in ext._held_aborts

    asyncio.run(go())


def test_original_error_survives_failed_cleanup_and_retry_releases_same_id() -> None:
    async def go() -> None:
        ledger = FakeLedger()
        rm_fails = {"on": True}

        def rm(argv: tuple[str, ...]) -> Any:
            if rm_fails["on"]:
                raise OSError("docker unreachable")
            return (0, b"", b"", False, False)

        docker = FakeDocker(
            {
                "image": _image,
                "create": lambda argv: (1, b"", b"Conflict. name in use", False, False),
                "inspect": _missing,
                "rm": rm,
            }
        )
        ext = _extension(ledger, docker)
        with pytest.raises(GatedNetworkError, match="sidecar_collision"):
            await ext.open_window(**_open_kwargs())
        assert ledger.settles == [("gw-1", None, False)]
        assert ext._held(KEY)
        held = ext._held_aborts[KEY]
        assert held[0] == TAG and held[1] == NET and held[3] is GRANT
        rm_fails["on"] = False
        assert await ext.retry_abort(KEY) is True
        assert len(ledger.settles) == 2 and ledger.settles[-1] == ("gw-1", None, False)
        assert not ext._held(KEY) and KEY not in ext._held_aborts

    asyncio.run(go())


def test_failed_settlement_keeps_handle_and_retry_settles_same_id() -> None:
    async def go() -> None:
        ledger = FakeLedger()
        ledger.settle_error = OSError("ledger busy")

        async def create(argv: tuple[str, ...]) -> Any:
            raise asyncio.CancelledError()

        docker = FakeDocker({"image": _image, "create": create, "inspect": _missing, "rm": _ok()})
        ext = _extension(ledger, docker)
        with pytest.raises(asyncio.CancelledError):
            await ext.open_window(**_open_kwargs())
        assert ext._held(KEY) and KEY in ext._held_aborts
        ledger.settle_error = None
        assert await ext.retry_abort(KEY) is True
        assert [entry[0] for entry in ledger.settles] == ["gw-1", "gw-1"]
        assert not ext._held(KEY)

    asyncio.run(go())


def test_outer_cancel_repeated_during_abort_still_completes_settlement() -> None:
    async def go() -> None:
        ledger = FakeLedger()
        ledger.settle_delay = 0.05
        started = asyncio.Event()

        async def create(argv: tuple[str, ...]) -> Any:
            started.set()
            await asyncio.sleep(10)

        docker = FakeDocker({"image": _image, "create": create, "inspect": _missing, "rm": _ok()})
        ext = _extension(ledger, docker)
        task = asyncio.ensure_future(ext.open_window(**_open_kwargs()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ledger.settles == [("gw-1", None, False)]
        assert not ext._held(KEY)

    asyncio.run(go())


def _row(
    *, state: WindowState = WindowState.OPEN, settlement: Any = None
) -> OutstandingNetworkWindow:
    return OutstandingNetworkWindow(
        grant=SimpleNamespace(window_id="gw-1", epoch=1),
        state=state,
        settlement=settlement,
    )


def _inventory(
    *,
    sidecars: tuple[str, ...] = (),
    exact: tuple[str, ...] = (),
    networks: tuple[str, ...] = (),
    exact_networks: tuple[str, ...] = (),
    fail_label_ps: bool = False,
) -> Any:
    def handler(argv: tuple[str, ...]) -> Any:
        flt = argv[argv.index("--filter") + 1]
        if argv[0] == "ps":
            if flt.startswith("label="):
                if fail_label_ps:
                    return (1, b"", b"daemon error", False, False)
                return (0, _lines(sidecars, "Names"), b"", False, False)
            return (0, _lines(exact, "Names"), b"", False, False)
        if flt.startswith("label="):
            return (0, _lines(networks, "Name"), b"", False, False)
        return (0, _lines(exact_networks, "Name"), b"", False, False)

    return handler


def _recover(ledger: FakeLedger, docker: FakeDocker, manager: FakeManager, row: Any) -> Any:
    ledger.rows = (row,)
    ext = _extension(ledger, docker, manager)

    async def go() -> Any:
        return await GatedWindowRecovery(ext).reconcile_instance(
            run_id="run", instance_id="inst", endpoint=None
        )

    return asyncio.run(go()), ext


@pytest.mark.parametrize("room", ["absent", "baseline"])
def test_verified_closure_releases_quarantine_after_same_id_unknown_settlement(room: str) -> None:
    ledger = FakeLedger()
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    report, _ext = _recover(ledger, docker, FakeManager(room=room), _row())
    assert ledger.settles == [("gw-1", None, False)]
    assert ledger.releases == [("gw-1", True)]
    outcome = report.windows[0]
    assert outcome.closure_verified and outcome.quarantine_released
    assert outcome.reason_code is None


def test_unbound_endpoint_settles_unknown_but_never_releases_or_inspects() -> None:
    ledger = FakeLedger()
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    report, _ext = _recover(ledger, docker, FakeManager("endpoint_unbound"), _row())
    assert ledger.settles == [("gw-1", None, False)]
    assert ledger.releases == []
    assert docker.calls == []
    assert report.windows[0].reason_code == "endpoint_unbound"
    assert not report.windows[0].closure_verified


@pytest.mark.parametrize(
    "label_kwargs, net_kwargs, reason",
    [
        (dict(sidecars=(TAG,)), {}, "sidecar_present"),
        (dict(exact=(TAG,)), {}, "sidecar_present"),
        ({}, dict(networks=(NET,)), "network_present"),
        ({}, dict(exact_networks=(NET,)), "network_present"),
        (dict(fail_label_ps=True), {}, "sidecar_inventory_unverified"),
    ],
)
def test_present_or_unproven_resources_keep_quarantine(
    label_kwargs: dict[str, Any], net_kwargs: dict[str, Any], reason: str
) -> None:
    ledger = FakeLedger()
    docker = FakeDocker({"ps": _inventory(**label_kwargs), "network": _inventory(**net_kwargs)})
    report, _ext = _recover(ledger, docker, FakeManager(), _row())
    assert ledger.releases == []
    assert report.windows[0].reason_code == reason
    assert not report.windows[0].quarantine_released


def test_unowned_or_busy_room_keeps_quarantine() -> None:
    ledger = FakeLedger()
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    report, _ext = _recover(ledger, docker, FakeManager(room="unverified"), _row())
    assert ledger.releases == []
    assert report.windows[0].reason_code == "room_unverified"


def test_settlement_failure_is_reported_without_release() -> None:
    ledger = FakeLedger()
    ledger.settle_error = OSError("ledger down")
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    report, _ext = _recover(ledger, docker, FakeManager(), _row())
    assert ledger.releases == []
    assert report.windows[0].reason_code == "settlement_failed"
    assert report.windows[0].settlement is None


def test_quarantined_window_with_settlement_is_not_resettled() -> None:
    ledger = FakeLedger()
    stored = SimpleNamespace(window_id="gw-1", state=WindowState.QUARANTINED)
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    report, _ext = _recover(
        ledger, docker, FakeManager(), _row(state=WindowState.QUARANTINED, settlement=stored)
    )
    assert ledger.settles == []
    assert ledger.releases == [("gw-1", True)]
    assert report.windows[0].settlement is stored


def test_in_process_held_window_is_left_alone() -> None:
    ledger = FakeLedger()
    docker = FakeDocker({"ps": _inventory(), "network": _inventory()})
    ledger.rows = (_row(),)
    ext = _extension(ledger, docker, FakeManager())
    ext._opening.add(KEY)

    async def go() -> Any:
        return await GatedWindowRecovery(ext).reconcile_instance(
            run_id="run", instance_id="inst", endpoint=None
        )

    report = asyncio.run(go())
    assert ledger.settles == [] and ledger.releases == []
    assert report.windows[0].reason_code == "held_in_process"


class CancelAfterReserve(FakeManager):
    async def ensure_workroom(self, key: str) -> str:
        raise asyncio.CancelledError()


def test_cancel_after_reservation_commit_settles_the_granted_identity() -> None:
    async def go() -> None:
        ledger = FakeLedger()
        docker = FakeDocker({"image": _image, "inspect": _missing, "rm": _ok()})
        ext = _extension(ledger, docker, CancelAfterReserve())
        with pytest.raises(asyncio.CancelledError):
            await ext.open_window(**_open_kwargs())
        assert ledger.settles == [("gw-1", None, False)]
        assert not ext._held(KEY)

    asyncio.run(go())


def test_hanging_cleanup_and_ledger_hit_total_budget_and_keep_identity_held(monkeypatch) -> None:
    import redteam.capability.gated_window as gated_window

    monkeypatch.setattr(gated_window, "_ABORT_BUDGET_SECONDS", 0.2)
    monkeypatch.setattr(gated_window, "_SETTLE_STEP_SECONDS", 0.05)

    async def go() -> None:
        ledger = FakeLedger()
        ledger.settle_delay = 3600.0

        async def create(argv: tuple[str, ...]) -> Any:
            raise asyncio.CancelledError()

        async def rm(argv: tuple[str, ...]) -> Any:
            await asyncio.Event().wait()

        docker = FakeDocker({"image": _image, "create": create, "inspect": _missing, "rm": rm})
        ext = _extension(ledger, docker)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(asyncio.CancelledError):
            await ext.open_window(**_open_kwargs())
        assert loop.time() - started < 5.0
        assert ext._held(KEY) and KEY in ext._held_aborts
        held = ext._held_aborts[KEY]
        assert held[0] == TAG and held[3] is GRANT
        assert ledger.releases == []
        assert ledger.settles == []

    asyncio.run(go())


def test_hanging_settlement_is_bounded_and_retry_after_recovery_settles_same_id(
    monkeypatch,
) -> None:
    import redteam.capability.gated_window as gated_window

    monkeypatch.setattr(gated_window, "_SETTLE_STEP_SECONDS", 0.05)

    async def go() -> None:
        ledger = FakeLedger()
        ledger.settle_delay = 3600.0

        async def create(argv: tuple[str, ...]) -> Any:
            raise asyncio.CancelledError()

        docker = FakeDocker({"image": _image, "create": create, "inspect": _missing, "rm": _ok()})
        ext = _extension(ledger, docker)
        with pytest.raises(asyncio.CancelledError):
            await ext.open_window(**_open_kwargs())
        assert ext._held(KEY)
        ledger.settle_delay = 0.0
        assert await ext.retry_abort(KEY) is True
        assert [entry[0] for entry in ledger.settles][-1] == "gw-1"
        assert not ext._held(KEY)

    asyncio.run(go())
