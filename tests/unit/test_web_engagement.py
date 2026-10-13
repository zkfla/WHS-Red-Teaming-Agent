"""Engagement interview: strict draft parsing, confirmations, safe edits and host restart."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from rich.console import Console

from redteam.app.cli.conversation import ConversationDecision
from redteam.app.cli.engagement_form import merge_edit
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult
from redteam.app.web.engagement import EngagementError, EngagementStore, parse_draft
from redteam.app.web.engagement_catalog import EngagementCatalog
from redteam.app.web.host import SessionHost
from redteam.app.web.security import CLIENT_HEADER, GuardPolicy
from redteam.app.web.server import create_app
from redteam.app.web.settings import (
    ENV_MODEL,
    ENV_POLICY,
    ENV_PROFILE,
    ENV_PROVIDER,
    SettingsStore,
)
from redteam.core.config import load_engagement_config
from redteam.intake.serializer import (
    _INTAKE_SOURCE_TAG,
    draft_to_engagement_config,
    engagement_config_to_draft,
)

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Origin": BASE, "Content-Type": "application/json", CLIENT_HEADER: "web"}
DRAFT = {
    "name": "lab",
    "description": "authorized lab at http://localhost:8081",
    "objective": "find xss",
    "allowed_hosts": ["localhost:8081"],
    "allowed_capabilities": ["web"],
    "denied_operations": ["not-an-operation"],
    "max_actions": 8,
}


class Conversation:
    async def decide(self, text, **kwargs):
        return ConversationDecision(mode="answer", response="ok")


class Launcher:
    def __init__(self):
        self.launches = 0
        self.stop = asyncio.Event()

    def build_config(self):
        return SimpleNamespace()

    async def launch(self, frontend, config):
        self.launches += 1

        async def runner(objective, controller):
            return RunResult()

        controller = SessionController(
            console=Console(file=io.StringIO(), width=80, color_system=None),
            frontend=frontend, conversation=Conversation(), runner=runner,
            engagement_name="x", scope=["x"],
        )
        return await controller.run()


def make_store(tmp_path):
    return EngagementStore(tmp_path / "engagement.yaml")


def test_unknown_and_mistyped_fields_are_rejected_with_codes_only():
    with pytest.raises(EngagementError) as raised:
        parse_draft({"name": 3, "evil": 1, "allowed_capabilities": ["root"], "max_actions": True})
    codes = {(p["field"], p["code"]) for p in raised.value.problems}
    assert codes == {
        ("name", "invalid_type"), ("evil", "unknown_field"),
        ("allowed_capabilities", "unknown_capability"), ("max_actions", "invalid_type"),
    }


def test_hosts_are_normalized_and_bad_addresses_rejected():
    assert parse_draft({"allowed_hosts": ["http://localhost:8081/path"]}).allowed_hosts
    with pytest.raises(EngagementError) as raised:
        parse_draft({"allowed_hosts": ["not a host!!"]})
    assert raised.value.problems == [{"field": "allowed_hosts", "code": "invalid_address"}]


def test_new_file_needs_scope_and_budget(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(EngagementError) as raised:
        store.preview({"name": "x"})
    fields = {p["field"] for p in raised.value.problems}
    assert {"allowed_hosts", "max_actions"} <= fields
    assert not store.path.exists()


def test_save_requires_explicit_scope_confirmation_and_writes_a_loadable_file(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(EngagementError) as raised:
        store.save(DRAFT, confirm_scope=False, confirm_expansion=False)
    assert raised.value.code == "confirmation_required" and not store.path.exists()
    result = store.save(DRAFT, confirm_scope=True, confirm_expansion=False)
    assert result["saved"] and result["unenforced_operations"] == ["not-an-operation"]
    loaded = load_engagement_config(store.path)
    assert loaded.scope.allowed_hosts and loaded.execution_budget.max_actions == 8
    assert store.ready() and store.status()["editable"] is True
    assert not list(tmp_path.glob(".engagement-*"))


def test_widening_scope_needs_its_own_confirmation_and_narrowing_does_not(tmp_path):
    store = make_store(tmp_path)
    store.save(DRAFT, confirm_scope=True, confirm_expansion=False)
    wider = {**DRAFT, "allowed_hosts": ["localhost:8081", "localhost:9000"]}
    assert store.preview(wider)["scope_expanded"] is True
    before = store.path.read_text()
    with pytest.raises(EngagementError) as raised:
        store.save(wider, confirm_scope=True, confirm_expansion=False)
    assert raised.value.field == "confirm_expansion" and store.path.read_text() == before
    store.save(wider, confirm_scope=True, confirm_expansion=True)
    narrower = {**DRAFT}
    assert store.preview(narrower)["scope_expanded"] is False
    store.save(narrower, confirm_scope=True, confirm_expansion=False)
    assert len(load_engagement_config(store.path).scope.allowed_hosts) == 1


def test_hand_authored_rules_make_the_file_not_editable_and_are_never_overwritten(tmp_path):
    path = tmp_path / "engagement.yaml"
    path.write_text(
        yaml.safe_dump({
            "name": "hand", "description": "d",
            "scope": {"allowed_hosts": ["localhost"]},
            "rules_of_engagement": [{"name": "a"}, {"name": "b"}],
            "execution_budget": {"max_actions": 3},
        }),
        encoding="utf-8",
    )
    store = EngagementStore(path)
    status = store.status()
    assert status["editable"] is False and status["reason"] == "not_managed" and status["valid"]
    before = path.read_text()
    with pytest.raises(EngagementError) as raised:
        store.save(DRAFT, confirm_scope=True, confirm_expansion=True)
    assert raised.value.code == "not_managed" and path.read_text() == before


def test_edit_keeps_unrelated_fields_and_handles_files_without_a_rule(tmp_path):
    path = tmp_path / "engagement.yaml"
    path.write_text(
        yaml.safe_dump({
            "name": "plain", "description": "d",
            "scope": {"allowed_hosts": ["localhost"]},
            "execution_budget": {"max_actions": 3},
            "rate_limit_policy": {"max_actions_per_minute": 7},
            "metadata": {"owner": "pm", "source": _INTAKE_SOURCE_TAG},
        }),
        encoding="utf-8",
    )
    existing = load_engagement_config(path)
    draft = engagement_config_to_draft(existing).model_copy(update={"notes": ["careful"]})
    merged = merge_edit(existing, draft_to_engagement_config(draft).engagement_config, draft)
    assert merged.rate_limit_policy.max_actions_per_minute == 7
    assert merged.metadata["owner"] == "pm"
    assert merged.rules_of_engagement[0].notes == "careful"


def test_invalid_existing_file_is_reported_not_overwritten(tmp_path):
    path = tmp_path / "engagement.yaml"
    path.write_text("name: [broken", encoding="utf-8")
    store = EngagementStore(path)
    assert store.status()["reason"] == "unreadable" and not store.ready()
    with pytest.raises(EngagementError) as raised:
        store.save(DRAFT, confirm_scope=True, confirm_expansion=True)
    assert raised.value.code == "unreadable" and path.read_text() == "name: [broken"


@pytest.fixture
async def stack(tmp_path):
    profiles = tmp_path / "agents"
    profiles.mkdir()
    (profiles / "alpha.yaml").write_text("profile_id: alpha\n", encoding="utf-8")
    env = {
        ENV_PROVIDER: "litellm", ENV_MODEL: "x/model", "LLM_API_KEY": "sk-test-not-real",
        ENV_PROFILE: "alpha",
        ENV_POLICY: json.dumps(
            {"mode": "bounded", "max_requests": 5, "max_concurrent_requests": 1}
        ),
    }
    settings = SettingsStore(
        env=env, state_path=tmp_path / "web.json", profile_dir=profiles, env_path=tmp_path / ".env"
    )
    launcher = Launcher()
    catalog = EngagementCatalog(tmp_path / "engagements")
    host = SessionHost(settings=settings, launcher=launcher, catalog=catalog)
    app = create_app(host, GuardPolicy.for_loopback(PORT))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield SimpleNamespace(host=host, client=client, launcher=launcher, catalog=catalog)
    await host.shutdown()


async def call(client, method, path, body=None):
    return await client.request(method, path, content=json.dumps(body or {}), headers=HEADERS)


async def wait_state(host, state):
    for _ in range(200):
        if host.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"state {host.state} != {state}")


async def create(stack, draft, **extra):
    return await call(
        stack.client, "PUT", "/api/v1/engagement",
        {"draft": draft, "confirm_scope": True, **extra},
    )


async def test_nothing_is_active_until_an_investigation_is_created_or_selected(stack):
    await stack.host.start()
    assert stack.host.state == "needs_engagement" and stack.launcher.launches == 0
    snapshot = (await stack.client.get("/api/v1/snapshot")).json()
    assert snapshot["engagement"] == {"active": None, "status": None, "items": []}
    form = (await stack.client.get("/api/v1/engagement")).json()
    assert [s["id"] for s in form["steps"]] == [
        "basic", "scope", "permissions", "limits", "budget",
    ]
    assert form["status"]["exists"] is False

    refused = await call(
        stack.client, "PUT", "/api/v1/engagement", {"draft": DRAFT, "confirm_scope": False}
    )
    assert refused.status_code == 422 and refused.json()["error"] == "confirmation_required"
    assert not stack.catalog.directory.exists() or not list(stack.catalog.directory.glob("*"))

    saved = await create(stack, DRAFT)
    assert saved.status_code == 200 and saved.json()["id"] == "lab"
    await wait_state(stack.host, "ready")
    assert stack.host.active_id == "lab" and stack.host.scope == ("localhost:8081",)
    assert (stack.catalog.directory / "lab.yaml").is_file()


async def test_each_investigation_has_its_own_file_and_selection_is_explicit(stack):
    await stack.host.start()
    await create(stack, DRAFT)
    await wait_state(stack.host, "ready")
    other = {**DRAFT, "allowed_hosts": ["localhost:9000"]}
    second = await create(stack, other)  # same name: id must not collide
    assert second.json()["id"] == "lab-2"
    await wait_state(stack.host, "ready")
    assert stack.host.active_id == "lab-2" and stack.launcher.launches == 2
    first = load_engagement_config(stack.catalog.directory / "lab.yaml")
    assert first.scope.allowed_hosts != load_engagement_config(
        stack.catalog.directory / "lab-2.yaml"
    ).scope.allowed_hosts

    listed = (await stack.client.get("/api/v1/engagements")).json()
    assert listed["active"] == "lab-2" and {i["id"] for i in listed["items"]} == {"lab", "lab-2"}

    switched = await call(stack.client, "POST", "/api/v1/engagement/activate", {"id": "lab"})
    assert switched.status_code == 202
    await wait_state(stack.host, "ready")
    assert stack.host.active_id == "lab" and stack.launcher.launches == 3
    assert stack.host.scope == ("localhost:8081",)


async def test_editing_another_investigation_does_not_restart_the_session(stack):
    await stack.host.start()
    await create(stack, DRAFT)
    await wait_state(stack.host, "ready")
    second = {**DRAFT, "name": "second", "allowed_hosts": ["localhost:9000"]}
    assert (await create(stack, second)).status_code == 200
    await wait_state(stack.host, "ready")
    launches = stack.launcher.launches
    edited = {**DRAFT, "objective": "changed goal"}
    response = await call(
        stack.client, "PUT", "/api/v1/engagement",
        {"id": "lab", "draft": edited, "confirm_scope": True},
    )
    assert response.status_code == 200 and stack.host.active_id == "second"
    assert stack.launcher.launches == launches


async def test_unknown_or_malformed_ids_never_touch_files_outside_the_directory(stack):
    await stack.host.start()
    for bad in ("../x", "a/b", "NOPE", "missing"):
        activate = await call(stack.client, "POST", "/api/v1/engagement/activate", {"id": bad})
        assert activate.status_code == 404, bad
        save = await call(
            stack.client, "PUT", "/api/v1/engagement",
            {"id": bad, "draft": DRAFT, "confirm_scope": True},
        )
        assert save.status_code == 404, bad
    assert (await stack.client.get("/api/v1/engagement?id=..%2Fx")).status_code == 404


async def test_invalid_draft_returns_problem_codes(stack):
    await stack.host.start()
    response = await call(stack.client, "POST", "/api/v1/engagement/preview", {"draft": {"x": 1}})
    assert response.status_code == 422
    assert response.json() == {
        "error": "invalid_engagement", "problems": [{"field": "x", "code": "unknown_field"}],
    }


async def test_active_investigation_cannot_be_replaced_while_a_run_is_pending(stack):
    await stack.host.start()
    await create(stack, DRAFT)
    await wait_state(stack.host, "ready")
    controller = stack.host.frontend.controller
    controller.deferred.append(object())
    try:
        response = await create(stack, {**DRAFT, "name": "late"})
        assert response.status_code == 409 and response.json() == {"error": "run_active"}
        assert not (stack.catalog.directory / "late.yaml").exists()
    finally:
        controller.deferred.clear()


async def test_guard_blocks_cross_origin_engagement_writes(stack):
    await stack.host.start()
    forged = await stack.client.request(
        "PUT", "/api/v1/engagement", content=json.dumps({"draft": DRAFT, "confirm_scope": True}),
        headers={**HEADERS, "Origin": "http://evil.example"},
    )
    assert forged.status_code == 403
