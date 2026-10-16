"""Host + HTTP API on top of a real SessionController with fake conversation/runner."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from rich.console import Console

from redteam.app.cli.conversation import ConversationDecision
from redteam.app.cli.session_controller import SessionController
from redteam.app.cli.session_types import RunResult
from redteam.app.web.host import SessionHost
from redteam.app.web.security import CLIENT_HEADER, GuardPolicy
from redteam.app.web.server import create_app
from redteam.app.web.settings import SettingsStore

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {
    "Origin": BASE,
    "Content-Type": "application/json",
    CLIENT_HEADER: "web",
}
SECRET = "sk-test-secret-value-1234567890"
READY = {
    "model": "provider/model-1",
    "api_key": SECRET,
    "profile_id": "alpha",
    "max_requests": 50,
    "max_concurrent_requests": 2,
}


class Conversation:
    def __init__(self):
        self.mode = "answer"

    async def decide(self, text, **kwargs):
        return ConversationDecision(
            mode=self.mode,
            response="공개 답변",
            objective=text,
            success_criteria=(
                ["허용된 결과를 원본 근거와 함께 요약합니다"]
                if self.mode == "execute" else []
            ),
            guidance=text,
            investigation="new" if self.mode == "execute" else None,
        )


class Operator:
    def __init__(self, end):
        self._identity = SimpleNamespace(mission_id="mission-A")
        self.end = end
        self.calls = []

    async def session_context(self):
        return "현재 조사"

    async def dispatch(self, kind, value):
        self.calls.append((kind, value))
        if kind == "stop":
            self.end.set()
        return True, "수락됨"


class FakeLauncher:
    """Runs the real controller with a fake runner; no LLM, MCP or target."""

    def __init__(self):
        self.conversation = Conversation()
        self.end = asyncio.Event()
        self.started = asyncio.Event()
        self.operator = Operator(self.end)
        self.objectives: list[str] = []
        self.success_criteria: list[list[str] | None] = []
        self.launches = 0
        self.frontend = None
        self.verifier = None

    def build_config(self):
        return SimpleNamespace()

    async def launch(self, frontend, config):
        self.launches += 1
        self.frontend = frontend
        self.end = asyncio.Event()
        self.operator = Operator(self.end)
        self.started = asyncio.Event()

        async def runner(objective, controller):
            self.objectives.append(objective)
            receipt = controller.active_receipt
            decision = receipt.decision if receipt is not None else None
            self.success_criteria.append(
                list(decision.success_criteria)
                if isinstance(decision, ConversationDecision) else None
            )
            controller.attach(self.operator)
            controller.observe(SimpleNamespace(run_id="run-A"))
            frontend.event_sink(
                SimpleNamespace(
                    event_type="ActionProposed", event_id="evt-1", run_id="run-A",
                    timestamp=None, source="test",
                    payload={"action_id": "act-1", "operation": "http_request",
                             "api_key": "should-not-leak"},
                )
            )
            self.started.set()
            await self.end.wait()
            final = SimpleNamespace(
                run_id="run-A", phase=SimpleNamespace(value="finished"),
                termination_reason="no_actionable_plan", mission_status=None,
                actions_executed=1, recent_observations=[],
            )
            return RunResult(final)

        controller = SessionController(
            console=Console(file=io.StringIO(), width=80, color_system=None),
            frontend=frontend,
            conversation=self.conversation,
            runner=runner,
            engagement_name="test-engagement",
            scope=["localhost"],
            approval_verifier=self.verifier,
        )
        return await controller.run()


@pytest.fixture
async def stack(tmp_path):
    profiles = tmp_path / "agents"
    profiles.mkdir()
    (profiles / "alpha.yaml").write_text("profile_id: alpha\n", encoding="utf-8")
    settings = SettingsStore(
        env={}, state_path=tmp_path / "web.json", profile_dir=profiles, env_path=tmp_path / ".env"
    )
    launcher = FakeLauncher()
    host = SessionHost(
        settings=settings, launcher=launcher, engagement_name="test-engagement",
        scope=("localhost",),
    )
    app = create_app(host, GuardPolicy.for_loopback(PORT))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield SimpleNamespace(host=host, client=client, launcher=launcher, settings=settings)
    launcher.end.set()
    await host.shutdown()


async def post(client, path, body=None, method="POST"):
    return await client.request(method, path, content=json.dumps(body or {}), headers=HEADERS)


async def become_ready(s):
    await s.host.start()
    response = await post(s.client, "/api/v1/settings", READY, method="PUT")
    assert response.status_code == 200, response.text
    for _ in range(100):
        if s.host.state == "ready":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"not ready: {s.host.state}")


async def settled(s):
    """Inputs processed by the controller and observed by the web frontend."""
    controller = s.host.frontend.controller
    # ``join_inputs`` would also wait for the active Run to end; only the queues matter here.
    await asyncio.wait_for(controller.queue.join(), 2)
    await asyncio.wait_for(controller.controls.join(), 2)
    for _ in range(100):
        s.host.frontend.invalidate()
        if all(r.state not in {"queued", "classifying"} for r in controller.receipts.values()):
            return
        await asyncio.sleep(0.02)


def inputs(host):
    return [item["data"] for item in host.log.tail() if item["kind"] == "input"]


async def test_starts_in_needs_settings_and_reports_missing_fields(stack):
    await stack.host.start()
    snapshot = (await stack.client.get("/api/v1/snapshot")).json()
    assert snapshot["schema_version"] == 1
    assert snapshot["session"]["state"] == "needs_settings"
    fields = {item["field"] for item in snapshot["settings"]["problems"]}
    assert {"model", "api_key", "profile_id", "llm_request_policy"} <= fields
    assert stack.launcher.launches == 0


async def test_input_is_refused_before_settings_are_complete(stack):
    await stack.host.start()
    response = await post(stack.client, "/api/v1/input", {"text": "hello"})
    assert response.status_code == 409
    assert response.json() == {"error": "session_not_ready"}
    assert stack.launcher.launches == 0


async def test_completing_settings_starts_the_session(stack):
    await become_ready(stack)
    assert stack.launcher.launches == 1
    snapshot = (await stack.client.get("/api/v1/snapshot")).json()
    assert snapshot["session"]["state"] == "ready"
    assert snapshot["state"]["phase"] == "idle"
    assert snapshot["settings"]["ready"] is True


async def test_api_key_never_appears_in_snapshot_settings_or_log(stack):
    await become_ready(stack)
    snapshot = (await stack.client.get("/api/v1/snapshot")).text
    summary = (await stack.client.get("/api/v1/settings")).text
    assert SECRET not in snapshot and SECRET not in summary
    assert SECRET not in json.dumps(stack.host.log.tail())


async def test_question_is_answered_through_the_existing_conversation_path(stack):
    await become_ready(stack)
    body = {"text": "무엇을 찾았어?", "request_id": "r1"}
    response = await post(stack.client, "/api/v1/input", body)
    assert response.status_code == 202
    input_id = response.json()["input_id"]
    await settled(stack)
    last = [item for item in inputs(stack.host) if item["input_id"] == input_id][-1]
    assert last["state"] == "handled"
    assert last["decision"]["mode"] == "answer"
    assert last["decision"]["response"] == "공개 답변"


async def test_duplicate_client_request_id_is_not_submitted_twice(stack):
    await become_ready(stack)
    controller = stack.host.frontend.controller
    first = await post(stack.client, "/api/v1/input", {"text": "질문", "request_id": "same"})
    second = await post(stack.client, "/api/v1/input", {"text": "질문", "request_id": "same"})
    assert first.json()["input_id"] == second.json()["input_id"]
    assert second.json()["duplicate"] is True
    assert controller.sequence == 1


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [({}, 400, "text_required"), ({"text": "   "}, 400, "text_required"),
     ({"text": "x" * 12001}, 413, "text_too_long"),
     ({"text": "ok", "request_id": "bad id!"}, 400, "invalid_request_id"),
     ({"text": "ok", "binding": "nope"}, 400, "invalid_binding")],
)
async def test_input_validation(stack, body, status, code):
    await become_ready(stack)
    response = await post(stack.client, "/api/v1/input", body)
    assert (response.status_code, response.json()["error"]) == (status, code)
    assert stack.host.frontend.controller.sequence == 0


async def test_new_investigation_runs_and_runtime_events_are_masked(stack):
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "localhost 점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    events = [item for item in stack.host.log.tail() if item["kind"] == "event"]
    assert events[0]["run_id"] == "run-A"
    assert events[0]["data"]["payload"]["operation"] == "http_request"
    assert events[0]["data"]["payload"]["api_key"] == "***REDACTED***"
    assert stack.launcher.objectives == ["localhost 점검"]
    assert stack.launcher.success_criteria == [["허용된 결과를 원본 근거와 함께 요약합니다"]]
    recorded_input = [item for item in inputs(stack.host) if item["text"] == "localhost 점검"][-1]
    assert recorded_input["decision"]["success_criteria"] == [
        "허용된 결과를 원본 근거와 함께 요약합니다",
    ]


async def test_settings_cannot_change_while_a_run_is_active(stack):
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    response = await post(stack.client, "/api/v1/settings", {"max_requests": 99}, method="PUT")
    assert response.status_code == 409 and response.json() == {"error": "run_active"}
    assert json.loads(stack.settings.env["REDTEAM_LLM_REQUEST_POLICY_JSON"])["max_requests"] == 50
    assert stack.launcher.launches == 1


async def test_idle_settings_change_restarts_the_session_for_new_runs(stack):
    await become_ready(stack)
    response = await post(stack.client, "/api/v1/settings", {"max_requests": 70}, method="PUT")
    assert response.status_code == 200
    for _ in range(100):
        if stack.launcher.launches == 2 and stack.host.state == "ready":
            break
        await asyncio.sleep(0.01)
    assert stack.launcher.launches == 2
    assert json.loads(stack.settings.env["REDTEAM_LLM_REQUEST_POLICY_JSON"])["max_requests"] == 70


async def test_invalid_settings_return_field_and_code_only(stack):
    await stack.host.start()
    response = await post(
        stack.client, "/api/v1/settings", {"profile_id": "../alpha", "api_key": SECRET},
        method="PUT",
    )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_settings", "field": "profile_id",
                               "code": "unknown_profile"}
    assert SECRET not in response.text


async def test_control_commands_reach_the_operator_channel(stack):
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    response = await post(stack.client, "/api/v1/run/pause")
    assert response.status_code == 202
    await settled(stack)
    assert ("pause", "") in stack.launcher.operator.calls
    assert (await post(stack.client, "/api/v1/run/explode")).status_code == 404


async def test_approval_requires_the_pending_id_and_the_current_run(stack):
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    controller = stack.host.frontend.controller
    controller.pending_approval = "APR-1"
    before = controller.sequence

    changed = await post(stack.client, "/api/v1/approvals/APR-2/approve", {"run_id": "run-A"})
    assert changed.status_code == 409 and changed.json() == {"error": "approval_changed"}
    other_run = await post(stack.client, "/api/v1/approvals/APR-1/approve", {"run_id": "run-B"})
    assert other_run.status_code == 409 and other_run.json() == {"error": "run_mismatch"}
    assert controller.sequence == before
    assert not [call for call in stack.launcher.operator.calls if call[0] == "approve"]

    accepted = await post(stack.client, "/api/v1/approvals/APR-1/approve", {"run_id": "run-A"})
    assert accepted.status_code == 202
    await settled(stack)
    assert ("approve", "APR-1") in stack.launcher.operator.calls


async def test_each_live_parallel_approval_can_be_decided_by_exact_id(stack):
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    controller = stack.host.frontend.controller
    for approval_id in ("APR-1", "APR-2"):
        controller.observe(
            SimpleNamespace(
                run_id="run-A",
                event_type="ApprovalRequired",
                payload={"approval_id": approval_id},
            )
        )

    response = await post(
        stack.client, "/api/v1/approvals/APR-2/deny", {"run_id": "run-A"}
    )
    assert response.status_code == 202
    await settled(stack)
    assert ("deny", "APR-2") in stack.launcher.operator.calls
    assert controller.pending_approvals == ("APR-1",)


async def test_approval_route_rejects_unknown_decisions_and_bad_ids(stack):
    await become_ready(stack)
    unknown = await post(stack.client, "/api/v1/approvals/APR-1/maybe", {"run_id": "r"})
    assert unknown.status_code == 404
    bad = await post(stack.client, "/api/v1/approvals/..%2Fx/approve", {"run_id": "r"})
    assert bad.status_code in {404, 400}
    missing = await post(stack.client, "/api/v1/approvals/APR-1/approve", {})
    assert missing.status_code == 400


async def test_writes_without_the_page_proofs_are_refused_by_the_guard(stack):
    await stack.host.start()
    response = await stack.client.post("/api/v1/input", content=b'{"text":"x"}')
    assert response.status_code == 403
    foreign = await stack.client.get("/api/v1/snapshot", headers={"Host": "evil.example"})
    assert foreign.status_code == 403


async def test_session_ended_by_the_user_is_reported_and_can_restart(stack):
    await become_ready(stack)
    await post(stack.client, "/api/v1/input", {"text": "/exit"})
    for _ in range(200):
        if stack.host.state == "stopped":
            break
        await asyncio.sleep(0.01)
    assert stack.host.state == "stopped"
    restarted = await post(stack.client, "/api/v1/session/restart")
    assert restarted.status_code == 202
    for _ in range(200):
        if stack.host.state == "ready":
            break
        await asyncio.sleep(0.01)
    assert stack.host.state == "ready"
    assert stack.launcher.launches == 2


async def test_approval_already_resolved_in_the_ledger_changes_nothing(stack):
    async def verifier(approval_id, run_id):
        return False  # the stored approval is no longer pending for this Run

    stack.launcher.verifier = verifier
    await become_ready(stack)
    stack.launcher.conversation.mode = "execute"
    await post(stack.client, "/api/v1/input", {"text": "점검"})
    await asyncio.wait_for(stack.launcher.started.wait(), 2)
    controller = stack.host.frontend.controller
    controller.pending_approval = "APR-1"
    response = await post(stack.client, "/api/v1/approvals/APR-1/approve", {"run_id": "run-A"})
    assert response.status_code == 202
    await settled(stack)
    assert not [call for call in stack.launcher.operator.calls if call[0] == "approve"]
    receipt = [i for i in inputs(stack.host) if i["text"] == "approve APR-1"][-1]
    assert receipt["state"] == "not_applied"


async def test_input_while_the_session_is_closing_is_refused_not_lost(stack):
    await become_ready(stack)
    stack.host.frontend.controller.request_close()
    response = await post(stack.client, "/api/v1/input", {"text": "늦은 입력"})
    assert response.status_code == 429
    assert response.json() == {"error": "input_not_accepted"}


async def test_input_after_server_shutdown_is_a_state_conflict(stack):
    await become_ready(stack)
    await stack.host.shutdown()
    response = await post(stack.client, "/api/v1/input", {"text": "x"})
    assert response.status_code == 409
    assert stack.host.state == "closed"
